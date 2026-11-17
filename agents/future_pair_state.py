"""Session conditions and the current result for two future shipments."""
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
import re

from services.future_pair import (
    compare_future_pair, compare_future_pair_history_reference,
    compare_future_pair_threshold_reference,
    preview_future_pair_history, price_revision, validate_future_pair_inputs,
    HISTORY_FIELDS, INPUT_FIELDS, LABELS, MAX_RATE_PERCENT, OPTION_FIELDS, OPTION_LABELS,
)
from services.economics import decimal
from agents.future_pair_intent import (
    explicit_shipment_quantity, shipment_choice_question, shipment_window_limit, shipment_window_start,
)


COST_QUESTIONS = {
    OPTION_LABELS["daily_storage_cost"]: "감귤 전체의 하루 보관비는 얼마인가요?",
    OPTION_LABELS["other_cost_krw"]: "그 밖에 드는 비용은 얼마인가요?",
    OPTION_LABELS["fee_percent"]: "판매 수수료는 몇 %인가요?",
}
COST_NAMES = dict(zip(COST_QUESTIONS, ("하루 보관비", "별도 비용", "판매 수수료")))
UNKNOWN_SPEC_RECOVERY = (
    "상품 규격이 확인되지 않아 다른 감귤의 과거 가격을 연결하지 않겠습니다. "
    "출하명세나 포장 라벨의 품종·등급·크기·포장·상자중량을 확인하면 자료 후보와 맞춰 볼 수 있어요. "
    "지금 확인하기 어렵다면 ‘자료 없이 손익분기’라고 말씀해 주세요. "
    "나중 출하 가격을 알고 있다면 가정 판매단가를 직접 입력할 수 있어요."
)
UNKNOWN_COST_RECOVERY = (
    "모르는 감모율·비용은 미확인으로 보존합니다. 0으로 간주하지 않습니다. "
    "확인 가능한 조건부터 알려주시거나 가정값을 직접 정해 주세요."
)
AMBIGUOUS_NUMERIC_RECOVERY = (
    "물음표가 붙은 수치는 확정값으로 저장하지 않았어요. "
    "확정값이면 물음표 없이 알려주시고, 가정값이면 가정으로 정해 주세요."
)
_UNKNOWN_INPUT = re.compile(r"모르|몰라|몰랐|확인\s*(?:못|할\s*수\s*없)|아직\s*(?:정해지지\s*않|안\s*나왔)")
_UNKNOWN_LOSS = re.compile(
    r"(?:(?P<side>[AB])\s*안?\s*)?(?:새\s*기간(?:의)?\s*)?"
    r"(?:감모율?|감량률?|얼마나\s*줄(?:지|어드는지))\s*(?:은|는|이|가|을|를)?\s*"
    rf"(?:얼마인지\s*)?(?:(?:아직|잘|정확히|지금은)\s*)*(?:모릅|{_UNKNOWN_INPUT.pattern})",
    re.I,
)
_UNKNOWN_COST = re.compile(r"비용|보관[비료]|저장[료비]|수수료|감모|감량|줄(?:어|지|는)")
_UNKNOWN_SPEC = re.compile(r"품종|등급|크기|포장|상자\s*(?:무게|중량)|도매시장|시장코드|거래\s*방식")
_INPUT_BURDEN = re.compile(
    r"(?:질문|입력|조건|값).{0,20}(?:많|너무|계속)|"
    r"(?:이렇게|너무).{0,20}(?:물어|묻|요구|입력|질문)|"
    r"(?:안\s*쓸|그만\s*(?:할|하|해)|더\s*이상\s*(?:못|안)|답답|귀찮|불편)",
    re.IGNORECASE,
)
HISTORY_STATUSES = {"NEEDS_CLARIFICATION": "NEEDS_INPUT", "INTERVAL_NOT_AVAILABLE": "NO_DATA",
                    "NO_CLEAN_COMPARISON": "NO_DATA", "AMBIGUOUS_CODES": "DATA_ERROR"}
HISTORY_QUESTIONS = {
    "variety": "어떤 품종의 감귤인가요?",
    "grade": "출하할 감귤의 등급은 어떻게 되나요?",
    "size": "출하할 감귤의 크기 표기는 어떻게 되나요?",
    "package": "어떤 포장으로 출하하나요?",
    "package_kg": "포장 한 개에 감귤이 몇 kg 들어가나요?",
    "market_code": "어느 도매시장 자료로 비교할까요?",
    "trade_type": "어떤 거래방식으로 출하하나요?",
}
HISTORY_HELP = {
    "variety": "품종은 출하처에 등록한 품종명이나 포장 라벨에서 확인할 수 있어요.",
    "grade": "등급은 포장 라벨이나 선별·출하 명세에서 확인해 보세요.",
    "size": "크기 표기는 포장 라벨이나 선별·출하 명세에서 확인해 보세요.",
    "package": "출하처에 전달할 때 쓰는 포장 종류를 확인해 보세요.",
    "package_kg": "상자 라벨이나 출하 명세에서 상자 한 개에 담긴 감귤 무게를 확인해 보세요.",
    "market_code": "출하처나 경락 내역에서 도매시장 이름을 확인해 보세요.",
    "trade_type": "출하처에 경매 등 어떤 방식으로 거래하는지 확인해 보세요.",
}


def unavailable_input(question, subject):
    clauses = re.split(r"[.!?\n,]+", question)
    return any(_UNKNOWN_INPUT.search(clause) and (subject.search(clause) or re.fullmatch(
        r"\s*(?:아직\s*|잘\s*|그건\s*|그건요\s*)*(?:모르\S*|몰라\S*|확인\s*못\S*)\s*", clause
    )) for clause in clauses)


def is_bare_unknown_reply(question: str) -> bool:
    """Only a plain uncertainty answer; never intercept a compound request."""
    return bool(re.fullmatch(
        r"\s*(?:아직\s*|잘\s*|그건\s*)?(?:몰라\S*|모르\S*|확인\s*못\s*하\S*)[.!?\s]*",
        question,
    ))


def is_input_burden_complaint(question: str) -> bool:
    """Recognize a request to stop a long clarification sequence."""
    return isinstance(question, str) and bool(_INPUT_BURDEN.search(question))


def is_questioned_numeric_reply(question: str) -> bool:
    """A numeric statement ending in a question mark is not a confirmed value."""
    return (isinstance(question, str) and bool(re.search(r"\d", question))
            and question.rstrip().endswith(("?", "？")))


def unavailable_cost(question):
    return unavailable_input(question, _UNKNOWN_COST)


def mentions_unknown_cost(question):
    return any(_UNKNOWN_COST.search(clause) and _UNKNOWN_INPUT.search(clause)
               for clause in re.split(r"[.!?\n,]+", question))


def unavailable_loss_rate(question, option):
    return any(match["side"] is None or match["side"].casefold() == option[-1]
               for match in _UNKNOWN_LOSS.finditer(question))


def unavailable_pair_inputs(result, question):
    if result.get("source_status") == "NEEDS_CLARIFICATION":
        return unavailable_input(question, _UNKNOWN_SPEC)
    return bool(result.get("history_preview") or result.get("comparison_inputs_pending")) and unavailable_cost(question)


def resume_pair_inputs(question):
    return any(phrase in question for phrase in (
        "남은 조건", "빠진 조건", "더 필요한 값", "더 필요한 정보",
        "계속 계산", "정밀 비교", "조건 입력을 계속", "다시 조건 확인",
    ))


def asks_where_to_check(question):
    return any(phrase in question for phrase in ("어디", "어떻게 확인", "확인하면"))


def future_pair_feedback(result, *, pause_costs=False, one_question=False):
    if result.get("ambiguous_input"):
        return AMBIGUOUS_NUMERIC_RECOVERY
    message = result.get("message") or "비교 조건을 확인해 주세요."
    if preview := result.get("history_preview"):
        start, end = preview["base_date_range"]
        lines = [f"**같은 규격의 과거 {preview['interval_days']}일 가격쌍 {preview['clean_pairs']}개를 찾았습니다.**",
                 f"관측 기준일 {start} ~ {end}",
                 " · ".join(f"{label} {Decimal(row['change_pct']):+,.2f}%"
                            for label, row in zip(("최저", "중앙", "최고"), preview["scenarios"]))]
        if preview["scenarios"][0]["option_b_price_per_kg"] is not None:
            prices = [f"{Decimal(row['option_b_price_per_kg']):,.2f}".rstrip("0").rstrip(".")
                      for row in preview["scenarios"]]
            lines.append(f"나중 출하 가정 단가(원/kg) · 최저 {prices[0]} · 중앙 {prices[1]} · 최고 {prices[2]}")
        lines.append("과거 가격 변동을 참고한 가정입니다.")
        if note := preview.get("sample_note"):
            lines.append(note)
        if pause_costs:
            return "\n\n".join([*lines, UNKNOWN_COST_RECOVERY])
        question_for = next_pair_question if one_question else _missing_questions
        next_step = question_for(result["missing_fields"], result.get("storage_days", {}))
        return "\n\n".join([*lines, next_step])
    fields = result.get("missing_fields", [])
    if not fields:
        return message
    if result.get("comparison_inputs_pending"):
        if pause_costs:
            return UNKNOWN_COST_RECOVERY
        return next_pair_question(fields, result.get("storage_days", {}))
    lines = [message, "", "확인할 항목: " + ", ".join(HISTORY_FIELDS.get(field, field) for field in fields)]
    for field, choices in result.get("choices", {}).items():
        if field in HISTORY_FIELDS and choices:
            lines.append(f"- {HISTORY_FIELDS[field]} 후보: " + ", ".join(str(choice) for choice in choices))
    return "\n".join(lines)


def paused_pair_feedback(result):
    """Acknowledge input fatigue and finish without another clarification."""
    lines = ["맞아요. 확인할 조건을 너무 많이 물었어요. 여기서 입력 요청을 멈출게요."]
    preview = result.get("history_preview") if isinstance(result, dict) else None
    if preview:
        start, end = preview["base_date_range"]
        lines += [
            "이미 확인한 과거 자료만 요약하면:",
            f"- 같은 규격의 {preview['interval_days']}일 가격쌍 {preview['clean_pairs']}건 · 관측 기준일 {start}~{end}",
            "- B안 가격 변화(과거 사례): " + " · ".join(
                f"{label} {Decimal(row['change_pct']):+,.2f}%"
                for label, row in zip(("낮은 사례", "중앙 사례", "높은 사례"), preview["scenarios"])
            ),
            "이 값은 과거 변동 범위이며 미래가격 예측은 아닙니다.",
        ]
    if result.get("source_status") == "NEEDS_CLARIFICATION":
        reason = "거래규격이 확인되지 않아 다른 상품의 가격을 대신 끌어오지 않았습니다."
    else:
        reason = "일부 가격·감모·비용 조건이 확인되지 않았고, 과거 변동만으로 미래가격을 단정할 수도 없습니다."
    lines += [
        "현재 정보만으로는 두 출하일 중 어느 쪽이 더 이익인지 결론낼 수 없습니다. "
        f"{reason} 이미 알려주신 조건은 유지하고, 더 입력을 요구하지 않을게요."
    ]
    return "\n\n".join(lines)


def history_clarification(result, question="", *, pause_specs=False, one_question=False):
    fields = [field for field in result.get("missing_fields", ()) if field in HISTORY_QUESTIONS]
    if not fields:
        return None
    asks_for_help = asks_where_to_check(question)
    if len(fields) > 1 and not (pause_specs or one_question or unavailable_input(question, _UNKNOWN_SPEC)):
        lines = ["가격자료를 맞춰 보려면 출하명세나 포장 라벨의 거래규격이 필요해요. 확인되는 항목을 한 번에 알려주세요."]
        if asks_for_help:
            lines.append("품종·등급·크기·포장·상자중량은 포장 라벨이나 출하명세에서, 시장·거래방식은 출하처나 경락 내역에서 확인할 수 있어요.")
        for field in fields:
            choices = [choice for choice in result.get("choices", {}).get(field, [])
                       if choice not in {"미상", ".", "-"}][:4]
            suffix = f" · 자료 후보: {', '.join(choices)}" if choices else ""
            lines.append(f"- {HISTORY_FIELDS[field]}{suffix}")
        lines.append("모르는 항목은 ‘모름’이라고 하셔도 됩니다. 거래규격이 확인되지 않으면 가격자료를 억지로 연결하지 않고 손익분기 조건부터 보여드릴게요.")
        return "\n".join(lines)
    first = fields[0]
    mentioned = [field for field in fields if HISTORY_FIELDS[field] in question]
    if "package_kg" in fields and "package_kg" not in mentioned and ("상자 무게" in question or "상자 중량" in question):
        mentioned.append("package_kg")
    if pause_specs or unavailable_input(question, _UNKNOWN_SPEC):
        guidance = " ".join(HISTORY_HELP[field] for field in (mentioned or [first])) if asks_for_help else ""
        return f"{UNKNOWN_SPEC_RECOVERY}\n\n{guidance}" if guidance else UNKNOWN_SPEC_RECOVERY
    if asks_for_help:
        guidance = " ".join(HISTORY_HELP[field] for field in (mentioned or [first]))
        if next_field := next((field for field in fields if field not in (mentioned or [first])), None):
            return f"{guidance}\n\n{HISTORY_QUESTIONS[next_field]}"
        return f"{guidance}\n\n두 출하일의 가정 단가를 알고 있다면 그 가격으로 비교할 수 있어요."
    choices = [choice for choice in result.get("choices", {}).get(first, [])
               if choice not in {"미상", ".", "-"}][:3]
    examples = f"\n\n자료에 있는 예시 · {', '.join(choices)}" if choices else ""
    return HISTORY_QUESTIONS[first] + examples



# The same field-selection rule controls both the displayed question and the
# short-answer interpreter. No parallel list of "what was asked" to drift.
PAIR_QUESTION_PRIORITY = (
    LABELS["start_date"], LABELS["quantity"],
    "A안 보관일", "B안 보관일", LABELS["price_a"], LABELS["price_basis"],
    "A안 기간 전체 감모율(%)", "B안 기간 전체 감모율(%)",
    "A안 수수료율(%)", "B안 수수료율(%)",
    "A안 전체 물량 하루 보관비(원)", "B안 전체 물량 하루 보관비(원)",
    "A안 별도 비용(원)", "B안 별도 비용(원)", LABELS["price_b"],
)


def next_pair_field(fields):
    """Canonical label of the pending input, shared by UI response and router."""
    return next((label for label in PAIR_QUESTION_PRIORITY if label in fields),
                fields[0] if fields else None)


_SIMPLE_NUM = r"(?:0|[1-9][0-9]*(?:,[0-9]{3})*)(?:\.[0-9]+)?"
_SIMPLE_REPLY_SUFFIX = re.compile(r"(?:이에요|예요|입니다|이요|요)$")


def shipment_variety_reply(question, varieties):
    text = re.sub(r"(?:에요|인데요)$", "", question.strip().rstrip(".!。 "))
    text = _SIMPLE_REPLY_SUFFIX.sub("", text).strip()
    text = re.sub(r"^(?:재배\s*)?품종\s*(?:은|는|이|가)?\s*", "", text)
    return text if text in varieties else None
_SIMPLE_SIDE = re.compile(
    r"^(?:(A|B)\s*안|(?:(먼저|나중)\s*(?:출하(?:하는)?|팔(?:때)?)))"
    r"\s*(?:의\s*)?(?:판매\s*)?(?:단가|가격)?\s*(?:은|는|이|가)?\s*", re.I,
)


def _simple_slot_patch(field, question):
    """Parse an unambiguous answer to the single field the app actually asked.

    No amount is inferred from arbitrary prose: no ranges, quotations,
    conflicting numbers, per-box prices, or unrequested historical evidence.
    All resulting values still go through FuturePairState.apply/_validate.
    """
    if not isinstance(question, str) or not isinstance(field, str):
        return None
    # Keep question marks: "5%?" is tentative, not a confirmed 5% input.
    text = question.strip().rstrip(".!。 ").strip()
    text = _SIMPLE_REPLY_SUFFIX.sub("", text).strip()
    side = None
    if found := _SIMPLE_SIDE.match(text):
        side = (found[1] or ("A" if found[2] == "먼저" else "B")).upper()
        text = text[found.end():].strip()
    expected_side = "A" if field.startswith("A안") else "B" if field.startswith("B안") else None
    if side and expected_side and side != expected_side:
        return None
    if side and expected_side is None:
        return None

    if field == LABELS["price_basis"]:
        if re.fullmatch(r"(?:수수료\s*(?:를\s*)?)?(?:차감|공제|빼기)\s*전", text):
            return {"price_basis": "gross"}
        if re.fullmatch(r"(?:수수료\s*(?:를\s*)?)?(?:차감|공제|빼기)\s*(?:후|뒤)", text):
            return {"price_basis": "commission_net"}
        return None

    if field == LABELS["start_date"]:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return {"start_date": text}
        return None

    text = re.sub(r"^(?:kg|킬로그램)\s*당\s*", "", text, flags=re.I)
    kind = None
    if field in (LABELS["price_a"], LABELS["price_b"]):
        kind = "price"
        match = re.fullmatch(rf"({_SIMPLE_NUM})\s*(?:원(?:\s*/\s*(?:kg|킬로그램))?)?", text, re.I)
    elif field == LABELS["quantity"]:
        kind = "quantity"
        match = re.fullmatch(rf"({_SIMPLE_NUM})\s*(kg|킬로그램|톤|t)?", text, re.I)
    elif field.endswith("보관일"):
        kind = "days"
        match = re.fullmatch(rf"({_SIMPLE_NUM})\s*일", text)
    elif "감모율" in field or "수수료율" in field:
        kind = "percent"
        match = re.fullmatch(rf"({_SIMPLE_NUM})\s*(?:%|퍼센트)?", text)
    elif "보관비" in field or "별도 비용" in field:
        kind = "cost"
        match = re.fullmatch(rf"({_SIMPLE_NUM})\s*원?", text)
    else:
        return None
    if match is None:
        return None
    value = Decimal(match[1].replace(",", ""))
    if kind == "quantity" and (match[2] or "").casefold() in {"톤", "t"}:
        value *= 1000
    if kind == "days":
        if value != value.to_integral_value():
            return None
        value = int(value)
    elif kind == "price" and value == 0 and field == LABELS["price_a"]:
        return None
    elif value < 0:
        return None
    if not isinstance(value, int):
        value = format(value, "f")
    if field in LABELS.values():
        key = next(k for k, label in LABELS.items() if label == field)
        return {key: value}
    which = "option_a" if field.startswith("A안") else "option_b"
    key = next((k for k, label in OPTION_LABELS.items() if label in field), None)
    return {which: {key: value}} if key else None

def next_pair_question(fields, storage_days):
    """Single missing decision for a manual A/B comparison, without guessing."""
    target = next_pair_field(fields)
    if target is None:
        return "확인된 조건을 유지하겠습니다."
    prompts = {
        LABELS["start_date"]: "감귤을 보관하기 시작하는 날이 언제인가요?",
        LABELS["quantity"]: "비교할 감귤은 몇 kg인가요? 톤으로 알려주셔도 됩니다.",
        "A안 보관일": "먼저 출하하는 A안은 며칠 보관하시나요?",
        "B안 보관일": "나중 출하하는 B안은 며칠 보관하시나요?",
        LABELS["price_a"]: "먼저 출하하는 A안의 판매단가를 kg당 얼마로 가정할까요?",
        LABELS["price_basis"]: "A안 판매단가는 수수료 차감 전인가요, 차감 후인가요?",
        LABELS["price_b"]: "나중 출하하는 B안의 가정 판매단가를 아시나요? 모르면 미확인으로 유지하겠습니다.",
    }
    if target in prompts:
        return prompts[target]
    side = "먼저 출하하는 A안" if target.startswith("A안") else "나중 출하하는 B안"
    days = storage_days.get("A안" if target.startswith("A안") else "B안")
    if "감모율" in target:
        return f"{side}의 {days}일 보관기간 전체 감모율은 몇 %인가요?" if days else f"{side}의 전체 감모율은 몇 %인가요?"
    if "보관비" in target:
        return f"{side}의 전체 물량 하루 보관비는 얼마인가요?"
    if "수수료율" in target:
        return f"{side}의 판매 수수료는 몇 %인가요?"
    if "별도 비용" in target:
        return f"{side}의 별도 비용은 얼마인가요?"
    return f"{target}을 알려주세요."


def _missing_questions(fields, storage_days):
    labels = {"A안": "먼저 출하", "B안": "나중 출하"}
    common_costs = [label for label in COST_QUESTIONS
                    if f"A안 {label}" in fields and f"B안 {label}" in fields]
    questions = []
    for field in fields:
        side, _, label = field.partition(" ")
        if label in common_costs:
            continue
        elif label == OPTION_LABELS["loss_percent"] and (days := storage_days.get(side)):
            questions.append(f"{labels[side]} · {days}일 보관하는 동안 판매 가능한 양이 몇 % 줄어드나요?")
        elif label in COST_QUESTIONS and side in labels:
            questions.append(f"{labels[side]} · {COST_QUESTIONS[label]}")
        elif field == LABELS["price_basis"] and LABELS["price_a"] not in fields:
            questions.append("먼저 출하 단가는 판매 수수료를 빼기 전인가요, 뺀 뒤인가요?")
        else:
            questions.append(f"{labels[side]} · {label}" if side in labels else field)
    if len(common_costs) == 1:
        questions.append(COST_QUESTIONS[common_costs[0]])
    elif common_costs:
        names = "·".join(COST_NAMES[label] for label in common_costs)
        questions.append(f"두 출하일에 드는 비용을 알려주세요 ({names}).")
    if len(questions) == 1 and questions[0].endswith("?"):
        text = questions[0]
    else:
        text = "\n".join(["**아직 확인할 조건**", "", *(f"- {question}" for question in questions)])
    if common_costs:
        text += "\n\n두 안의 비용이 같으면 한 번만 알려주세요. 다른 항목만 먼저·나중을 나눠 적어 주세요."
    return text


@dataclass(frozen=True)
class _ShipmentWindow:
    first: date
    version: int
    last: date | None = None
    quantity: float | None = None
    awaiting_shipment_variety: bool = False


class FuturePairState:
    def __init__(self):
        self.version = 0
        self.inputs = self._empty()
        self.result = None
        self.source_revision = None
        self.pending = None
        self.box_price = None
        self.shipment_window = None
        self.shipment_lookup_version = None
        self.opening_field = None
        self.costs_deferred = False
        self.specs_deferred = False

    @staticmethod
    def _empty():
        return {**dict.fromkeys(INPUT_FIELDS), "option_a": dict.fromkeys(OPTION_FIELDS),
                "option_b": dict.fromkeys(OPTION_FIELDS)}

    def snapshot(self, *, start_new=False):
        return {"version": self.version, "inputs": self._empty() if start_new else deepcopy(self.inputs)}

    def clear_result(self):
        self.result = None
        self.source_revision = None
        self.pending = None
        self.box_price = None
        self.shipment_lookup_version = None
        self.opening_field = None

    def current_result(self):
        if self.source_revision is not None and self.source_revision != price_revision():
            self.clear_result()
        return deepcopy(self.result)

    def replace(self, inputs, *, start_new=False):
        if start_new or inputs != self.inputs:
            self.inputs = deepcopy(inputs)
            self.version += 1
            self.clear_result()

    def reset(self):
        self.replace(self._empty())
        self.clear_result()
        self.shipment_window = None
        self.costs_deferred = False
        self.specs_deferred = False

    def _response(self, status, message, **details):
        return {"analysis_type": "future_pair", "status": status, "message": message,
                "future_pair_version": self.version, **details}

    def _storage_days(self):
        return {f"{side}안": self.inputs[f"option_{side.lower()}"]["delay_days"] for side in ("A", "B")}

    def saved_conditions_line(self):
        from presenters.reporting import _number
        from services.shipment_timing import resolve_pair_dates

        first, later = resolve_pair_dates(self.inputs)
        if not all(value is not None for value in (first, later, self.inputs["quantity"], self.inputs["price_a"])):
            return ""
        dates = [date.fromisoformat(value) for value in (first, later)]
        quantity = _number(self.inputs["quantity"])
        prices = f"A안 {_number(self.inputs['price_a'])}원/kg"
        if self.inputs["price_b"] is not None:
            prices += f" · B안 {_number(self.inputs['price_b'])}원/kg"
        return (f"**저장한 조건** · 출하 {dates[0].month}/{dates[0].day} · {dates[1].month}/{dates[1].day}"
                f" · {quantity}kg · {prices}")

    def calculate(self, *, preview_history=False, on_progress=None):
        self.clear_result()
        historical = self.inputs["history_filters"] is not None
        if not historical and self._threshold_reference_ready(self.inputs):
            result = compare_future_pair_threshold_reference(**{
                key: self.inputs[key] for key in (
                    "quantity", "price_basis", "start_date", "option_a", "option_b", "price_a",
                )
            })
            if result["status"] == "OK":
                result["future_pair_version"] = self.version
                self.result = deepcopy(result)
                return result
            if result["status"] != "NEEDS_INPUT":
                return {**result, "analysis_type": "future_pair", "future_pair_version": self.version}
        if missing := self._missing(self.inputs):
            if preview_history and historical:
                return self._preview_history(missing, on_progress)
            if not historical and self.inputs["price_b"] is None:
                optional_cost_fields = {
                    f"{side}안 {OPTION_LABELS[field]}"
                    for side in ("A", "B") for field in ("daily_storage_cost", "other_cost_krw")
                }
                missing = [field for field in missing if field not in optional_cost_fields]
            return self._response("NEEDS_INPUT", "비교 조건을 확인해 주세요.", missing_fields=missing,
                                  comparison_inputs_pending=preview_history, storage_days=self._storage_days())
        revision = price_revision() if historical else None
        result = compare_future_pair(**self.inputs, on_progress=on_progress)
        if historical and ((revision is None and result["status"] == "OK")
                           or (revision is not None and revision != price_revision())):
            return self._response("DATA_ERROR", "조회 중 가격 자료가 바뀌었습니다. B안 단가를 직접 입력할 수 있습니다.",
                                  manual_price_required=True)
        result = {**result, "analysis_type": "future_pair", "future_pair_version": self.version}
        if result["status"] == "OK":
            self.result = deepcopy(result)
            self.source_revision = revision
        return self._agent_result(result)

    def _preview_history(self, missing, on_progress):
        start = self.inputs["start_date"]
        a, b = (self.inputs[side]["delay_days"] for side in ("option_a", "option_b"))
        date_labels = {LABELS["start_date"], "A안 보관일", "B안 보관일"}
        if date_fields := [field for field in missing if field in date_labels]:
            return self._response("NEEDS_INPUT", "과거 가격을 조회할 날짜부터 알려주세요.", missing_fields=date_fields)
        revision = price_revision()
        if on_progress:
            on_progress("price_history")
        result = preview_future_pair_history(start, a, b, self.inputs["price_a"], self.inputs["history_filters"])
        if result.get("scenarios"):
            if revision is None or revision != price_revision():
                return self._response("DATA_ERROR", "조회 중 가격 자료가 바뀌었습니다. B안 단가를 직접 입력할 수 있습니다.",
                                      manual_price_required=True)
            preview = {key: result[key] for key in ("interval_days", "clean_pairs", "base_date_range",
                                                    "scenarios", "conditions", "source")}
            if result.get("small_sample"):
                # 사례가 10쌍 미만이면 가격 도구가 붙인 안내를 미리보기에도 그대로 전달한다.
                preview["sample_note"] = result["cautions"][0]
            if self._threshold_reference_ready(self.inputs):
                reference = compare_future_pair_history_reference(
                    **{key: self.inputs[key] for key in (
                        "quantity", "price_basis", "start_date", "option_a", "option_b", "price_a",
                    )},
                    history_preview=preview,
                )
                if reference["status"] == "OK":
                    reference["future_pair_version"] = self.version
                    self.result = deepcopy(reference)
                    self.source_revision = revision
                    return reference
            optional_cost_fields = {
                f"{side}안 {OPTION_LABELS[field]}"
                for side in ("A", "B") for field in ("daily_storage_cost", "other_cost_krw")
            }
            missing = [field for field in missing if field not in optional_cost_fields]
            return self._response("NEEDS_INPUT", "과거 가격 근거를 확인했습니다. 손익 계산 조건을 이어서 확인해 주세요.",
                                  source_status="HISTORY_READY", missing_fields=missing, history_preview=preview,
                                  comparison_inputs_pending=True, storage_days=self._storage_days())
        return self._agent_result({**result, "analysis_type": "future_pair", "future_pair_version": self.version})

    @staticmethod
    def _agent_result(result):
        status = result["status"]
        if status == "OK":
            return result
        normalized = HISTORY_STATUSES.get(status, status)
        message = result.get("message")
        if status == "NO_CLEAN_COMPARISON":
            message = ("같은 규격의 과거 기록은 있지만 품질 기준을 통과한 가격쌍이 없어요. "
                       "나중 출하 단가를 직접 입력해 주세요.")
        elif normalized == "NO_DATA":
            message = "해당 기간의 과거 가격쌍이 없습니다. 나중 출하 단가를 직접 입력해 비교할 수 있어요."
        if status == "ERROR":
            normalized = "INVALID_INPUT" if result.get("error_kind") == "INVALID_INPUT" else "DATA_ERROR"
            if normalized == "INVALID_INPUT":
                message = result.get("error")
        if normalized == "DATA_ERROR" and not message:
            message = "과거 가격 자료를 검증하지 못했습니다. B안 단가를 직접 입력할 수 있습니다."
        response = {**{key: result[key] for key in ("analysis_type", "future_pair_version", "missing_fields", "manual_price_required") if key in result},
                    "status": normalized, "source_status": status, "message": message}
        if result.get("choices"):
            response["choices"] = {field: choices[:8] for field, choices in result["choices"].items() if field in HISTORY_FIELDS}
        if normalized in {"DATA_ERROR", "NO_DATA"}:
            response["manual_price_required"] = True
        return response

    @staticmethod
    def _missing(inputs):
        missing = [label for field, label in LABELS.items()
                   if inputs[field] is None and field != "price_b"]
        for key, side in (("option_a", "A"), ("option_b", "B")):
            missing += [f"{side}안 {OPTION_LABELS[field]}" for field in OPTION_FIELDS
                        if inputs[key][field] is None and (field != "fee_percent" or inputs["price_basis"] == "gross")]
        return missing

    @staticmethod
    def _threshold_reference_ready(inputs):
        """Minimum confirmed inputs for a useful conditional break-even result."""
        if inputs["price_b"] is not None:
            return False
        required = ("quantity", "price_basis", "start_date", "price_a")
        if any(inputs[field] is None for field in required):
            return False
        for side in ("option_a", "option_b"):
            option = inputs[side]
            if option["delay_days"] is None or option["loss_percent"] is None:
                return False
            if inputs["price_basis"] == "gross" and option["fee_percent"] is None:
                return False
        return True

    def apply(self, action, expected_version, patch, clear_fields=None, *, on_progress=None,
              box_price=None, unresolved_box_price=False):
        if type(expected_version) is not int or expected_version != self.version:
            return self._response("INVALID_INPUT", "비교 조건 버전이 바뀌었습니다. 최신 조건을 확인해 주세요.")
        if action not in ("new", "edit") or not isinstance(patch, dict):
            return self._response("INVALID_INPUT", "새 비교 또는 조건 수정을 선택해 주세요.")
        candidate = self._empty() if action == "new" else deepcopy(self.inputs)
        try:
            self._apply_patch(candidate, patch, clear_fields)
            validate_future_pair_inputs(candidate)
        except ValueError as exc:
            return self._response("INVALID_INPUT", str(exc))
        self.replace(candidate, start_new=action == "new")
        if action == "new":
            self.costs_deferred = False
            self.specs_deferred = False
        if box_price is not None or unresolved_box_price:
            self.clear_result()
            self.box_price = box_price
            message = box_price.question() if box_price else "어느 출하안의 가격인지와 상자당 금액을 알려주세요."
            result = self._response("NEEDS_INPUT", message, source_status="BOX_PRICE_CONFIRMATION")
            self.pending = deepcopy(result)
            return result
        result = self.calculate(preview_history=True, on_progress=on_progress)
        if result.get("source_status") != "NEEDS_CLARIFICATION":
            self.specs_deferred = False
        if result["status"] in {"NEEDS_INPUT", "NO_DATA"} or result.get("manual_price_required"):
            self.pending = deepcopy(result)
        elif result["status"] == "OK":
            self.costs_deferred = False
        return result


    def waiting_field(self):
        """Field asked in the latest manual A/B clarification, if still pending."""
        pending = self.pending or {}
        if self.opening_field is not None and not (self.costs_deferred or self.specs_deferred):
            return self.opening_field
        if (self.box_price is not None or pending.get("status") != "NEEDS_INPUT"
                or not pending.get("comparison_inputs_pending") or self.costs_deferred or self.specs_deferred):
            return None
        fields = pending.get("missing_fields", ())
        # Historical previews display a list of remaining conditions. Bare values
        # are safe there only if one field is left. Manual comparison asks one.
        if pending.get("history_preview") and len(fields) != 1:
            return None
        return next_pair_field(fields)

    def apply_short_answer(self, question, *, on_progress=None):
        """Atomic local continuation; None means leave interpretation to the model."""
        field = self.waiting_field()
        patch = _simple_slot_patch(field, question) if field else None
        if patch is None:
            return None
        return self.apply("edit", self.version, patch, on_progress=on_progress)

    def apply_explicit_pair_prices(self, question, *, on_progress=None):
        """Use two explicitly labelled kg quotes to continue an active comparison."""
        if (self.pending is None or self.box_price is not None
                or any(option["delay_days"] is None for option in (self.inputs["option_a"], self.inputs["option_b"]))):
            return None
        parts = re.split(r"[,\n]\s*(?=[AB]\s*안)", question.strip(), flags=re.I)
        if len(parts) != 2:
            return None
        prices = {}
        for part in parts:
            side = re.match(r"([AB])\s*안", part, re.I)
            if side is None or "원" not in part:
                return None
            field = f"price_{side[1].lower()}"
            if (price := _simple_slot_patch(LABELS[field], part)) is None:
                return None
            prices.update(price)
        if len(prices) != 2:
            return None
        return self.apply("edit", self.version, {**prices, "price_mode": "manual"}, on_progress=on_progress)

    def cancel_shipment_window(self):
        if self.shipment_window is not None and self.shipment_window.version == self.version:
            self.pending = None
        self.shipment_window = None

    def _shipment_window_lookup(self, window, variety, on_progress):
        from services.shipment_timing import analyze_shipment_timing

        if on_progress:
            on_progress("shipment_csv")
        self.shipment_window = replace(window, awaiting_shipment_variety=False)
        return "", analyze_shipment_timing({
            "shipment_variety": variety, "option_a_date": window.first.isoformat(),
            "option_b_date": window.last.isoformat(),
        })

    def follow_up_shipment_window(self, question, today, *, previous_reply="", shipment_variety=None,
                                  on_progress=None):
        """Use a stated shipping window to obtain a dated, per-kg A quote."""
        stated_limit = None
        if (first := shipment_window_start(question, today)) is not None and 1 <= (first - today).days < 365:
            self.shipment_window = _ShipmentWindow(first, self.version, quantity=explicit_shipment_quantity(question))
            stated_limit = shipment_window_limit(re.split(r"[.!?]", question, maxsplit=1)[0], first)
            if stated_limit is None:
                return f"{first.month}월 {first.day}일 이후에 출하할 수 있군요. 늦어도 언제까지 출하해야 하나요?", None
        window = self.shipment_window
        if window is None or window.version != self.version:
            return None

        first, last = window.first, window.last
        first_label = f"{first.month}월 {first.day}일"
        if last is None:
            last = stated_limit or shipment_window_limit(question, first)
            if last is None:
                return None
            if last <= first:
                return f"{first_label}보다 늦게 출하할 수 있는 날이 있나요?", None
            if (last - today).days > 365:
                return "출하 비교는 보관 시작일로부터 365일 이내에서 할 수 있어요. 더 가까운 날을 알려주시겠어요?", None
            result = self.apply("new", self.version, {
                "start_date": today.isoformat(),
                "quantity": window.quantity,
                "option_a": {"delay_days": (first - today).days},
            })
            if result["status"] != "NEEDS_INPUT":
                return None
            self.shipment_window = replace(window, last=last, version=self.version)
            self.pending = {**result, "missing_fields": [LABELS["price_a"]]}
            return (f"{last.month}월 {last.day}일까지 출하할 수 있군요. "
                    f"{first_label} 출하 단가는 kg당 얼마로 제안받으셨나요?", self.pending)

        last_label = f"{last.month}월 {last.day}일"
        confirmation = f"{first_label}과 {last_label}을 먼저 비교해 볼까요?"
        if window.awaiting_shipment_variety:
            from services.shipment_timing import CATEGORIES

            if variety := shipment_variety_reply(question, CATEGORIES):
                return self._shipment_window_lookup(window, variety, on_progress)

        if self.inputs["price_a"] is None and self.waiting_field() == LABELS["price_a"]:
            text = question.strip()
            text = re.sub(rf"^(?:{first.month}\s*월\s*)?{first.day}\s*일"
                          r"\s*(?:에|출하\s*시)?\s*(?:제시받은\s*)?"
                          r"(?:kg\s*당\s*)?(?:단가\s*(?:=|은|는)?\s*)?", "", text)
            unknown_quote = re.sub(r"^(?:제시받은\s*)?(?:단가|가격)\s*(?:은|는|을|를)?\s*", "", text)
            if is_bare_unknown_reply(unknown_quote):
                if shipment_variety:
                    return self._shipment_window_lookup(window, shipment_variety, on_progress)
                self.shipment_window = replace(window, awaiting_shipment_variety=True)
                return (f"{first_label}과 {last_label}의 과거 같은 시기 출하량부터 볼 수 있어요. "
                        "출하통계에서 어떤 품종을 찾아볼까요?", None)
            result = self.apply_short_answer(text, on_progress=on_progress)
            if result is None:
                return None
            self.shipment_window = replace(window, version=self.version, awaiting_shipment_variety=False)
            price = decimal(self.inputs["price_a"], LABELS["price_a"])
            return (f"{first_label} 제시 단가를 **{price:,.12g}원/kg**으로 저장했어요. " + confirmation, result)

        if self.inputs["price_a"] is None:
            return None
        accepts = re.fullmatch(r"\s*(?:네|예|좋아요|(?:(?:네|예)[,\s]+)?(?:비교)?해\s*(?:줘|주세요))[.!\s]*", question)
        if accepts and previous_reply.endswith(confirmation):
            self.shipment_window = None
            start = date.fromisoformat(self.inputs["start_date"])
            result = self.apply("edit", self.version, {"option_b": {"delay_days": (last - start).days}},
                                on_progress=on_progress)
            return (f"{first_label}과 {last_label}을 비교할게요. "
                    + future_pair_feedback(result), result)
        if shipment_choice_question(question) and "언제" in question:
            price = decimal(self.inputs["price_a"], LABELS["price_a"])
            return (f"{first_label} 제시 단가는 **{price:,.12g}원/kg**이에요. "
                    "나중에 받는 단가와 보관비를 알아야 어느 쪽이 유리한지 판단할 수 있어요. "
                    + confirmation, None)
        return None

    def is_compound_value_answer(self, question):
        """Require an A/B update tool for unmistakable data-only multi-slot replies."""
        pending = self.pending or {}
        if (self.box_price is not None or pending.get("status") != "NEEDS_INPUT"
                or not pending.get("comparison_inputs_pending")):
            return False
        if not re.match(r"\s*(?:[0-9]|[AB]\s*안|총\s*물량|물량|수수료|감모|보관비|별도\s*비용|단가)", question, re.I):
            return False
        if any(word in question for word in (
                "뜻", "의미", "설명", "조회", "찾아", "출하량", "통계", "과거",
                "보고서", "예측", "새 비교", "비교해", "어떻게", "왜", "?", "？",
                "아니면", "혹은", "인지", "일지", "몰라", "모르", "정도", "가량", "대략", "약 ")):
            return False
        return bool(re.search(r"[0-9]", question) and
                    re.search(r"(?:원|%|퍼센트|kg|킬로그램|톤|\b[AB]안\b|감모|수수료|보관비)", question, re.I))

    def follow_up_box_price(self, question, *, on_progress=None):
        if self.box_price is None:
            return None
        action, quote = self.box_price.follow_up(question)
        if action == "confirm":
            field = "price_a" if quote.side == "A" else "price_b"
            price = quote.unit_price
            patch = {field: str(price)}
            if quote.side == "B":
                patch["price_mode"] = "manual"
            result = self.apply("edit", self.version, patch, on_progress=on_progress)
            if result["status"] == "INVALID_INPUT":
                return result
            return {**result, "confirmed_box_price": str(price), "confirmed_box_side": quote.side}
        if action == "cancel":
            self.clear_result()
            self.pending = self._response("NEEDS_INPUT", f"상자 단가 적용을 취소했어요. {quote.side or '비교에'} 사용할 kg당 단가를 알려주세요.")
            return deepcopy(self.pending)
        if action == "clarify":
            self.box_price = quote
            self.pending = self._response("NEEDS_INPUT", quote.question(), source_status="BOX_PRICE_CONFIRMATION")
            return deepcopy(self.pending)
        return None

    @staticmethod
    def _apply_patch(candidate, patch, clear_fields):
        allowed = set(LABELS) | {"option_a", "option_b", "price_mode", "history_filters"}
        if set(patch) - allowed:
            raise ValueError("허용되지 않은 비교 조건입니다.")
        if clear_fields is None:
            clear_fields = []
        if not isinstance(clear_fields, list) or any(not isinstance(field, str) for field in clear_fields):
            raise ValueError("지울 항목은 이름 목록으로 전달해 주세요.")
        mode = patch.get("price_mode")
        filters = patch.get("history_filters")
        if mode not in (None, "manual", "history"):
            raise ValueError("B안 단가의 입력 방식을 확인해 주세요.")
        if filters is not None:
            if not isinstance(filters, dict) or set(filters) - set(HISTORY_FIELDS):
                raise ValueError("과거 가격 거래규격을 확인해 주세요.")
            if any(value is not None and not isinstance(value, str) for value in filters.values()):
                raise ValueError("과거 가격 거래규격은 글자로 입력해 주세요.")
            filters = {key: value.strip() for key, value in filters.items() if value and value.strip()}
        manual_price = patch.get("price_b") is not None
        if (manual_price and (mode == "history" or filters)) or (mode == "manual" and filters):
            raise ValueError("B안 단가는 직접 입력과 과거 변동 가정 중 하나를 선택해 주세요.")
        history = mode == "history" or (mode is None and not manual_price and candidate["history_filters"] is not None)
        if filters and not history:
            raise ValueError("거래규격으로 비교하려면 과거 변동 가정을 선택해 주세요.")
        candidate["history_filters"] = (candidate["history_filters"] or {}) if history else None
        if history:
            candidate["price_b"] = None
        for field in clear_fields:
            side, _, name = field.partition(".")
            if field in LABELS:
                candidate[field] = None
            elif side in ("option_a", "option_b") and name in OPTION_FIELDS:
                candidate[side][name] = None
                if name == "delay_days":
                    candidate[side]["loss_percent"] = None
            elif mode == "manual" and field == "history_filters":
                # Switching to a manual price already removed the history filters.
                continue
            elif side == "history_filters" and name in HISTORY_FIELDS and (history or mode == "manual"):
                if history:
                    candidate["history_filters"].pop(name, None)
            else:
                raise ValueError("지울 비교 항목을 확인해 주세요.")
        for field in LABELS:
            if patch.get(field) is not None:
                candidate[field] = patch[field]
        for side in ("option_a", "option_b"):
            updates = patch.get(side)
            if updates is None:
                continue
            if not isinstance(updates, dict) or set(updates) - {*OPTION_FIELDS, "loss_confirmed_for_period"}:
                raise ValueError("A·B안 보관 조건을 확인해 주세요.")
            option = candidate[side]
            updates = dict(updates)
            confirmed = updates.pop("loss_confirmed_for_period", False)
            if type(confirmed) is not bool:
                raise ValueError("새 기간의 감모율 확인 여부를 알려 주세요.")
            if updates.get("delay_days") is not None and updates["delay_days"] != option["delay_days"] and option["delay_days"] is not None:
                option["loss_percent"] = None
                if not confirmed:
                    updates.pop("loss_percent", None)
            option.update({key: value for key, value in updates.items() if value is not None})
        if filters:
            candidate["history_filters"].update(filters)
