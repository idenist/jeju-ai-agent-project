"""One local shipment, immutable planning baseline and auditable settlement revisions.

No receipts, identities, filesystem writes, model calls or inferred actual costs.
Imported outputs/citations are never executed or reused: reconciliation is regenerated.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import hashlib
import json
import re
from uuid import uuid4

from .casefiles import _canonical, parse_case, MAX_BYTES

FORMAT = "jeju-shipment-record"
RULE = "settlement-v1-decimal-half-up-0.01krw"
CATEGORIES = {"commission": "수수료", "sorting": "선별비", "packaging": "포장비",
              "transport": "운송비", "storage": "보관비", "other": "기타"}
KINDS = {"unknown": "실제값·가정 구분 미확인", "user_reported": "사용자가 실제 기록으로 입력",
         "hypothetical": "가상 예시·사용자 가정"}
SCALARS = {"shipment_date": "실행일", "initial_weight_kg": "실제 출하중량(kg)",
    "sold_weight_kg": "판매중량(kg)", "gross_krw": "정산서 매출(원)",
    "statement_net_krw": "정산서 지급액(원)", "received_krw": "사용자 입력 입금액(원)"}
FLAGS = {"deductions_complete": "정산서 공제 전체 확인", "external_complete": "정산 밖 비용 전체 확인",
         "overlap_distinct": "양쪽 같은 비용범주의 항목은 서로 다른 비용임을 확인"}


def digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def model_receipt(result):
    """The model gets a completion receipt, not the local financial result.

    Keep this explicit allowlist: raw inputs, money, citations, hashes and baseline
    snapshots belong to the local renderer/export, including on subsequent turns.
    """
    return {"status": result.get("status", "INVALID_INPUT"), "analysis_type": "settlement",
            "record_revision": result.get("record_revision"), "local_result_displayed": "amounts" in result,
            "message": "정산 금액·입력·근거는 로컬 화면에서 확인합니다. 모델에는 상세 정산값을 제공하지 않습니다. 기록이 없으면 정산 카드를 연결·확인하세요."}


def reject_private_record(content):
    """Package guard for our known financial export format, not a universal PII scan."""
    try:
        document = json.loads(content)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return
    if isinstance(document, dict) and document.get('format') == FORMAT:
        raise ValueError("개인 출하·정산 JSON은 제출 패키지에 포함할 수 없습니다.")


def empty_values():
    return {**dict.fromkeys(SCALARS), **dict.fromkeys(FLAGS, False), "input_kind": "unknown",
            "sales": [], "deductions": [], "external_costs": []}


def number(value, *, weight=False):
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("숫자는 0 이상의 유한값이어야 합니다.")
    raw = str(value).strip()
    if len(raw) > 32 or not re.fullmatch(r"\d+(?:\.\d{1,6})?", raw):
        raise ValueError("숫자는 소수 여섯 자리 이내, 지수·음수 없이 입력하세요.")
    try:
        val = Decimal(raw)
        if not val.is_finite() or val > Decimal("1000000000000"):
            raise ValueError("숫자 범위를 확인하세요.")
        if not weight and val.as_tuple().exponent < -2:
            raise ValueError("원 금액은 소수 두 자리 이내로 입력하세요.")
        return format(val.normalize(), "f") if val else "0"
    except InvalidOperation as exc:
        raise ValueError("숫자를 확인하세요.") from exc


def normalize(values):
    if not isinstance(values, dict) or set(values) != set(empty_values()):
        raise ValueError("출하·정산 입력 필드가 다릅니다.")
    v = deepcopy(values)
    if not isinstance(v["input_kind"], str) or v["input_kind"] not in KINDS:
        raise ValueError("실제값/가정 구분을 확인하세요.")
    for key in FLAGS:
        if type(v[key]) is not bool:
            raise ValueError("확인 표시는 true/false여야 합니다.")
    for key in SCALARS:
        if key == "shipment_date":
            if v[key] == "": v[key] = None
            if v[key] is not None and (not isinstance(v[key], str) or date.fromisoformat(v[key]).isoformat() != v[key]):
                raise ValueError("실행일은 YYYY-MM-DD로 입력하세요.")
        else:
            v[key] = number(v[key], weight=key.endswith("_kg"))
    for key in ["sales", "deductions", "external_costs"]:
        items = v[key]
        if not isinstance(items, list) or len(items) > 12:
            raise ValueError("한 출하의 항목은 종류별 최대 12개입니다.")
        ids = set()
        for item in items:
            expected = {"id", "weight_kg", "price_per_kg"} if key == "sales" else {"id", "category", "amount_krw"}
            if not isinstance(item, dict) or set(item) != expected:
                raise ValueError("판매/비용 항목 필드가 다릅니다.")
            if not isinstance(item["id"], str) or not re.fullmatch(r"[a-z][0-9]{1,3}", item["id"]) or item["id"] in ids:
                raise ValueError("항목 ID는 영문 한 글자+숫자이며 목록 안에서 고유해야 합니다.")
            ids.add(item["id"])
            if key == "sales":
                item["weight_kg"] = number(item["weight_kg"], weight=True)
                item["price_per_kg"] = number(item["price_per_kg"])
            else:
                if not isinstance(item["category"], str) or item["category"] not in CATEGORIES:
                    raise ValueError("지원하는 비용 범주를 선택하세요.")
                item["amount_krw"] = number(item["amount_krw"])
    return v


def _fields(values):
    for key in sorted(values):
        value = values[key]
        if isinstance(value, list):
            for item in value:
                for field in sorted(item):
                    val = item[field]
                    if field != "id": yield f"{key}.{item['id']}.{field}", val
        else:
            yield key, value


def _baseline(case_bytes, selected_option):
    saved = parse_case(case_bytes)
    from agents.common.calculation_service import compare_shipment_options
    if compare_shipment_options(saved["snapshot"]["scenario"])["status"] != "OK":
        raise ValueError("먼저 출하 전 비교 조건을 완성하고 계산하세요.")
    if isinstance(selected_option, str):
        selected_option = {"kind": "immediate", "option_id": None} if selected_option == 'current' else {"kind": "delayed", "option_id": selected_option}
    ids = [x['id'] for x in saved['snapshot']['scenario']['options']]
    if not isinstance(selected_option, dict) or set(selected_option) != {'kind', 'option_id'} or not (
        selected_option == {'kind':'immediate', 'option_id':None} or
        (selected_option.get('kind') == 'delayed' and selected_option.get('option_id') in ids)):
        raise ValueError("연결할 원래 대안을 선택하세요.")
    # Do not copy free text, archived results, conversation or absolute data paths.
    return {"case_sha256": hashlib.sha256(case_bytes).hexdigest(), "snapshot": saved["snapshot"],
            "snapshot_sha256": digest(saved["snapshot"]), "selected_option": deepcopy(selected_option),
            "input_context": saved["input_context"],
            "data_revision_sha256": digest(saved["data_revision"])}


class ShipmentStore:
    """Single event with bounded immutable revision history; no shared persistence."""
    def __init__(self):
        self._record = None
        self.imported = False

    @property
    def revision(self):
        return len(self._record["revisions"]) if self._record else 0

    def snapshot(self):
        return deepcopy(self._record)

    def values(self):
        return deepcopy(self._record["revisions"][-1]["values"]) if self.revision else empty_values()

    def connect(self, case_bytes, selected_option):
        if self._record:
            raise ValueError("현재 기록을 내려받거나 삭제한 후 새 기록을 연결하세요.")
        baseline = _baseline(case_bytes, selected_option)
        self._record = {"record_id": uuid4().hex, "baseline": baseline, "revisions": []}

    def confirm(self, values, expected_revision):
        if not self._record or type(expected_revision) is not int or expected_revision != self.revision:
            raise ValueError("기록 버전이 바뀌었습니다. 최신 기록을 확인하세요.")
        if self.revision >= 50:
            raise ValueError("수정 이력은 최대 50개입니다. 기록을 내려받아 보관하세요.")
        candidate = normalize(values)
        if self.revision and candidate == self.values(): return False
        previous = self._record["revisions"][-1] if self.revision else None
        before = dict(_fields(previous["values"])) if previous else {}
        provenance = {}
        for path, value in _fields(candidate):
            provenance[path] = deepcopy(previous["provenance"][path]) if previous and path in before and before[path] == value else {
                "source": "manual_card", "confirmed_revision": self.revision + 1}
        node = {"revision": self.revision + 1, "values": candidate, "provenance": provenance,
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
                "previous_sha256": digest(previous) if previous else digest(self._record["baseline"])}
        self._record["revisions"].append(node)
        return True

    def clear(self):
        self._record = None
        self.imported = False

    def export(self):
        if not self._record: raise ValueError("연결한 출하 기록이 없습니다.")
        portable = ShipmentStore()
        portable._record = self.snapshot()
        result = _canonical({"format": FORMAT, "version": 1, "payload": self._record,
                             "derived_result": reconcile(portable), "sha256": digest(self._record)})
        if len(result) > MAX_BYTES: raise ValueError("출하 기록은 1MiB 이하이어야 합니다.")
        return result

    def restore(self, content, expected_revision):
        if type(expected_revision) is not int or expected_revision != self.revision:
            raise ValueError("기록 버전이 바뀌었습니다.")
        candidate = parse_shipment(content)
        self._record = candidate
        self.imported = True


def parse_shipment(content):
    try:
        if not isinstance(content, bytes) or len(content) > MAX_BYTES: raise ValueError()
        def unique(pairs):
            obj = {}
            for key, val in pairs:
                if key in obj: raise ValueError()
                obj[key] = val
            return obj
        def invalid(_): raise ValueError()
        doc = json.loads(content.decode("utf-8-sig"), object_pairs_hook=unique, parse_constant=invalid)
        if set(doc) != {"format", "version", "payload", "derived_result", "sha256"} or doc["format"] != FORMAT or type(doc["version"]) is not int or doc["version"] != 1: raise ValueError()
        record = doc["payload"]
        if not isinstance(record, dict) or set(record) != {"record_id", "baseline", "revisions"} or not re.fullmatch(r"[a-f0-9]{32}", record["record_id"]): raise ValueError()
        if digest(record) != doc["sha256"]: raise ValueError()
        base = record["baseline"]
        if not isinstance(base, dict) or set(base) != {"case_sha256", "snapshot", "snapshot_sha256", "selected_option", "input_context", "data_revision_sha256"}: raise ValueError()
        for key in ["case_sha256", "snapshot_sha256", "data_revision_sha256"]:
            if not isinstance(base[key], str) or not re.fullmatch(r"[a-f0-9]{64}", base[key]): raise ValueError()
        if digest(base["snapshot"]) != base["snapshot_sha256"]: raise ValueError()
        # Validate the original snapshot with the existing case contract, without accepting archived output.
        saved = {"name": "기준 검토", "created_at_kst": "2026-11-09T00:00:00+09:00", "input_context": base["input_context"],
                 "snapshot": base["snapshot"], "history": [], "data_revision": [], "archived_results": []}
        checked = _baseline(_canonical({"format": "jeju-shipping-case", "version": 1, "payload": saved, "sha256": digest(saved)}), base["selected_option"])
        if checked["snapshot_sha256"] != base["snapshot_sha256"]: raise ValueError()
        if not isinstance(record["revisions"], list) or len(record["revisions"]) > 50: raise ValueError()
        prev = base
        for i, node in enumerate(record["revisions"], 1):
            if not isinstance(node, dict) or set(node) != {"revision", "values", "provenance", "confirmed_at", "previous_sha256"}: raise ValueError()
            if type(node["revision"]) is not int or node["revision"] != i or node["previous_sha256"] != digest(prev): raise ValueError()
            if normalize(node["values"]) != node["values"]: raise ValueError()
            fields = dict(_fields(node["values"]))
            if not isinstance(node["provenance"], dict) or set(node["provenance"]) != set(fields): raise ValueError()
            prior_fields = dict(_fields(prev["values"])) if i > 1 else {}
            for path, info in node["provenance"].items():
                if not isinstance(info, dict) or set(info) != {"source", "confirmed_revision"} or type(info["confirmed_revision"]) is not int: raise ValueError()
                expected = prev["provenance"][path] if path in prior_fields and prior_fields[path] == fields[path] else {"source": "manual_card", "confirmed_revision": i}
                if info != expected: raise ValueError()
            if datetime.fromisoformat(node["confirmed_at"]).tzinfo is None: raise ValueError()
            prev = node
        portable = ShipmentStore()
        portable._record = deepcopy(record)
        if doc["derived_result"] != reconcile(portable): raise ValueError()
        return deepcopy(record)
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError, OverflowError, ArithmeticError) as exc:
        raise ValueError("출하 JSON의 형식·버전·해시·이력·입력을 확인하세요. 원래 기록은 유지됩니다.") from exc


def reconcile(store):
    if not isinstance(store, ShipmentStore) or not store.revision:
        return {"status": "NEEDS_INPUT", "analysis_type": "settlement", "message": "출하 기록을 연결하고 정산 카드에서 아는 항목을 확인·저장하세요."}
    record, v = store.snapshot(), store.values()
    rid, rev = record["record_id"], store.revision
    registry, paths = {}, {}
    prefix = f"{rid[:8]}-v{rev}"
    for i, (path, value) in enumerate(_fields(v), 1):
        ref = f"입력-{prefix}-{i}"
        paths[path] = ref
        registry[ref] = {"kind": "input", "record_id": rid, "revision": rev, "path": path, "value": value,
            "input_kind": v["input_kind"], **record["revisions"][-1]["provenance"][path]}
    amounts, refs, checks, pending = {}, {}, [], []
    def calc(key, value, formula, dependencies):
        value = format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f") if value is not None else None
        amounts[key] = value
        if value is not None:
            ref = f"계산-{prefix}-{len(refs) + 1}"
            registry[ref] = {"kind": "calculation", "record_id": rid, "revision": rev, "rule_version": RULE,
                "formula": formula, "input_refs": [paths[x] for x in dependencies], "unit": "원", "value": value}
            refs[key] = ref
        return Decimal(value) if value is not None else None
    def dec(key): return Decimal(v[key]) if v[key] is not None else None
    with localcontext() as ctx:
        ctx.prec = 60
        sales = v["sales"]
        rows_ready = bool(sales) and all(x["weight_kg"] is not None and x["price_per_kg"] is not None for x in sales)
        rows_deps = [f"sales.{x['id']}.{f}" for x in sales for f in ["weight_kg", "price_per_kg"]]
        row_gross = calc("row_gross_krw", sum((Decimal(x["weight_kg"]) * Decimal(x["price_per_kg"]) for x in sales), Decimal(0)) if rows_ready else None,
                         "Σ(판매중량kg × 단가원/kg), 합계 후 0.01원 반올림", rows_deps)
        if sales and not rows_ready: pending.append("판매 행의 중량·단가 미확인")
        gross = dec("gross_krw")
        gross_deps = ["gross_krw"]
        if gross is None:
            gross, gross_deps = row_gross, rows_deps
        elif row_gross is not None and gross != row_gross:
            checks.append("정산서 매출과 판매 행 합계 불일치")
        calc("gross_krw", gross, "입력 매출 또는 판매 행 합계", gross_deps)
        for key, flag, out in [("deductions", "deductions_complete", "deductions_krw"), ("external_costs", "external_complete", "external_krw")]:
            deps = [f"{key}.{x['id']}.amount_krw" for x in v[key]]
            known = sum((Decimal(x["amount_krw"]) for x in v[key] if x["amount_krw"] is not None), Decimal(0))
            calc(out + "_known", known if any(x['amount_krw'] is not None for x in v[key]) or (not v[key] and v[flag]) else None, "입력된 비용만 합계, 전체 비용 확정 아님", deps + [flag])
            complete = v[flag] and all(x["amount_krw"] is not None for x in v[key])
            amounts[out] = format(known, "f") if complete else None
            if not complete: pending.append(FLAGS[flag] + " 필요")
        d = Decimal(amounts["deductions_krw"]) if amounts["deductions_krw"] is not None else None
        e = Decimal(amounts["external_krw"]) if amounts["external_krw"] is not None else None
        ddeps = ["deductions_complete", *[f"deductions.{x['id']}.amount_krw" for x in v['deductions']]]
        edeps = ["external_complete", *[f"external_costs.{x['id']}.amount_krw" for x in v['external_costs']]]
        over = d is not None and gross is not None and d > gross
        if over: checks.append("공제가 매출을 초과함: 반품·추가 정산 등 첫 버전 미지원 유형 확인")
        expected = calc("expected_net_krw", gross - d if gross is not None and d is not None and not over else None,
                        "G - D (매출 - 정산서 공제)", gross_deps + ddeps)
        n = dec("statement_net_krw")
        calc("statement_delta_krw", n - expected if n is not None and expected is not None else None,
             "N - (G - D), 0이면 입력 정산 항등식 일치", ["statement_net_krw", *gross_deps, *ddeps])
        if amounts["statement_delta_krw"] is not None and Decimal(amounts["statement_delta_krw"]) != 0: checks.append("정산서 지급액과 검산 지급액 불일치")
        shared_ids = {x["id"] for x in v["deductions"]} & {x["id"] for x in v["external_costs"]}
        shared_categories = {x["category"] for x in v["deductions"]} & {x["category"] for x in v["external_costs"]}
        blocked = bool(shared_ids or (shared_categories and not v["overlap_distinct"]))
        if shared_ids: checks.append("정산서/외부 비용에 동일 항목 ID: 이중 차감 확인 필요")
        elif blocked: checks.append("같은 비용 범주의 정산서/외부 항목: 서로 다른 비용인지 확인 필요")
        calc("after_entered_costs_krw", n - e if n is not None and e is not None and not blocked else None,
             "N - E (보고된 정산 지급액 - 확인한 정산 밖 비용), 전체 순이익 아님",
             ["statement_net_krw", "overlap_distinct", *edeps, *[f"deductions.{x['id']}.category" for x in v['deductions']], *[f"external_costs.{x['id']}.category" for x in v['external_costs']]])
        received = dec("received_krw")
        calc("received_delta_krw", received - n if received is not None and n is not None else None,
             "입력 입금액 - 정산서 지급액, 은행 확인 아님", ["received_krw", "statement_net_krw"])
        if amounts["received_delta_krw"] is not None and Decimal(amounts["received_delta_krw"]) != 0: checks.append("입력 입금액과 정산서 지급액 불일치")
        if rows_ready and dec("sold_weight_kg") is not None and sum(Decimal(x["weight_kg"]) for x in sales) != dec("sold_weight_kg"):
            checks.append("판매중량과 판매 행 중량 합계 불일치")
        if dec("initial_weight_kg") is not None and dec("sold_weight_kg") is not None and dec("initial_weight_kg") != dec("sold_weight_kg"):
            checks.append("출하중량과 판매중량 차이: 잔량·폐기·계측 기준 확인, 감모로 자동 분류하지 않음")
    if gross is None: pending.append("매출 또는 완성된 판매 행 입력 필요")
    if n is None: pending.append("정산서 지급액 입력 필요")
    if received is None: pending.append("입금액 미확인: 정산 검산과 별개")
    if v["shipment_date"] is None: pending.append("실행일 미확인")
    if v["input_kind"] == "unknown": pending.append("실제값·가정 구분 미확인")
    return {"status": "CHECK_REQUIRED" if checks else "PARTIAL" if pending else "OK", "analysis_type": "settlement",
        "record_id": rid, "record_revision": rev, "input_kind": v["input_kind"], "amounts": amounts,
        "amount_refs": refs, "citations": registry, "checks": checks, "pending": pending,
        "baseline_ref": {"snapshot_sha256": record["baseline"]["snapshot_sha256"], "data_revision_sha256": record["baseline"]["data_revision_sha256"]},
        "input_digest": digest(v), "imported_not_verified": store.imported, "rule_version": RULE,
        "warnings": ["사용자가 확인한 입력의 검산이며 정산서 원문·은행 입금 진위 확인이 아닙니다.",
                     "정산 지급액과 입력 비용 반영금액은 농가 전체 순이익이 아닙니다.",
                     "공공 경매 시세·과거 출하량을 개인 정산/입금 근거로 사용하지 않았습니다.",
                     "원래 가정과 실제 결과의 금액 대조·보관 효과 추정은 이번 기능에 포함하지 않습니다."]}
