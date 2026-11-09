#!/usr/bin/env python3
"""감귤 Agent V0 실제 LLM 정량 평가 도구.

평가용 질문(JSONL)을 읽고, 기존 agents.agent.run_turn을 변경 없이 호출한다.
도구 호출·입력 인수·Python 계산 결과·최종 출력·호출 시간·토큰을 따로 기록한다.

주요 원칙
- 평가셋의 정답은 Agent 내부 계산 함수를 호출해 만들지 않는다.
- 도구 선택, 입력 인수, 최종 수치 표시를 개별 항목으로 채점한다.
- 자연어 평가에는 한계가 있어 '근거 없는 주장'은 검토용 의심 사례로만 표시한다.
- 인증키와 OpenAI 요청 헤더는 저장하지 않는다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.agent import run_turn  # noqa: E402

DEFAULT_DATASET = PROJECT_ROOT / "evals" / "dataset.jsonl"
DEFAULT_MANIFEST = PROJECT_ROOT / "evals" / "manifest.json"
DEFAULT_CSV = PROJECT_ROOT / "data" / "processed" / "daily_prices.csv"

# Python이 실제로 출력하는 라벨을 기준으로 확인한다. 숫자의 존재만으로 정답 판정을 하지 않는다.
REPORT_LABELS = {
    "current_price_krw_per_kg": r"비교 기준가격:\s*([\d,.-]+)원/kg",
    "current_gross_krw": r"(?:최근 관측가격|사용자 입력가격) 기준 즉시 출하 가정 매출:\s*([\d,.-]+)원",
    "remaining_kg": r"\d+일 후 판매 가능 물량\(가정\):\s*([\d,.-]+)kg",
    "extra_storage_cost_krw": r"추가 보관비:\s*([\d,.-]+)원",
    "break_even_price_krw_per_kg": r"지연 출하 손익분기 단가:\s*([\d,.-]+)원/kg",
    "minimum_integer_price_krw_per_kg": r"원 단위 가격 기준 최소 필요 단가:\s*([\d,.-]+)원/kg 이상",
    "required_increase_percent": r"필요 가격 상승률:\s*([\d,.-]+)%",
    "future_price_krw_per_kg": r"가정한 지연 후 가격:\s*([\d,.-]+)원/kg",
    "delayed_sale_minus_storage_krw": r"보관비 차감 후 지연 출하 금액:\s*([\d,.-]+)원",
    "difference_vs_now_krw": r"즉시 출하 대비 차액:\s*([\d,.-]+)원",
    "observed_price_krw_per_kg": r"관측 경락가격:\s*([\d,.-]+)원/kg",
    "observed_date": r"최근 관측일:\s*(\d{4}-\d{2}-\d{2})",
    "transactions": r"거래:\s*([\d,]+)건",
}

# 누락 조건을 질문했는지 파악하기 위한 규칙. 자연어 의미 판정의 완전한 대체물은 아니다.
MISSING_TERMS = {
    "variety": r"품종",
    "grade": r"등급|특등급|등급명",
    "size": r"크기|규격|사이즈",
    "package_kg": r"포장|상자.*kg|포장중량",
    "quantity_kg": r"물량|수량|몇\s*(?:kg|톤)",
    "delay_days": r"보관기간|보관\s*일수|며칠|몇\s*일",
    "daily_storage_cost": r"보관비|보관료|보관\s*비용|추가\s*비용",
    "loss_percent": r"감량|감모|폐기|손실률|부패율",
}
INVALID_TERMS = {
    "loss_percent": r"감량|손실|100%|비율",
    "quantity_kg": r"물량|수량|0kg|0\s*이하|양수",
    "delay_days": r"보관|기간|0일|1일\s*이상",
    "daily_storage_cost": r"보관비|비용|음수|0\s*이상|마이너스",
}

# 아래 탐지는 '검토 필요 플래그'일 뿐 정확한 환각 판정이 아니다.
RISK_PATTERNS = [
    re.compile(r"(?:가격|시세).{0,20}(?:반드시|무조건|확실히)\s*(?:상승|하락|오를|내릴)"),
    re.compile(r"(?:반드시|무조건|확실히).{0,20}(?:이득|수익|손실이\s*없|가격이\s*오르)"),
    re.compile(r"\d+(?:\.\d+)?%\s*(?:확률로|가능성으로)\s*(?:상승|하락|이득)"),
]


def canonical_blob_sha(path: Path) -> str:
    """Windows CRLF 체크아웃도 Git 저장소의 LF 파일과 동일하게 비교한다."""
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_versions(manifest: dict, prices_file: Path, *, allow_mismatch: bool) -> list[dict]:
    """코드·입력데이터가 V0로 고정된 버전인지 검증한다."""
    locations = {
        "agent": PROJECT_ROOT / "agents" / "agent.py",
        "baseline": PROJECT_ROOT / "agents" / "baseline.py",
        "daily_prices": prices_file,
    }
    report = []
    for name, path in locations.items():
        if not path.is_file():
            raise FileNotFoundError(f"필수 파일이 없습니다: {path}")
        actual = canonical_blob_sha(path)
        expected = manifest["git_blob_shas"][name]
        report.append({"name":name,"path":str(path),"expected":expected,"actual":actual,"matched":actual == expected})
    bad = [item for item in report if not item["matched"]]
    if bad and not allow_mismatch:
        affected = ", ".join(i["name"] for i in bad)
        raise ValueError(
            f"고정된 V0와 파일 내용이 다릅니다: {affected}. "
            "V1 비교를 의도했다면 --allow-version-mismatch를 지정하세요."
        )
    return report


def load_cases(path: Path) -> list[dict]:
    """문항·정답 계약이 잘못된 경우 실제 API 호출 전에 중단한다."""
    if not path.is_file():
        raise FileNotFoundError(f"평가셋 파일을 찾을 수 없습니다: {path}")
    cases=[]; seen=set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(),1):
        if not line.strip(): continue
        try: item=json.loads(line)
        except json.JSONDecodeError as e: raise ValueError(f"평가셋 {lineno}번째 행 JSON 오류: {e}") from e
        ident=item.get("id")
        if not isinstance(ident,str) or not ident or ident in seen:
            raise ValueError(f"평가 문항 ID 누락·중복: {ident!r}")
        seen.add(ident)
        if item.get("split") not in ("dev","holdout"):
            raise ValueError(f"{ident}: split은 dev 또는 holdout이어야 합니다.")
        if not isinstance(item.get("turns"),list) or not item["turns"]:
            raise ValueError(f"{ident}: turns가 비어 있습니다.")
        for j,turn in enumerate(item["turns"],1):
            e=turn.get("expected")
            if not isinstance(turn.get("question"),str) or not turn["question"].strip():
                raise ValueError(f"{ident} {j}턴: 질문이 없습니다.")
            if not isinstance(e,dict) or e.get("action") not in ("compare","price","no_data","ask","reject"):
                raise ValueError(f"{ident} {j}턴: expected.action이 유효하지 않습니다.")
            if e["action"] in ("compare","price","no_data"):
                if not isinstance(e.get("args"),dict) or not e.get("required_tool"):
                    raise ValueError(f"{ident} {j}턴: 도구 인수 정답이 없습니다.")
                if e["action"] in ("compare","price") and not e.get("numeric"):
                    raise ValueError(f"{ident} {j}턴: 수치 정답이 없습니다.")
            elif e["action"]=="ask":
                if not e.get("missing") or set(e["missing"]) - MISSING_TERMS.keys():
                    raise ValueError(f"{ident} {j}턴: 누락 항목이 잘못됐습니다.")
            else:
                if e.get("reason") not in INVALID_TERMS:
                    raise ValueError(f"{ident} {j}턴: 오류 유형이 잘못됐습니다.")
        cases.append(item)
    if not cases:raise ValueError("평가 문항이 하나도 없습니다.")
    return cases


class RecordingResponses:
    """OpenAI SDK 응답을 그대로 전달하면서 호출 관찰 정보만 기록한다."""
    def __init__(self, sdk_responses: Any):
        self.sdk_responses=sdk_responses
        self.events: list[dict]=[]

    def create(self, **kwargs: Any) -> Any:
        begin=time.perf_counter()
        response=self.sdk_responses.create(**kwargs)
        elapsed=time.perf_counter()-begin
        functions=[]
        for item in getattr(response,"output",[]) or []:
            if getattr(item,"type",None)!="function_call":continue
            try: decoded=json.loads(getattr(item,"arguments","{}"))
            except (json.JSONDecodeError,TypeError): decoded=None
            functions.append({"name":getattr(item,"name",None),"args":decoded,"call_id":getattr(item,"call_id",None)})
        outputs=[]
        inp=kwargs.get("input")
        if isinstance(inp,list):
            for obj in inp:
                if not isinstance(obj,dict) or obj.get("type")!="function_call_output":continue
                try: result=json.loads(obj.get("output","{}"))
                except (ValueError,TypeError): result=None
                outputs.append({"call_id":obj.get("call_id"),"result":result})
        usage=getattr(response,"usage",None)
        tokens={
            "input":getattr(usage,"input_tokens",None),
            "output":getattr(usage,"output_tokens",None),
            "total":getattr(usage,"total_tokens",None),
        } if usage is not None else None
        self.events.append({
            "response_id":getattr(response,"id",None),
            "status":getattr(response,"status",None),
            "elapsed_seconds":round(elapsed,4),
            "functions":functions,
            "tool_outputs":outputs,
            "tokens":tokens,
        })
        return response


class RecordingClient:
    def __init__(self, sdk_client: Any):
        self.responses=RecordingResponses(sdk_client.responses)


def normalize_num(x: Any) -> Decimal | None:
    if x is None:return None
    if isinstance(x,bool):return None
    try:return Decimal(str(x).replace(",",""))
    except (InvalidOperation,ValueError):return None


def match_arg(a: Any,b: Any) -> bool:
    """숫자는 표현이 달라도 같은 값이면 허용하되, 문자열·null은 구분한다."""
    if a is None or b is None:return a is None and b is None
    if isinstance(a,bool) or isinstance(b,bool):return type(a)==type(b) and a==b
    if isinstance(a,(int,float)) and isinstance(b,(int,float)):
        return normalize_num(a)==normalize_num(b)
    return a==b


def test_args(gold: dict, actual: dict | None) -> tuple[bool,list[str]]:
    if actual is None:return False,["도구 호출의 인수 JSON을 읽을 수 없음"]
    problems=[]
    for field,expected in gold.items():
        if field not in actual or not match_arg(expected,actual[field]):
            problems.append(f"{field}: 기대 {expected!r}, 실제 {actual.get(field,'<누락>')!r}")
    unexpected=set(actual)-set(gold)
    if unexpected:problems.append("추가 인수: "+", ".join(sorted(unexpected)))
    return not problems,problems


def report_numeric_match(answer: str, key: str, expected: Any) -> tuple[bool,str]:
    match=re.search(REPORT_LABELS[key],answer)
    if not match:
        return False,f"{key}: 결과 보고서에서 값이나 라벨을 찾을 수 없음"
    actual=match.group(1)
    if key=="observed_date":
        good=actual==str(expected)
    else:
        good=normalize_num(actual)==normalize_num(expected)
    return good, "" if good else f"{key}: 기대 {expected}, 실제 {actual}"


def collect_tools(events: list[dict]) -> list[dict]:
    return [tool for event in events for tool in event["functions"]]


def collect_tool_outputs(events: list[dict]) -> list[dict]:
    return [o for event in events for o in event["tool_outputs"]]


def check_claim_flags(answer: str) -> list[str]:
    return sorted({m.group(0) for p in RISK_PATTERNS for m in p.finditer(answer)})


def check_tool_result_numeric(action: str, expected: dict, events: list[dict]) -> tuple[bool, list[str]]:
    """LLM의 문장과 독립적으로, Python 도구가 실제 반환한 구조화 숫자도 확인한다."""
    outputs = [o.get("result") for o in collect_tool_outputs(events) if isinstance(o.get("result"), dict)]
    if action == "compare":
        results = [obj for obj in outputs if "current_gross_krw" in obj]
        if not results:
            return False, ["compare_shipping 도구 결과가 전달되지 않음"]
        obj = results[-1]
        actual = {k: obj.get(k) for k in expected["numeric"] if k in obj}
        hypothesis = obj.get("future_hypothesis", {})
        for key in ("future_price_krw_per_kg", "delayed_sale_minus_storage_krw", "difference_vs_now_krw"):
            if key in expected["numeric"]:
                actual[key] = hypothesis.get({"future_price_krw_per_kg":"assumed_price_krw_per_kg"}.get(key,key))
        problems = [f"도구값 {k}: 기대 {v}, 실제 {actual.get(k)}"
                    for k,v in expected["numeric"].items()
                    if normalize_num(actual.get(k)) != normalize_num(v)]
        return not problems, problems
    if action == "price":
        results = [obj for obj in outputs if obj.get("has_price")]
        if not results:
            return False, ["get_market_price 성공 결과가 전달되지 않음"]
        obj = results[-1]
        numeric = expected["numeric"]
        fields = {"observed_price_krw_per_kg":"observed_price_krw_per_kg", "observed_date":"observed_date", "transactions":"transactions"}
        problems=[]
        for gold_key,key in fields.items():
            actual=obj.get(key)
            correct = (str(actual)==str(numeric[gold_key])) if gold_key=="observed_date" else normalize_num(actual)==normalize_num(numeric[gold_key])
            if not correct:problems.append(f"도구값 {key}: 기대 {numeric[gold_key]}, 실제 {actual}")
        return not problems, problems
    return True, []


def grade_step(expected: dict, answer: str, events: list[dict]) -> dict:
    """평가 기준은 V0 기능 범위에 한정한다. 규칙 기반 자연어 채점의 한계도 남긴다."""
    action=expected["action"]
    tools=collect_tools(events)
    outs=collect_tool_outputs(events)
    found=[c for c in tools if c.get("name")==expected.get("required_tool")]
    tool_ok=None; args_ok=None; numeric_ok=None; tool_result_ok=None; safe_ok=None; notes=[]
    missing_coverage=None; unnecessary_lookup=False

    if action in ("compare","price","no_data"):
        tool_ok=bool(found)
        if not found:
            notes.append(f"필요 도구 {expected['required_tool']}를 호출하지 않음")
            args_ok=False
        else:
            # 필요 도구를 반복 호출한 경우, 정확한 인수로 호출한 실행이 있는지 본다.
            arg_grades=[test_args(expected["args"],call.get("args")) for call in found]
            args_ok=any(ok for ok,_ in arg_grades)
            if not args_ok:notes += [p for ok,problems in arg_grades for p in problems][:8]

        if action=="no_data":
            success_reports=[o for o in outs if isinstance(o.get("result"),dict) and o["result"].get("has_price")]
            absence=re.search(r"없|찾지\s*못|조회(?:되|할)\s*수\s*없|확인(?:되|할)\s*수\s*없",answer)
            numeric_ok=bool(absence) and not success_reports and "[감귤 경락가격 조회 — 검증된 도구 결과]" not in answer
            if not numeric_ok:notes.append("일치 거래 없음에 대한 명시적 응답 확인 실패")
        else:
            if action in ("compare", "price"):
                tool_result_ok, tool_problems = check_tool_result_numeric(action, expected, events)
                notes += tool_problems[:8]
            # 정답의 개별 필드가 최종 사용자 출력에 그대로 들어 있는지 확인.
            numerical=[report_numeric_match(answer,k,v) for k,v in expected["numeric"].items()]
            numeric_ok=all(ok for ok,_ in numerical)
            notes += [msg for ok,msg in numerical if not ok][:8]
            if action=="compare" and expected["args"].get("future_price_per_kg") is not None:
                if "가정" not in answer or "예측 아님" not in answer:
                    numeric_ok=False
                    notes.append("미래 가격은 사용자 가정이며 예측이 아님을 표시하지 않음")
            if action=="compare" and expected["args"].get("current_price_per_kg") is not None:
                if "(사용자 입력)" not in answer:
                    numeric_ok=False
                    notes.append("사용자 입력 단가를 시장 관측가격으로 혼동")
            # 도구가 올바른 값을 제공한 경우에만 최종 결과 점수 인정.
        pass_step=bool(tool_ok and args_ok and numeric_ok and (tool_result_ok is not False))
    elif action=="ask":
        forbidden=[c for c in tools if c.get("name")=="compare_shipping"]
        missing_coverage={field:bool(re.search(MISSING_TERMS[field],answer)) for field in expected["missing"]}
        safe_ok=not forbidden and "[감귤 출하 시점 비교 — 검증된 도구 계산 결과]" not in answer
        unnecessary_lookup=any(c.get("name")=="get_market_price" for c in tools)
        # 누락값을 순차 질문할 수 있으므로 '최소 1개 이상 확인'과 '모두 확인'을 따로 센다.
        any_asked=bool(answer.strip()) and any(missing_coverage.values())
        all_asked=bool(answer.strip()) and all(missing_coverage.values())
        if not safe_ok:notes.append("필수 조건이 없는데 compare_shipping 호출 또는 계산 보고서 생성")
        if not any_asked:notes.append("누락된 조건을 묻는 질문이 감지되지 않음")
        if not all_asked:notes.append("아직 묻지 않은 필수 조건: "+", ".join(k for k,v in missing_coverage.items() if not v))
        if unnecessary_lookup:notes.append("필수 조회조건 확인 전 가격 도구를 호출함(효율성 참고)")
        pass_step=bool(safe_ok and any_asked)
    else:  # reject
        successful_comparisons=[o for o in outs if isinstance(o.get("result"),dict) and "current_gross_krw" in o["result"]]
        reason=expected["reason"]
        safe_ok=not successful_comparisons and "[감귤 출하 시점 비교 — 검증된 도구 계산 결과]" not in answer
        informative=bool(re.search(INVALID_TERMS[reason],answer)) and bool(re.search(r"오류|불가|불가능|할\s*수\s*없|미만|이상|0보다|수정|잘못|음수|양수|안\s*됩니다|안\s*돼",answer))
        if not safe_ok:notes.append("유효하지 않은 입력으로 계산 보고서가 생성됨")
        if not informative:notes.append("유효하지 않은 조건이라는 설명이 부족함")
        pass_step=bool(safe_ok and informative)

    flags=check_claim_flags(answer)
    if flags:notes.append("근거 없는 단정 표현 수동 검토 필요")
    return dict(
        passed=pass_step,
        action=action,
        tool_selection_ok=tool_ok,
        parameter_accuracy_ok=args_ok,
        numeric_accuracy_ok=numeric_ok,
        tool_result_accuracy_ok=tool_result_ok,
        safe_handling_ok=safe_ok,
        missing_coverage=missing_coverage,
        unnecessary_lookup=unnecessary_lookup,
        unsupported_claim_review_flags=flags,
        notes=notes,
    )


def summarize(runs: list[dict], *, model: str, seconds: float, prices: dict) -> dict:
    """분모와 측정 불가 항목을 함께 기록해 오해를 방지한다."""
    steps=[st for run in runs for st in run.get("steps",[])]
    recorded=[step for step in steps if "evaluation" in step]
    all_evals=[s["evaluation"] for s in recorded]
    def ratio(label: str) -> dict:
        vals=[g.get(label) for g in all_evals if g.get(label) is not None]
        return {"passed":sum(v is True for v in vals),"total":len(vals),"rate":round(sum(v is True for v in vals)/len(vals),4) if vals else None}
    case_success=[r for r in runs if r.get("steps") and all(step.get("evaluation",{}).get("passed") for step in r["steps"])]
    lat=[float(s["latency_seconds"]) for s in recorded if s.get("latency_seconds") is not None]
    events=[event for step in recorded for event in step.get("api_events",[])]
    valid_usage=[event["tokens"] for event in events if event.get("tokens") and event["tokens"].get("input") is not None and event["tokens"].get("output") is not None]
    ti=sum(u["input"] for u in valid_usage)
    to=sum(u["output"] for u in valid_usage)
    estimate=(ti*prices["input"]+to*prices["output"])/1e6 if prices["input"] is not None and prices["output"] is not None and len(valid_usage)==len(events) else None
    category_rates={}
    for cat in sorted({r["category"] for r in runs}):
        sub=[r for r in runs if r["category"]==cat]
        p=sum(r in case_success for r in sub)
        category_rates[cat]={"passed":p,"total":len(sub),"rate":round(p/len(sub),4) if sub else None}
    missing=[s["evaluation"] for s in recorded if s["evaluation"]["action"]=="ask"]
    all_missing=[s for s in missing if s.get("missing_coverage")]
    return {
        "model":model,
        "cases_attempted":len(runs),
        "cases_passed":len(case_success),
        "case_pass_rate":round(len(case_success)/len(runs),4) if runs else None,
        "steps_attempted":len(steps),
        "steps_graded":len(recorded),
        "steps_passed":sum(g["passed"] for g in all_evals),
        "step_pass_rate":round(sum(g["passed"] for g in all_evals)/len(steps),4) if steps else None,
        "api_or_agent_errors":len(steps)-len(recorded),
        "tool_selection":ratio("tool_selection_ok"),
        "parameter_extraction":ratio("parameter_accuracy_ok"),
        "numeric_output":ratio("numeric_accuracy_ok"),
        "tool_result_numeric":ratio("tool_result_accuracy_ok"),
        "safe_ask_or_reject":ratio("safe_handling_ok"),
        "missing_fields_all_asked":{"passed":sum(all(c.values()) for c in (g["missing_coverage"] for g in all_missing)),"total":len(all_missing)},
        "premature_price_lookup_in_missing_questions":sum(g["unnecessary_lookup"] for g in missing),
        "unsupported_claims_review_count":sum(bool(g["unsupported_claim_review_flags"]) for g in all_evals),
        "category_rates":category_rates,
        "latency_seconds":{"mean":round(statistics.mean(lat),3) if lat else None,"median":round(statistics.median(lat),3) if lat else None,"max":round(max(lat),3) if lat else None},
        "api_calls":len(events),
        "token_usage":{"input":ti,"output":to,"responses_with_usage":len(valid_usage),"responses_total":len(events)},
        "estimated_api_cost_usd":round(estimate,6) if estimate is not None else None,
        "cost_caution":"단순 표준 토큰단가 추정치이며 캐시 할인·추가 과금항목은 반영하지 않음" if estimate is not None else "가격 미입력 또는 토큰 기록 누락으로 비용을 추정하지 않음",
        "wall_time_seconds":round(seconds,2),
    }


def build_parser() -> argparse.ArgumentParser:
    p=argparse.ArgumentParser(description="제주 감귤 Agent V0 재현 가능한 LLM 평가")
    p.add_argument("--validate",action="store_true",help="평가셋·버전만 확인하고 API는 호출하지 않음")
    p.add_argument("--live",action="store_true",help="실제 OpenAI API 호출(별도 과금 가능)")
    p.add_argument("--split",choices=["dev","holdout","all"],default="dev")
    p.add_argument("--limit",type=int,help="실행할 문항 수 제한(스모크 테스트용)")
    p.add_argument("--case-id",action="append",help="특정 문항만 실행, 여러 번 지정 가능")
    p.add_argument("--repeats",type=int,default=1,help="한 문항을 독립적으로 반복 실행")
    p.add_argument("--model",help="사용 모델. 미설정 시 .env의 OPENAI_MODEL")
    p.add_argument("--dataset",type=Path,default=DEFAULT_DATASET)
    p.add_argument("--prices-file",type=Path,default=DEFAULT_CSV)
    p.add_argument("--out-dir",type=Path,default=PROJECT_ROOT/"evals"/"results")
    p.add_argument("--allow-version-mismatch",action="store_true",help="고정된 V0 코드와 다른 V1을 평가할 때 사용")
    p.add_argument("--input-usd-per-million",type=float,help="입력 100만 토큰당 달러 요금(사용자가 직접 확인 후 지정)")
    p.add_argument("--output-usd-per-million",type=float,help="출력 100만 토큰당 달러 요금(사용자가 직접 확인 후 지정)")
    p.add_argument("--delay-seconds",type=float,default=0.0,help="문항 사이 요청 대기")
    return p


def main(argv: list[str]|None=None) -> int:
    p=build_parser();opts=p.parse_args(argv)
    if opts.limit is not None and opts.limit<=0:p.error("--limit는 1 이상이어야 합니다.")
    if opts.repeats<=0:p.error("--repeats는 1 이상이어야 합니다.")
    if opts.delay_seconds<0:p.error("--delay-seconds는 0 이상이어야 합니다.")
    for rate in [opts.input_usd_per_million, opts.output_usd_per_million]:
        if rate is not None and rate<0:p.error("토큰 요금은 0 이상이어야 합니다.")
    if opts.live == opts.validate:
        p.error("--validate(무과금 점검) 또는 --live(실제 API 평가) 중 하나만 선택하세요.")

    cases=load_cases(opts.dataset)
    manifest=json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    try: versions=validate_versions(manifest,opts.prices_file,allow_mismatch=opts.allow_version_mismatch)
    except (OSError,ValueError) as exc:
        print(f"[파일 버전 확인 실패] {exc}",file=sys.stderr)
        return 2
    selected=[c for c in cases if opts.split=="all" or c["split"]==opts.split]
    if opts.case_id:
        wanted=set(opts.case_id)
        absent=wanted-set(c["id"] for c in cases)
        if absent:p.error("없는 평가 문항: "+", ".join(sorted(absent)))
        selected=[c for c in selected if c["id"] in wanted]
    if opts.limit is not None:selected=selected[:opts.limit]
    if not selected:p.error("실행할 평가 문항이 없습니다. --split 또는 --case-id를 확인하세요.")
    print(f"평가셋: {len(cases)}문항, 총 {sum(len(c['turns']) for c in cases)}턴 | 이번 실행: {len(selected)}문항 x {opts.repeats}회")
    print(f"코드·가격 데이터 버전: {'V0 일치' if all(v['matched'] for v in versions) else '변경됨(V1 등)'}")
    if opts.validate:
        print("[점검 완료] 실제 API 호출 없음. --live를 지정해야 성능을 측정합니다.")
        return 0

    try:
        from dotenv import load_dotenv
        from openai import OpenAI
    except ImportError as exc:
        print(f"[오류] 라이브러리 없음: {exc}. python -m pip install -r requirements.txt",file=sys.stderr)
        return 2
    load_dotenv(PROJECT_ROOT/".env",override=False)
    if not os.environ.get("OPENAI_API_KEY"):
        print("[오류] OPENAI_API_KEY가 없습니다. 프로젝트 루트 .env를 확인하세요.",file=sys.stderr)
        return 2
    model=opts.model or os.environ.get("OPENAI_MODEL") or "gpt-5-mini"
    print(f"[실제 API 평가 시작] 모델: {model}. API 사용료가 발생할 수 있습니다.")
    client=RecordingClient(OpenAI())
    started=time.perf_counter()
    runs=[]
    for item in selected:
        for trial in range(1,opts.repeats+1):
            previous=None; steps=[]
            for i,turn in enumerate(item["turns"],1):
                position=len(client.responses.events)
                begin=time.perf_counter()
                try:
                    answer, previous=run_turn(
                        client,turn["question"],previous_response_id=previous,
                        model=model,prices_file=opts.prices_file,
                    )
                    events=client.responses.events[position:]
                    grade=grade_step(turn["expected"],answer,events)
                    steps.append({
                        "turn":i,
                        "question":turn["question"],
                        "answer":answer,
                        "expected":turn["expected"],
                        "evaluation":grade,
                        "latency_seconds":round(time.perf_counter()-begin,4),
                        "api_events":events,
                    })
                except Exception as exc:  # 한 사례가 실패해도 다른 문항을 계속 평가한다.
                    steps.append({
                        "turn":i,"question":turn["question"],
                        "expected":turn["expected"],
                        "error_type":type(exc).__name__,
                        "error_note":"API 인증·통신 또는 Agent 내부 오류(세부 정보는 로컬에서 확인)",
                        "latency_seconds":round(time.perf_counter()-begin,4),
                        "api_events":client.responses.events[position:],
                    })
                    break
            run={"id":item["id"],"category":item["category"],"split":item["split"],"trial":trial,"steps":steps}
            runs.append(run)
            status="통과" if all(s.get("evaluation",{}).get("passed") for s in steps) else "실패"
            print(f"  {item['id']} ({item['category']}, 반복 {trial}): {status}")
            if opts.delay_seconds and not (item==selected[-1] and trial==opts.repeats):time.sleep(opts.delay_seconds)

    summary=summarize(runs,model=model,seconds=time.perf_counter()-started,prices={"input":opts.input_usd_per_million,"output":opts.output_usd_per_million})
    opts.out_dir.mkdir(parents=True,exist_ok=True)
    metadata={
        "evaluated_at_utc":datetime.now(timezone.utc).isoformat(),
        "model":model,
        "split":opts.split,
        "repeats":opts.repeats,
        "case_ids":[x["id"] for x in selected],
        "dataset_sha256":file_sha256(opts.dataset),
        "manifest_baseline_commit":manifest["baseline_commit"],
        "version_checks":versions,
        "important":"실제 API 응답의 자연어 품질·근거 주장은 일부 규칙 기반 검사라 수동 검토가 필요함",
    }
    (opts.out_dir/"metadata.json").write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (opts.out_dir/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    with (opts.out_dir/"cases.jsonl").open("w",encoding="utf-8",newline="\n") as out:
        for run in runs:out.write(json.dumps(run,ensure_ascii=False)+"\n")
    print(f"결과: {summary['cases_passed']}/{summary['cases_attempted']}문항 통과, {summary['steps_passed']}/{summary['steps_attempted']}턴 통과")
    print(f"도구 선택: {summary['tool_selection']} | 파라미터: {summary['parameter_extraction']} | 출력 수치: {summary['numeric_output']}")
    print(f"기록 위치: {opts.out_dir}")
    print("※ 근거 없는 주장 플래그는 규칙 기반이므로 수동 검토 없이 오류율로 해석하지 마세요.")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
