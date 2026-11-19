"""Parse a quoted total and confirm how it applies to each storage duration."""

from dataclasses import dataclass
from decimal import Decimal
import re

from agents.future_pair_intent import _DAY_NAMES, confirmed_total_storage_cost


_PERIOD = rf"(?:\d{{1,3}}\s*일|{'|'.join(_DAY_NAMES)})"
_DURATION = re.compile(rf"({_PERIOD})\s*(?:치|간|동안)?\s*(?:보관(?: 기간)?)?")
_TOTAL = re.compile(
    rf"(?:(?P<period>{_PERIOD})\s*(?:치|간|동안|에)?\s*"
    r"(?:보관(?:에|하면|하는\s*데)?\s*)?)?"
    r"(?P<total>총(?:액)?|합계|전체)?\s*(?:은|는|이|가)?\s*"
    r"(?P<amount>(?:\d+(?:,\d{3})*(?:\.\d+)?)?\s*(?:만|천)?\s*원)"
)
_METHODS = {
    "prorated": re.compile(r"(?:일할(?:로(?:\s*계산(?:해|해요|합니다)?)?)?|하루\s*단위(?:로)?|보관\s*일수에\s*비례)"),
    "fixed": re.compile(r"(?:(?:기간\s*내\s*)?정액(?:으로(?:\s*내)?)?|일찍\s*(?:빼도|출하해도)\s*같아(?:요)?)"),
}
_BILL_FIELD = r"(?:보관비|보관료|저장료)(?:\s*청구)?(?:\s*(?:방식|기준))?(?:은|는|을|를)?\s*"
_REVISE = r"(?:바꿔|변경해|수정해|정정해)\s*(?:줘(?:요)?|주세요|주실래요)"
_REPORTED_CORRECTION = re.compile(
    rf"(?:(?:아까|전에|앞서|방금|원래)\s*)?(?:{_BILL_FIELD})?(?:일할|정액)\s*"
    r"(?:(?:이라고|로|으로)\s*(?:말|입력|작성)?했는데|(?:이|가)?\s*(?:아니라|아니고|말고)|에서)"
    rf"\s*(?:실제로는\s*)?(?P<mode>일할|정액)\s*"
    rf"(?:(?:이에요|예요|입니다|이요|이 맞아요|이?래요)|(?:으?로)?\s*(?:{_REVISE}|해\s*주세요))"
)
_MODE_COMMAND = re.compile(
    rf"(?:(?:아까|기존|현재)\s*)?{_BILL_FIELD}(?P<mode>일할|정액)(?:으?로)?\s*"
    rf"(?:{_REVISE}|계산해\s*(?:줘(?:요)?|주세요))"
)
_UNSET_MODE_COMMAND = re.compile(rf"(?:보관비|보관료|저장료)\s*(?:청구\s*)?(?:방식|기준)(?:을|를)?\s*{_REVISE}")
_MODE_HYPOTHESIS = re.compile(
    rf"(?:{_BILL_FIELD})?(?:일할|정액)(?:이?면|이?라면|로\s*가정하면)\s*"
    r"(?:(?:\d{1,2}월\s*\d{1,2}일|\d{1,2}/\d{1,2})(?:은|는|의)?\s*)?"
    r"(?:(?:비용|보관비|보관료|저장료|수취액|금액)(?:은|는|이|가)?\s*)?(?:얼마|어떻게|달라지)"
)


def _quoted_days(period):
    days = _DAY_NAMES.get(period)
    if days is None:
        days = int(period.removesuffix("일").strip())
    if not 1 <= days <= 365:
        raise ValueError("청구 기준 보관 기간을 1일에서 365일 사이로 알려주세요.")
    return days


@dataclass(frozen=True)
class StorageBillDraft:
    total_krw: Decimal
    days: int | None

    @property
    def question(self):
        amount = format(self.total_krw, ",f")
        if "." in amount:
            amount = amount.rstrip("0").rstrip(".")
        if self.days is None:
            return f"총 보관비 {amount}원은 며칠 보관할 때의 금액인가요?"
        return (f"{self.days}일 보관에 총 {amount}원이군요. "
                "일할로 계산하나요, 아니면 일찍 출하해도 같은 정액인가요?")


def stated_storage_bill(text):
    match = _TOTAL.fullmatch(text)
    if not match or not (match["period"] or match["total"]):
        return None
    amount_text = re.sub(r"^(만|천)\s*원$", r"1\1원", match["amount"].strip())
    amount = confirmed_total_storage_cost(amount_text)
    if amount is None:
        return None
    return StorageBillDraft(Decimal(amount), _quoted_days(match["period"]) if match["period"] else None)


def storage_bill_days(text):
    match = _DURATION.fullmatch(text)
    return _quoted_days(match[1]) if match else None


def storage_bill_mode(text):
    return next((mode for mode, pattern in _METHODS.items() if pattern.fullmatch(text)), None)


def storage_mode_correction(text):
    """Match explicit corrections and requests to choose a different billing mode."""
    statement = text.strip().rstrip(".!。 ")
    if match := _REPORTED_CORRECTION.fullmatch(statement) or _MODE_COMMAND.fullmatch(statement):
        return storage_bill_mode(match["mode"])
    if _UNSET_MODE_COMMAND.fullmatch(statement):
        return "choose"
    return None


def asks_storage_mode_hypothetically(text):
    return bool(_MODE_HYPOTHESIS.match(text.strip()))
