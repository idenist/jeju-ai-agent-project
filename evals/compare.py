#!/usr/bin/env python3
"""V0와 V1의 동일 평가셋 성능 차이를 수치로 비교한다."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


KEYS = (
    ("case_pass_rate", "전체 문항 성공률"),
    ("step_pass_rate", "전체 대화 턴 성공률"),
    ("tool_selection", "도구 선택 정확도"),
    ("parameter_extraction", "인수 추출 정확도"),
    ("tool_result_numeric", "도구 계산 결과 정확도"),
    ("numeric_output", "최종 숫자 정확도"),
    ("safe_ask_or_reject", "정보 누락·오류 안전 처리"),
)


def read_run(path: Path) -> tuple[dict, dict, list[dict]]:
    try:
        metadata = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
        summary = json.loads((path / "summary.json").read_text(encoding="utf-8"))
        cases = [json.loads(l) for l in (path / "cases.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    except (OSError, ValueError) as e:
        raise ValueError(f"{path} 결과 파일을 읽지 못했습니다: {e}") from e
    return metadata,summary,cases


def metric(s: dict, key: str) -> float | None:
    v=s.get(key)
    if isinstance(v, dict):v=v.get("rate")
    return float(v) if v is not None else None


def compare_runs(left: tuple, right: tuple, *, allow_different_model: bool = False) -> dict:
    m0,s0,c0=left;m1,s1,c1=right
    if m0.get("dataset_sha256") != m1.get("dataset_sha256"):
        raise ValueError("평가셋 해시가 달라 비교할 수 없습니다. V0·V1은 같은 문항·정답이어야 합니다.")
    def ids(cases): return [(x["id"],x["trial"]) for x in cases]
    if ids(c0)!=ids(c1) or m0.get("case_ids") != m1.get("case_ids"):
        raise ValueError("평가 문항, 실행 순서 또는 반복 횟수가 다릅니다.")
    if not c0 or not c1:raise ValueError("평가 결과가 비어 있습니다.")
    def data_sha(m):
        for item in m.get("version_checks",[]):
            if item.get("name")=="daily_prices":return item.get("actual")
        return None
    if data_sha(m0)!=data_sha(m1) or data_sha(m0) is None:
        raise ValueError("경락가격 CSV 버전이 다르거나 확인할 수 없습니다. 같은 데이터로 재평가해야 공정하게 비교할 수 있습니다.")
    if m0.get("model")!=m1.get("model") and not allow_different_model:
        raise ValueError("모델이 다릅니다. 구조 변경의 효과를 비교하려면 같은 모델을 사용하세요. 모델 변경 비교는 --allow-different-model 지정.")
    rows=[]
    for key,label in KEYS:
        a=metric(s0,key);b=metric(s1,key)
        rows.append({"metric":key,"name":label,"v0":a,"v1":b,"difference_percentage_points":round((b-a)*100,2) if a is not None and b is not None else None})
    # 성공/실패가 뒤집힌 사례를 기록하면 개선을 설명하기 쉽다.
    def passed(case):
        steps=case.get("steps",[])
        return bool(steps) and all(x.get("evaluation",{}).get("passed",False) for x in steps)
    improved=[];regressed=[]
    for a,b in zip(c0,c1):
        label=f"{a['id']} (반복 {a['trial']})"
        if not passed(a) and passed(b):improved.append(label)
        if passed(a) and not passed(b):regressed.append(label)
    return {
        "dataset_sha256":m0["dataset_sha256"],
        "model_v0":m0.get("model"),"model_v1":m1.get("model"),
        "caution":"모델이 다르면 Agent 구조 외 변수도 변했으므로 인과적 개선으로 단정하지 않음" if m0.get("model")!=m1.get("model") else "평가 질문·정답·모델이 동일함. API의 비결정성 및 환경 차이는 여전히 고려해야 함",
        "metrics":rows,
        "cases_improved":improved,
        "cases_regressed":regressed,
        "latency_mean_seconds_v0":s0.get("latency_seconds",{}).get("mean"),
        "latency_mean_seconds_v1":s1.get("latency_seconds",{}).get("mean"),
        "total_api_calls_v0":s0.get("api_calls"),
        "total_api_calls_v1":s1.get("api_calls"),
        "total_input_tokens_v0":s0.get("token_usage",{}).get("input"),
        "total_input_tokens_v1":s1.get("token_usage",{}).get("input"),
        "total_output_tokens_v0":s0.get("token_usage",{}).get("output"),
        "total_output_tokens_v1":s1.get("token_usage",{}).get("output"),
    }


def main(argv:list[str]|None=None)->int:
    p=argparse.ArgumentParser(description="감귤 Agent V0/V1 고정 평가셋 성능 비교")
    p.add_argument("v0",type=Path,help="V0 평가 결과 폴더")
    p.add_argument("v1",type=Path,help="V1 평가 결과 폴더")
    p.add_argument("--allow-different-model",action="store_true")
    p.add_argument("--output",type=Path,help="선택적 JSON 보고서 경로")
    a=p.parse_args(argv)
    try: result=compare_runs(read_run(a.v0),read_run(a.v1),allow_different_model=a.allow_different_model)
    except ValueError as exc:p.error(str(exc))
    print(f"모델: {result['model_v0']} → {result['model_v1']}")
    print("평가 지표                       V0       V1      차이(%p)")
    for r in result["metrics"]:
        fmt=lambda v:f"{v*100:6.1f}%" if v is not None else "     - "
        diff=f"{r['difference_percentage_points']:+7.2f}" if r["difference_percentage_points"] is not None else "      -"
        print(f"{r['name']:<22} {fmt(r['v0'])}  {fmt(r['v1'])}   {diff}")
    print(f"개선 문항: {result['cases_improved']}")
    print(f"회귀 문항: {result['cases_regressed']}")
    print("주의:",result["caution"])
    if a.output:
        a.output.parent.mkdir(parents=True,exist_ok=True)
        a.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        print("비교 보고서 저장:",a.output)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
