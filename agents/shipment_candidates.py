"""Recognize explicit candidate dates and keep their common inputs separate from A/B."""
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
import re
from typing import Literal
from uuid import uuid4

from agents.box_price import BoxPrice, mentions_box_price, standalone_box_price
from agents.candidate_storage_billing import (
    StorageBillDraft, stated_storage_bill, storage_bill_days, storage_bill_mode, storage_mode_correction,
)
from agents.future_pair_intent import (
    confirmed_total_storage_cost, explicit_shipment_quantity, shipment_availability, shipment_choice_question,
    without_shipment_exclusions,
)
from services.economics import decimal
from services.shipment_candidates import compare_shipment_candidates


_DATE = re.compile(
    r"(?<![\d./-])(?:(?P<iso>\d{4}-\d{2}-\d{2})|"
    r"(?:(?P<year>\d{4})\s*년\s*)?(?:(?P<month>\d{1,2})\s*(?:월|/)\s*)?"
    r"(?P<day>\d{1,2})\s*(?P<unit>일)?)(?![\d./-])"
)
_EXPLICIT = re.compile(r"(?:출하\s*)?후보(?:일|\s*날짜)|(?:출하일|날짜)\s*(?:두세|2\s*[~～-]\s*3|세|3)\s*개|세\s*날짜")
_STORAGE_TAIL = re.compile(r"^\s*(?:(?:부터|에)\s*보관|보관\s*시작)")
_STORAGE_BEFORE = re.compile(r"(?:보관\s*시작(?:일)?|입고일)\s*(?:은|는|이|을|[:：])?\s*$")
_PERIOD_TAIL = re.compile(r"^\s*(?:뒤|후|동안|치|부터|이후|까지|[~～–])")
_SLASH_DATE = re.compile(r"(?<![\d/])([12]?\d)/(\d{1,2})(?:\s*일)?(?![\d/])")
_DECLINED = re.compile(r"(?:비교|계산)(?:는|을)?\s*(?:하지\s*(?:말|마|않)|안\s*해)")
_QUOTE_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d{1,12})(?:\.\d{1,2})?"
_QUOTE_LEAD = re.compile(r"^\s*(?:에는|에|은|는|의)?\s*(?:(?:출하|판매)\s*)?(?:견적|단가)?\s*(?:는|가|를)?\s*")
_QUOTE_ACCEPT = (
    r"(?:으?로\s*)?(?:받기로\s*(?:했어요|했습니다|정했어요)|확정(?:됐어요|했어요|했습니다)|"
    r"(?:반영|수정|변경|저장)해\s*(?:줘(?:요)?|주세요)|바꿔\s*(?:줘(?:요)?|주세요))"
)
_QUOTE_STATEMENT = re.compile(
    rf"(?:(?:kg|킬로(?:그램)?)\s*당\s*(?P<before>{_QUOTE_NUMBER})\s*원|"
    rf"(?P<after>{_QUOTE_NUMBER})\s*원\s*/\s*kg)\s*"
    rf"(?:{_QUOTE_ACCEPT}|이에요|예요|입니다|이요)?[.!。\s]*", re.I,
)
_BOX_ACCEPT_TAIL = re.compile(rf"{_QUOTE_ACCEPT}[.!。\s]*$")
_UNKNOWN_WEIGHT = (r"(?:실제\s*)?(?:상자\s*)?(?:(?:무게|중량)(?:는|를|가)?\s*)?"
                   r"(?:잘\s*)?(?:몰라요|모르겠어요|모릅니다|몰라)")
_UNKNOWN_BOX_WEIGHT_TAIL = re.compile(rf"(?:인데|이고)?[,\s]+{_UNKNOWN_WEIGHT}[.!。\s]*$")
_QUOTE_BASIS = re.compile(
    r"수수료\s*(?:를\s*)?(?:(?P<gross>(?:(?:차감|공제|제외)(?:하기\s*)?|빼기|떼기)\s*전|포함(?:한)?)"
    r"|(?P<commission_net>(?:(?:차감|공제|제외)(?:한\s*)?|빼기|떼기)\s*(?:후|뒤)|뺀(?:\s*(?:후|뒤))?|"
    r"뗀(?:\s*(?:후|뒤))?|빼고|떼고|빠진|제외한)|(?P<unknown>기준(?:은|을)?\s*(?:잘\s*)?(?:몰라요|모르겠어요)))"
    r"(?:\s*(?:금액|단가|가격|기준))?(?:으?로|이고|인데|이에요|입니다)?"
)
_BARE_QUOTE = re.compile(rf"(?P<before>{_QUOTE_NUMBER})\s*원\s*(?:이에요|예요|입니다|이요)?[.!。\s]*")
_CANCEL_QUOTE = re.compile(
    r"(?:(?:(?:그|이|방금|아까|이전)\s*)?(?:견적|단가)(?:\s*입력)?(?:은|는|을|를)?\s*)?"
    r"(?:아니요|아니에요|아닙니다|취소(?:할게요|할래요|합니다|해\s*(?:줘(?:요)?|주세요))?"
    r"|(?:그냥|그대로)\s*(?:둘게요|둬\s*(?:줘|주세요)))[.!。\s]*"
)
_QUOTE_REDIRECT = re.compile(r"^(?:아니(?:요)?|그럼|그러면|이번에는|이번엔|아)[,\s]+")
_QUOTE_DATE_ONLY = re.compile(
    r"\s*(?:이요|요|이에요|예요|입니다)?\s*"
    r"(?:(?:은|는|에|으?로|의)?\s*(?:견적|단가)(?:은|는|을|를|으?로)?\s*)?"
    r"(?:으?로\s*)?(?:볼게요|바꿀게요|해주세요|해\s*줘(?:요)?|바꿔\s*(?:줘(?:요)?|주세요))?[.!?？。\s]*"
)
_MENTIONS_QUOTE_AMOUNT = re.compile(r"\d[\d,.]*\s*(?:만\s*)?원")
_OTHER_QUOTE_CONTEXT = re.compile(r"\b[AB]\s*안|과거|경락|출하량|손익분기|정산", re.I)
_COMMON_FIELDS = ("start", "quantity", "basis", "daily_storage", "other_cost", "fee")
_QUESTIONS = {
    "start": ("보관 시작일", "보관을 시작하는 날짜가 언제인가요?"),
    "basis": ("수수료 기준", "입력한 단가는 판매수수료를 빼기 전인가요, 뺀 뒤인가요?"),
    "fee": ("판매 수수료율", "판매 수수료율이 몇 %인가요?"),
    "daily_storage": ("하루 보관비", "전체 물량을 하루 보관하는 데 얼마가 드나요?"),
    "other_cost": ("별도 비용", "보관비 외 따로 드는 비용은 얼마인가요?"),
}
_CONDITION_PREFIX = {
    "start": r"보관\s*시작(?:일|\s*날짜)?", "basis": r"(?:판매\s*)?수수료\s*기준",
    "fee": r"(?:판매\s*)?수수료(?:율)?", "daily_storage": r"보관비|보관료|저장료",
    "other_cost": r"(?:별도|기타)\s*비용", "loss": r"(?:기간\s*전체\s*)?(?:감모율?|감량률?)",
}
_UNKNOWN_CONDITION = re.compile(r"(?:(?:아직|잘|정확히|그건|지금은)\s*)*(?:몰라(?:요)?|모르(?:겠어요|겠어|겠습니다|는\s*데요)|모릅니다|미확인)")
_ANSWER_END = re.compile(r"\s*(?:이에요|예요|입니다|이요|이죠|요|들어요|부터)\s*$")
_TIMING_REQUEST = re.compile(
    r"(?:과거\s*(?:신고\s*)?출하량|#\s*855)(?:을|를|도)?\s*(?:먼저\s*)?"
    r"(?:비교|조회|확인|보여|볼|보기|봐)(?!\s*(?:하지|말고))"
)
_NO_QUOTE = re.compile(
    r"(?:아직\s*)?(?:받은\s*)?(?:견적|단가|가격)(?:은|는|이|가|을|를)?\s*"
    r"(?:(?:아직|잘|하나도)\s*)*(?:없어요|없습니다|몰라요|모르겠어요|못\s*받았어요|안\s*받았어요)[.!。\s]*"
)
_CHECK_STORAGE_COST = "저장고 견적서에서 전체 물량의 하루 보관비를 확인해 주세요."
_RESUME_STORAGE_COST = "이 대화에서 하루 보관비만 알려주시면 이어서 비교할게요."
_DAILY_COST_PREFIX = re.compile(r"^(?:하루\s*(?:에|당)?|일일|1\s*일\s*(?:에|당)|일당)\s*")
_DAILY_COST_SUFFIX = re.compile(r"\s*/\s*(?:1\s*)?일$")
_OTHER_STORAGE_UNIT = re.compile(
    r"^(?:총(?:액)?|합계|월(?:간|당)?|(?:한\s*)?달(?:에|당)|(?:상자|박스|kg|킬로(?:그램)?)\s*당)"
    r"|\d+\s*(?:일|개월|달)\s*(?:치|에|간|동안)|/\s*(?:달|월|상자|박스|kg)\b", re.I,
)
_LOSS_WEIGHT = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d{1,12})(?:\.\d{1,6})?\s*(?:kg|킬로(?:그램)?|톤|t)"
_SALEABLE_ANSWER = re.compile(
    rf"(?:(?P<total>{_LOSS_WEIGHT})\s*(?:중에서|중|가운데)\s*)?"
    rf"(?:판매\s*가능(?:한)?\s*(?:물량|중량|무게|양)\s*(?:은|는|이|가)?\s*)?"
    rf"(?P<saleable>{_LOSS_WEIGHT})(?:\s*(?:은|는|이|가|만|을|를))?"
    r"(?:\s*(?:팔\s*수\s*있(?:어요|습니다)?|(?:판매|출하)\s*(?:가능(?:해요|합니다)?|"
    r"할\s*수\s*있(?:어요|습니다)?)|남(?:아요|습니다)))?",
    re.I,
)
_LOSS_PERCENT = re.compile(
    rf"(?:(?:{_CONDITION_PREFIX['loss']})(?:은|는|이|가)?\s*)?({_QUOTE_NUMBER})\s*(?:%|퍼센트)"
)
_LOSS_ANSWER_SPLIT = re.compile(r"[;；\n]|(?<!\d),|,\s+|\s+그리고\s+")


def _candidate_weight_kg(weight):
    unit = re.search(r"kg|킬로(?:그램)?|톤|t", weight, re.I)
    amount = decimal(weight[:unit.start()].strip().replace(",", ""), "판매 물량")
    return amount * 1000 if unit.group().casefold() in {"톤", "t"} else amount


def _keep_unchanged_number(previous, entered):
    # Number inputs return floats even when a stored Decimal was never edited.
    if previous is not None and isinstance(entered, float) and float(previous) == entered:
        return previous
    return entered


@dataclass(frozen=True)
class CandidateCondition:
    field: str
    day: date | None
    question: str = field(compare=False)

    @property
    def label(self):
        if self.field == "loss":
            return f"{self.day.month}월 {self.day.day}일 감모율"
        return _QUESTIONS[self.field][0]


def _quote_terms(text, *, allow_bare=False):
    bases = {match.lastgroup for match in _QUOTE_BASIS.finditer(text)}
    basis = "unknown" if len(bases) > 1 else next(iter(bases), None)
    clean = _QUOTE_LEAD.sub("", text, count=1)
    clean = _QUOTE_BASIS.sub("", clean).strip(" ,·")
    unit = _QUOTE_STATEMENT.fullmatch(clean)
    if allow_bare and not unit:
        unit = _BARE_QUOTE.fullmatch(clean)
    if unit:
        amount = unit.groupdict().get("before") or unit["after"]
        return basis, Decimal(amount.replace(",", ""))
    unknown_weight = _UNKNOWN_BOX_WEIGHT_TAIL.search(clean)
    if unknown_weight:
        clean = clean[:unknown_weight.start()]
    box = standalone_box_price(_BOX_ACCEPT_TAIL.sub("", clean))
    return basis, replace(box, weight=None) if box is not None and unknown_weight else box


@dataclass(frozen=True)
class _PendingCandidateQuote:
    day: date
    box: BoxPrice | None = None
    required_basis: str | None = None

    def question(self):
        label = f"{self.day.month}월 {self.day.day}일"
        if self.required_basis:
            basis = "수수료를 빼기 전" if self.required_basis == "gross" else "수수료를 뺀 뒤"
            return (f"비교에는 {basis} 단가를 쓰고 있어요. "
                    f"{label} 견적도 같은 기준으로 kg당 얼마인지 알려주시겠어요?")
        if self.box is None:
            return f"{label}에 받은 견적은 kg당 얼마인가요?"
        return f"{label} 견적에서 실제 판매할 상자 한 개에 감귤이 몇 kg 들어가나요?"


def _calendar_text(question):
    return _SLASH_DATE.sub(r"\1월 \2일", question)


def _stated_timing_variety(question):
    from services.shipment_timing import CATEGORIES

    labels = [name for name in CATEGORIES if name in question]
    return labels[0] if len(labels) == 1 and not re.search(r"말고|아닌|또는|혹은|인지|헷갈|아니에요", question) else None


def requested_candidate_dates(question, today):
    """Return explicitly listed dates and an explicitly stated storage start."""
    if not shipment_choice_question(question) or _DECLINED.search(question):
        return None
    question = _calendar_text(question)
    explicit = bool(_EXPLICIT.search(question))
    _, excluded = shipment_availability(question, today)
    question = without_shipment_exclusions(question)
    anchor = today
    has_month = False
    dates = set()
    storage_start = None
    for match in _DATE.finditer(question):
        try:
            if match["iso"]:
                day = date.fromisoformat(match["iso"])
            elif match["month"] or (has_month and match["unit"]):
                month = int(match["month"] or anchor.month)
                year = int(match["year"] or (anchor.year + (anchor.month == 12 and month == 1)))
                day = date(year, month, int(match["day"]))
            else:
                continue
        except ValueError:
            continue
        if match["iso"] or match["month"]:
            anchor, has_month = day, True
        tail = question[match.end():]
        if _STORAGE_TAIL.match(tail) or _STORAGE_BEFORE.search(question[:match.start()]):
            storage_start = day
        elif not _PERIOD_TAIL.match(tail):
            dates.add(day)
    return (tuple(sorted(dates)), storage_start) if explicit or len(dates | excluded) >= 3 else None


@dataclass
class ShipmentCandidateDraft:
    dates: tuple[date, ...]
    window: tuple[date, date] | None
    unavailable: set[date]
    pair_version: int
    scenario_version: int
    opened_on: date
    common: dict = field(default_factory=dict)
    by_date: dict = field(default_factory=dict)
    form_key: str = field(default_factory=lambda: uuid4().hex)
    revision: int = 0
    result: dict | None = None
    reference_date: date | None = None
    pending_quote: _PendingCandidateQuote | None = None
    gross_fee: float | None = None
    initial_inputs_saved: bool = False
    deferred_conditions: set[CandidateCondition] = field(default_factory=set)
    storage_pause: Literal["waiting", "later"] | None = None
    pending_storage_bill: StorageBillDraft | None = None
    storage_billing: dict | None = None
    conflicting_quotes: set[date] = field(default_factory=set)
    timing_variety: str | None = None
    timing_dates: tuple[date, ...] | None = None
    absolute_loss_days: set[date] = field(default_factory=set)

    @property
    def eligible(self):
        return tuple(day for day in self.dates if day >= self.opened_on and day not in self.unavailable
                     and (not self.window or self.window[0] <= day <= self.window[1]))

    @property
    def excluded(self):
        return tuple(day for day in self.dates if day not in self.eligible)

    def has_quote(self):
        return any(self.by_date.get(day, (None, None))[0] is not None for day in self.eligible)

    def summary(self):
        def labels(days):
            return ", ".join(f"{day.month}월 {day.day}일" for day in days)
        lines = []
        if self.excluded:
            lines.append(f"**비교에서 제외** · {labels(self.excluded)}")
        if self.eligible:
            lines.append(f"**가능한 후보일** · {labels(self.eligible)}")
        if len(self.eligible) < 2:
            missing = "날짜를 하나 더" if self.eligible else "날짜를 두세 개"
            lines.append(f"비교할 수 있도록 출하 가능한 {missing} 알려주세요.")
        elif len(self.eligible) > 3:
            lines.append("이 중 비교할 날짜를 두세 개로 골라 주세요.")
        elif step := self.next_condition():
            lines.append(step.question)
        elif self.storage_pause:
            lines.append(self.condition_feedback())
        elif conflicts := self.conflicting_quotes.intersection(self.eligible):
            lines.append(f"{labels(sorted(conflicts))} 견적에 서로 다른 금액이 있어 비워뒀어요. "
                         "확인한 단가를 넣어 주세요.")
        else:
            lines.append("전체 물량과 받은 단가만 먼저 알려주세요. 단가를 모르는 날짜는 비워 두세요.")
        return "\n\n".join(lines)

    def update_availability(self, question, today):
        window, unavailable = shipment_availability(_calendar_text(question), today,
                                                     anchor=self.dates[0] if self.dates else None)
        if not unavailable and window is None:
            return False
        if window:
            self.window = window
        self.unavailable.update(unavailable)
        self.invalidate()
        return True

    def complete_selection(self, question, today):
        if 2 <= len(self.eligible) <= 3:
            return False
        text = _calendar_text(question)
        if not re.fullmatch(r"(?:\s*[,·/와과및]\s*|\s+)*(?:이요|예요|입니다|만|까지|로\s*해주세요)?[.!\s]*",
                            _DATE.sub("", text)):
            return False
        anchor = self.dates[0] if self.dates else today
        if re.match(r"\s*\d{1,2}\s*일", text):
            text = f"{anchor.year}년 {anchor.month}월 " + text.lstrip()
        parsed = requested_candidate_dates(f"{text} 출하 후보일 비교", today)
        if not parsed or not parsed[0]:
            return False
        self.dates = tuple(sorted(set(parsed[0]) | (set(self.dates) if len(self.eligible) < 2 else set())))
        self.invalidate()
        return True

    def calculate(self, *, by_date=None, reference_date=None):
        if self.pending_storage_bill:
            return {"status": "NEEDS_INPUT", "message": self.pending_storage_bill.question}
        return compare_shipment_candidates(
            self.dates, excluded_dates=self.excluded,
            by_date=self.by_date if by_date is None else by_date, reference_date=reference_date or self.reference_date,
            allow_unknown_losses=self.initial_inputs_saved,
            storage_billing=self.storage_billing,
            **{field: self.common.get(field) for field in _COMMON_FIELDS},
        )

    def save_initial_inputs(self, quantity, quotes_by_date):
        try:
            if quantity is None or decimal(quantity, "전체 물량") <= 0:
                raise ValueError("전체 물량을 0kg보다 크게 입력해 주세요.")
            if any(price is not None and decimal(price, "받은 단가") <= 0 for price in quotes_by_date.values()):
                raise ValueError("받은 단가는 0원보다 커야 해요.")
        except ValueError as error:
            return {"status": "INVALID_INPUT", "message": str(error)}
        previous_quantity = self.common.get("quantity")
        self.common["quantity"] = quantity
        for day, price in quotes_by_date.items():
            if day in self.eligible:
                self.by_date[day] = price, self.by_date.get(day, (None, None))[1]
        self._clear_weight_losses_for_quantity(previous_quantity, quantity)
        self.conflicting_quotes.clear()
        self.initial_inputs_saved = True
        self.invalidate()
        return self._finish_conditions()

    def next_condition(self):
        if (not self.initial_inputs_saved or self.pending_quote or self.storage_pause or not 2 <= len(self.eligible) <= 3
                or not self.has_quote()):
            return None
        fields = ["start", "basis"]
        if self.common.get("basis") == "gross":
            fields.append("fee")
        steps = [CandidateCondition(name, None, _QUESTIONS[name][1]) for name in fields if self.common.get(name) is None]
        storage_question = self._storage_billing_question()
        if storage_question or (self.storage_billing is None and self.common.get("daily_storage") is None):
            steps.append(CandidateCondition("daily_storage", None, storage_question or _QUESTIONS["daily_storage"][1]))
        if self.common.get("other_cost") is None:
            steps.append(CandidateCondition("other_cost", None, _QUESTIONS["other_cost"][1]))
        start = self.common.get("start")
        if start is not None:
            steps.extend(CandidateCondition("loss", day,
                         ("전체 물량을 바꿔서 다시 확인할게요. " if day in self.absolute_loss_days else "") +
                         f"{day.month}월 {day.day}일에 팔 때까지 판매 가능한 양이 몇 % 줄어드나요? "
                         "팔 수 있는 무게를 kg이나 톤으로 알려주셔도 돼요.")
                         for day in self.eligible if day > start and self.by_date.get(day, (None, None))[1] is None)
        for step in steps:
            if step not in self.deferred_conditions:
                return step
            if step.field in {"start", "basis"}:
                break
        return None

    def condition_feedback(self):
        if self.pending_quote:
            return ""
        if self.storage_pause and (pending := self.pending_storage_bill):
            field = "총액의 청구 기간" if pending.days is None else "일할인지 정액인지"
            if self.storage_pause == "waiting":
                return f"저장고에 {field} 확인해 주세요."
            return f"이 대화에서 {field} 알려주시면 이어서 비교할게요."
        if self.storage_pause == "waiting":
            return _CHECK_STORAGE_COST
        if self.storage_pause == "later":
            return _RESUME_STORAGE_COST
        if not self.has_quote():
            return "필요 단가의 기준을 잡으려면 후보일 한 곳의 받은 단가가 필요해요."
        if self.deferred_conditions:
            labels = sorted(step.label for step in self.deferred_conditions if step.day is None or step.day in self.eligible)
            if labels:
                return "미확인으로 둔 조건 · " + ", ".join(labels)
        return ""

    def timing_request(self, question):
        """Keep a #855 date selection separate from the economic candidate list."""
        from agents.future_pair_state import shipment_variety_reply
        from services.shipment_timing import CATEGORIES

        explicitly_requests_timing = bool(_TIMING_REQUEST.search(question))
        wants_timing = explicitly_requests_timing or (
            not self.has_quote() and any(_NO_QUOTE.fullmatch(clause.strip())
                                         for clause in re.split(r"[.!。;,，\n]", question))
        )
        variety = _stated_timing_variety(question) if wants_timing else shipment_variety_reply(question, CATEGORIES)
        if variety and not wants_timing and self.timing_dates is None:
            self.timing_variety = variety
            if not self.has_quote():
                return f"{variety}로 확인했어요. 같은 날짜의 과거 출하량을 먼저 볼 수 있어요."

        text = _calendar_text(question)
        choosing_dates = self.timing_dates == () and bool(
            re.fullmatch(r"[\s,·와과및랑하고]*(?:두\s*(?:날|날짜))?(?:이요|요|로|먼저|로\s*봐\s*줘)?[.!。\s]*",
                         _DATE.sub("", text))
        )
        if not (wants_timing or choosing_dates or (self.timing_dates is not None and variety)):
            return None
        if variety:
            self.timing_variety = variety
        eligible = self.eligible
        if len(eligible) < 2:
            return "과거 출하량을 비교하려면 출하 가능한 날짜가 두 개 필요해요."
        mentioned = {self._quote_date(match)[0] for match in _DATE.finditer(text)
                     if (explicitly_requests_timing or choosing_dates)
                     and (match["iso"] or match["month"] or match["unit"])
                     and not _STORAGE_TAIL.match(text[match.end():])
                     and not _STORAGE_BEFORE.search(text[:match.start()])}
        if mentioned and not mentioned <= set(eligible):
            return "현재 출하 가능한 후보일 중에서 두 날짜를 골라 주세요."
        if len(mentioned) == 2:
            self.timing_dates = tuple(sorted(mentioned))
        elif len(eligible) == 2:
            self.timing_dates = eligible
        elif not self.timing_dates or not set(self.timing_dates) <= set(eligible):
            self.timing_dates = ()
            return "과거 출하량은 두 날짜씩 볼 수 있어요. 먼저 볼 날짜 두 개를 골라 주세요."
        if not self.timing_variety:
            return "과거 출하량은 품종별로 볼 수 있어요. 감귤 품종이 무엇인가요?"
        earlier, later = self.timing_dates
        return {"shipment_variety": self.timing_variety, "option_a_date": earlier.isoformat(),
                "option_b_date": later.isoformat()}

    def _finish_conditions(self):
        if step := self.next_condition():
            return {"status": "NEEDS_INPUT", "message": step.question}
        if self.storage_pause:
            return {"status": "NEEDS_INPUT", "message": self.condition_feedback()}
        result = self.calculate()
        if result["status"] == "OK":
            self.result = result
        return result

    def _condition_to_answer(self):
        if self.storage_pause and self.initial_inputs_saved and not self.pending_quote and 2 <= len(self.eligible) <= 3:
            return CandidateCondition("daily_storage", None,
                                      self._storage_billing_question() or _QUESTIONS["daily_storage"][1])
        return self.next_condition()

    def _storage_billing_question(self):
        if pending := self.pending_storage_bill:
            return pending.question
        if self.storage_billing and (start := self.common.get("start")) and self.eligible:
            days = (self.eligible[-1] - start).days
            billed_days = self.storage_billing["days"]
            if days > billed_days:
                return (f"가장 늦은 후보는 {days}일 보관해야 해요. 확인한 {billed_days}일을 넘는 보관비를 "
                        "‘하루에 얼마’ 또는 ‘며칠에 총 얼마’로 알려주세요.")
        return None

    def _answer_storage_bill(self, text, *, daily_stated, corrected_mode=None):
        if daily_stated:
            return None
        if bill := stated_storage_bill(text):
            self.pending_storage_bill = bill
            self.storage_billing = None
            self.storage_pause = None
            self.common["daily_storage"] = None
            self.invalidate()
            return self._finish_conditions()
        if (pending := self.pending_storage_bill) is None:
            if (self.storage_billing and confirmed_total_storage_cost(text) is not None
                    and (question := self._storage_billing_question())):
                return {"status": "NEEDS_INPUT", "message": question}
            return None
        if days := storage_bill_days(text):
            self.pending_storage_bill = replace(pending, days=days)
            self.storage_pause = None
            self.invalidate()
            return self._finish_conditions()
        mode = corrected_mode or storage_bill_mode(text)
        if mode in {"fixed", "prorated"}:
            if pending.days is not None:
                self._save_storage_mode(pending, mode)
                return self._finish_conditions()
            return {"status": "NEEDS_INPUT", "message": pending.question}
        if confirmed_total_storage_cost(text) is not None or _OTHER_STORAGE_UNIT.search(text):
            return {"status": "NEEDS_INPUT", "message": pending.question}
        return None

    def _save_storage_mode(self, bill, mode):
        self.storage_billing = {"mode": mode, "days": bill.days, "total_krw": bill.total_krw}
        self.pending_storage_bill = None
        self.storage_pause = None
        self.invalidate()

    def _correct_storage_mode(self, question):
        if self.storage_billing is None or not (mode := storage_mode_correction(question)):
            return None
        if self.pending_quote is not None:
            return {"status": "NEEDS_INPUT", "message": self.pending_quote.question()}
        if mode == self.storage_billing["mode"]:
            return self.result if self.result is not None else self._finish_conditions()
        bill = StorageBillDraft(self.storage_billing["total_krw"], self.storage_billing["days"])
        if mode == "choose":
            self.pending_storage_bill = bill
            self.storage_billing = None
            self.storage_pause = None
            self.invalidate()
        else:
            self._save_storage_mode(bill, mode)
        return self._finish_conditions()

    def postpone_storage(self):
        if not self.storage_pause:
            return None
        self.storage_pause = "later"
        return self.condition_feedback()

    def apply_condition(self, step, value, *, entered_as_weight=False):
        if step is None or step != self._condition_to_answer():
            return {"status": "INVALID_INPUT", "message": "지금 확인 중인 조건을 다시 확인해 주세요."}
        name = step.field
        try:
            if value is not None and name == "start" and (
                type(value) is not date or not 0 <= (self.eligible[-1] - value).days <= 365 or value > self.eligible[0]
            ):
                raise ValueError("보관 시작일은 첫 출하일과 같거나 이전이어야 하고, 보관 기간은 365일 이내여야 해요.")
            if value is not None and name == "basis" and value not in {"gross", "commission_net"}:
                raise ValueError("판매수수료를 빼기 전인지 뺀 뒤인지 골라 주세요.")
            if value is not None and name not in {"start", "basis"}:
                amount = decimal(value, step.label)
                if amount < 0 or (name in {"fee", "loss"} and amount >= 100):
                    raise ValueError("비율은 0% 이상 100% 미만으로 입력해 주세요." if name in {"fee", "loss"}
                                     else "비용은 0원 이상으로 입력해 주세요.")
        except ValueError as error:
            return {"status": "INVALID_INPUT", "message": str(error)}
        if name == "daily_storage":
            self.storage_pause = (self.storage_pause or "waiting") if value is None else None
            if value is not None:
                self.pending_storage_bill = None
                self.storage_billing = None
        elif value is None:
            self.deferred_conditions.add(step)
        if name == "loss":
            self.by_date[step.day] = self.by_date.get(step.day, (None, None))[0], value
            if entered_as_weight:
                self.absolute_loss_days.add(step.day)
            else:
                self.absolute_loss_days.discard(step.day)
        else:
            self.common[name] = value
            if name == "fee":
                self.gross_fee = value
        self.invalidate()
        return self._finish_conditions()

    def answer_condition(self, question):
        if correction := self._correct_storage_mode(question):
            return correction, False
        if self.storage_pause and re.fullmatch(r"나중에\s*계속(?:할게요|할래요|해주세요)?[.!。\s]*", question.strip()):
            return {"status": "NEEDS_INPUT", "message": self.postpone_storage()}, False
        step = self._condition_to_answer()
        if step is None or re.search(r"[?？]", question):
            return None
        text = question.strip().rstrip(".!。 ")
        daily_cost = step.field == "daily_storage"
        if daily_cost:
            text, leading_daily = _DAILY_COST_PREFIX.subn("", text, count=1)
        text = re.sub(rf"^(?:{_CONDITION_PREFIX[step.field]})(?:은|는|이|가|를|을)?\s*", "", text)
        if _UNKNOWN_CONDITION.fullmatch(text):
            return self.apply_condition(step, None), True
        if step.field != "loss":
            text = _ANSWER_END.sub("", text).strip()
        if daily_cost:
            text, inner_daily = _DAILY_COST_PREFIX.subn("", text, count=1)
            text, trailing_daily = _DAILY_COST_SUFFIX.subn("", text, count=1)
            daily_stated = bool(leading_daily or inner_daily or trailing_daily)
            try:
                if bill_answer := self._answer_storage_bill(
                    text, daily_stated=daily_stated, corrected_mode=storage_mode_correction(question),
                ):
                    return bill_answer, False
            except ValueError as error:
                return {"status": "NEEDS_INPUT", "message": str(error)}, False
            if not daily_stated and _OTHER_STORAGE_UNIT.search(text):
                return {"status": "NEEDS_INPUT",
                        "message": "전체 물량을 하루 보관하는 데 드는 금액을 알려주시겠어요?"}, False
            text = re.sub(r"^(만|천)\s*원$", r"1\1원", text)
        try:
            if step.field == "loss":
                loss = self._loss_answer(step, text)
                if loss is None:
                    return None
                value, entered_as_weight = loss
                return self.apply_condition(step, value, entered_as_weight=entered_as_weight), False
            value = self._condition_value(step, text)
        except ValueError as error:
            return {"status": "NEEDS_INPUT", "message": str(error)}, False
        return (self.apply_condition(step, value), False) if value is not None else None

    def _condition_value(self, step, text):
        if step.field == "basis":
            if text in {"빼기 전", "차감 전"}:
                return "gross"
            if text in {"뺀 뒤", "차감 후"}:
                return "commission_net"
            match = _QUOTE_BASIS.fullmatch(text)
            return match.lastgroup if match and match.lastgroup != "unknown" else None
        if step.field == "start":
            match = _DATE.fullmatch(_calendar_text(text))
            if match and (match["iso"] or match["month"]):
                try:
                    return date.fromisoformat(match["iso"]) if match["iso"] else date(
                        int(match["year"] or self.eligible[0].year), int(match["month"]), int(match["day"]),
                    )
                except ValueError:
                    return None
        elif step.field == "fee":
            if percent := re.fullmatch(rf"({_QUOTE_NUMBER})\s*(?:%|퍼센트)", text):
                return decimal(percent[1].replace(",", ""), step.label)
        elif text in {"없", "없어", "없어요", "없습니다", "없음"}:
            return 0
        else:
            return confirmed_total_storage_cost(text.removeprefix("하루 "))
        return None

    def _loss_answer(self, step, text):
        values = []
        entered_as_weight = False
        for clause in _LOSS_ANSWER_SPLIT.split(text):
            clause = _calendar_text(clause.strip())
            date_match = _DATE.match(clause)
            has_date = date_match and (date_match["iso"] or date_match["month"] or date_match["unit"])
            if has_date:
                clause = re.sub(r"^\s*(?:에는|에|은|는|의)?\s*", "", clause[date_match.end():])
            amount_clause = _ANSWER_END.sub("", clause).strip()
            if percent := _LOSS_PERCENT.fullmatch(amount_clause):
                value = decimal(percent[1].replace(",", ""), step.label)
            elif weight := _SALEABLE_ANSWER.fullmatch(clause) or _SALEABLE_ANSWER.fullmatch(amount_clause):
                total = decimal(self.common["quantity"], "전체 물량")
                if weight["total"] is not None and _candidate_weight_kg(weight["total"]) != total:
                    raise ValueError("말씀하신 전체 물량이 현재 비교할 물량과 달라요. 어느 쪽이 맞나요?")
                saleable = _candidate_weight_kg(weight["saleable"])
                if not 0 < saleable <= total:
                    raise ValueError("팔 수 있는 양은 0kg보다 크고 전체 물량 이하여야 해요. 무게를 확인해 주세요.")
                value = (total - saleable) * 100 / total
                entered_as_weight = True
            else:
                return None
            if has_date and self._quote_date(date_match)[0] != step.day:
                raise ValueError(f"지금은 {step.day.month}월 {step.day.day}일에 팔 수 있는 양을 확인하고 있어요. "
                                 "이 날짜의 양이나 감모율을 알려주세요.")
            values.append(value)
        if len(set(values)) != 1:
            raise ValueError("판매 가능한 양과 감모율이 서로 달라요. 어느 값을 적용할까요?")
        return values[0], entered_as_weight

    def _clear_weight_losses_for_quantity(self, previous, quantity):
        if not self.absolute_loss_days or previous == quantity:
            return False
        if previous is not None and quantity is not None and decimal(previous, "전체 물량") == decimal(quantity, "전체 물량"):
            return False
        for day in self.absolute_loss_days:
            self.by_date[day] = self.by_date.get(day, (None, None))[0], None
        return bool(self.absolute_loss_days.intersection(self.eligible))

    def save_form_inputs(self, common, by_date, *, storage_mode=None):
        """Return True when edited conditions need guided confirmation."""
        if storage_mode is not None and (self.storage_billing is None or common["daily_storage"] is not None
                                         or storage_mode not in {"prorated", "fixed"}):
            raise ValueError("청구 방식과 보관비 기준을 다시 확인해 주세요.")
        common = {name: (_keep_unchanged_number(self.common.get(name), value)
                         if name in {"quantity", "daily_storage", "other_cost", "fee"} else value)
                  for name, value in common.items()}
        previous_basis, basis = self.common.get("basis"), common["basis"]
        previous_quantity = self.common.get("quantity")
        previous_start = self.common.get("start")
        start_changed = previous_start is not None and previous_start != common["start"]
        needs_quotes = basis != previous_basis and (
            previous_basis is not None or (not self.initial_inputs_saved
                                           and any(price is not None for price, _ in self.by_date.values()))
        )
        if previous_basis != "commission_net":
            self.gross_fee = common.get("fee")
        self.common = {**common, "fee": self.gross_fee if basis == "gross" else None}
        if self.common["daily_storage"] is not None:
            self.storage_pause = None
            self.pending_storage_bill = None
            self.storage_billing = None
        elif storage_mode is not None and storage_mode != self.storage_billing["mode"]:
            bill = StorageBillDraft(self.storage_billing["total_krw"], self.storage_billing["days"])
            self._save_storage_mode(bill, storage_mode)
        prior_losses = {day: self.by_date[day][1] for day in self.absolute_loss_days}
        for day, (price, loss) in by_date.items():
            old_price, old_loss = self.by_date.get(day, (None, None))
            self.by_date[day] = (_keep_unchanged_number(old_price, price), _keep_unchanged_number(old_loss, loss))
        recheck_weight = self._clear_weight_losses_for_quantity(previous_quantity, common["quantity"])
        if not recheck_weight:
            for day, old_loss in prior_losses.items():
                if old_loss != self.by_date[day][1]:
                    self.absolute_loss_days.discard(day)
        if start_changed:
            self.by_date = {day: (price, None) for day, (price, _) in self.by_date.items()}
            self.absolute_loss_days.clear()
            self.deferred_conditions = {step for step in self.deferred_conditions if step.field != "loss"}
            self.invalidate()
        elif recheck_weight:
            self.invalidate()
        self.deferred_conditions = {step for step in self.deferred_conditions
                                    if (by_date.get(step.day, (None, None))[1] if step.day else
                                        self.common.get(step.field)) is None}
        if needs_quotes:
            self.by_date = {day: (None, loss) for day, (_, loss) in self.by_date.items()}
            self.initial_inputs_saved = False
            self.invalidate()
        return needs_quotes or recheck_weight

    def cancel_quote(self, question):
        if self.pending_quote is not None and _CANCEL_QUOTE.fullmatch(question.strip()):
            self.pending_quote = None
            return True
        return False

    def update_quote(self, question, today, *, on_progress=None):
        """Apply one confirmed price only after resolving its existing candidate date."""
        if _OTHER_QUOTE_CONTEXT.search(question) or shipment_choice_question(question):
            return None
        text = _calendar_text(question).strip()
        pending = self.pending_quote
        if pending is not None:
            text = _QUOTE_REDIRECT.sub("", text, count=1)
        match_date = _DATE.match(text)
        explicit_date = match_date and (match_date["iso"] or match_date["month"] or match_date["unit"])
        if not explicit_date:
            followup = self._follow_up_quote(text, today, on_progress=on_progress)
            if followup is not None:
                return followup
        tail = text[match_date.end():] if explicit_date else ""
        basis, quote = _quote_terms(tail) if explicit_date else (None, None)
        refocus = explicit_date and pending is not None and _QUOTE_DATE_ONLY.fullmatch(tail)
        if quote is None and not refocus:
            mentions_amount = mentions_box_price(text) or _MENTIONS_QUOTE_AMOUNT.search(text)
            if mentions_amount and (explicit_date or _BARE_QUOTE.fullmatch(text) or _QUOTE_STATEMENT.fullmatch(text)
                                    or re.match(r"(?:kg|킬로)\s*당", text, re.I)):
                if explicit_date:
                    self.pending_quote = None
                return "어느 후보일 견적인가요? 출하 날짜와 kg당 단가를 함께 알려주세요."
            return None
        day, error = self._quote_date(match_date)
        if pending is not None and pending.day != day:
            self.pending_quote = None
        if error:
            return error
        if day not in self.eligible or day < today:
            return "출하 불가일로 제외된 날짜에는 견적을 반영할 수 없어요. 현재 가능한 후보일을 확인해 주세요."
        current_basis = self.common.get("basis")
        if refocus and quote is None:
            if self.pending_quote is None:
                self.pending_quote = _PendingCandidateQuote(day, required_basis=current_basis)
            return self.pending_quote.question()
        if basis and not current_basis:
            return "직접 입력에서 후보일 공통 수수료 기준을 정한 뒤 이 견적을 다시 알려주시겠어요?"
        if basis and basis != current_basis:
            self.pending_quote = _PendingCandidateQuote(day, required_basis=current_basis)
            return self.pending_quote.question()
        return self._apply_quote(day, quote, on_progress=on_progress)

    def _follow_up_quote(self, text, today, *, on_progress):
        pending = self.pending_quote
        if pending is None:
            return None
        if pending.day not in self.eligible or pending.day < today:
            self.pending_quote = None
            return None
        basis, quote = _quote_terms(text, allow_bare=pending.box is None)
        if basis and basis != self.common.get("basis"):
            return pending.question() if pending.required_basis else None
        if isinstance(quote, Decimal):
            return self._apply_quote(pending.day, quote, on_progress=on_progress)
        if pending.box is None:
            return pending.question() if quote is not None else None
        if quote is not None:
            return self._apply_quote(pending.day, quote, on_progress=on_progress)
        status, box = pending.box.follow_up(text)
        if status == "cancel":
            self.pending_quote = None
            return "견적은 그대로 둘게요."
        if status == "clarify" and box.weight is not None:
            return self._apply_quote(pending.day, box, on_progress=on_progress)
        unknown_weight = re.fullmatch(rf"{_UNKNOWN_WEIGHT}[.!。\s]*", text)
        if unknown_weight or (status and re.search(r"몰라|모르", text)):
            return f"상자 무게를 모르시면 {pending.day.month}월 {pending.day.day}일 견적을 kg당 얼마로 받으셨는지 알려주시겠어요?"
        return pending.question() if status else None

    def _apply_quote(self, day, quote, *, on_progress):
        box_terms = None
        price = quote
        if isinstance(quote, BoxPrice):
            if quote.weight is None:
                self.pending_quote = _PendingCandidateQuote(day, box=quote)
                return self.pending_quote.question()
            price = quote.unit_price
            box_terms = (quote.weight, quote.amount)
        if price <= 0:
            return "kg당 단가는 0원보다 커야 해요. 받은 견적을 다시 확인해 주세요."
        original, loss = self.by_date.get(day, (None, None))
        from presenters.reporting import shipment_candidate_quote_reply

        if self.result is not None and original is not None and Decimal(str(original)) == price:
            self.pending_quote = None
            return shipment_candidate_quote_reply(day, price, self.result, already_applied=True, box_terms=box_terms)
        proposed = {**self.by_date, day: (price, loss)}
        reference = self.result.get("reference_date") if self.result else None
        if on_progress:
            on_progress("compare")
        result = self.calculate(by_date=proposed, reference_date=date.fromisoformat(reference) if reference else None)
        if result["status"] not in {"OK", "NEEDS_INPUT"}:
            return "견적을 반영하지 못했어요. " + result["message"]
        self.by_date = proposed
        self.conflicting_quotes.discard(day)
        if self.timing_dates and self.common.get("quantity") is not None:
            self.initial_inputs_saved = True
        self.invalidate()
        if step := self.next_condition():
            result = {"status": "NEEDS_INPUT", "message": step.question}
        elif result["status"] == "OK":
            self.result = result
        return shipment_candidate_quote_reply(day, price, result, box_terms=box_terms)

    def _quote_date(self, match):
        if match["iso"]:
            candidates = [day for day in self.dates if day.isoformat() == match["iso"]]
        else:
            candidates = [day for day in self.dates if day.day == int(match["day"])
                          and (not match["month"] or day.month == int(match["month"]))
                          and (not match["year"] or day.year == int(match["year"]))]
        if len(candidates) == 1:
            return candidates[0], None
        if candidates:
            return None, "같은 일자가 여러 후보에 있어요. 월과 일을 함께 알려주세요."
        return None, "그 날짜는 현재 후보일에 없어요. 견적을 받은 후보일의 날짜와 kg당 단가를 알려주세요."

    def invalidate(self):
        if self.result and self.result.get("reference_date"):
            self.reference_date = date.fromisoformat(self.result["reference_date"])
        self.result = None
        self.pending_quote = None
        self.revision += 1


def open_candidate_draft(dates, start, question, today, pair, scenario_version, *, new_comparison):
    window, excluded = shipment_availability(_calendar_text(question), today, anchor=dates[0] if dates else None)
    values = pair.inputs
    quantity = explicit_shipment_quantity(question)
    common = {"start": start, "quantity": quantity}
    by_date = {}
    if not new_comparison and re.search(r"(?:같은|기존|앞서\s*입력한)\s*조건", question):
        pair_start = date.fromisoformat(values["start_date"]) if values["start_date"] else None
        common.update(start=start or pair_start,
                      quantity=quantity if quantity is not None else values["quantity"], basis=values["price_basis"])
        for field, name in (("daily_storage_cost", "daily_storage"), ("other_cost_krw", "other_cost"), ("fee_percent", "fee")):
            if values["option_a"][field] == values["option_b"][field]:
                common[name] = values["option_a"][field]
        from services.shipment_timing import resolve_pair_dates

        for side, day in zip(("a", "b"), resolve_pair_dates(values)):
            if day:
                loss = values[f"option_{side}"]["loss_percent"] if common["start"] == pair_start else None
                by_date[date.fromisoformat(day)] = values[f"price_{side}"], loss
    dates = tuple(sorted(set(dates) | excluded))
    if not new_comparison:
        window = window or pair.availability
        excluded.update(pair.unavailable_dates)
    draft = ShipmentCandidateDraft(dates, window, excluded, pair.version, scenario_version, today, common, by_date)
    draft.timing_variety = _stated_timing_variety(question)
    # Explicitly dated kg quotes in a separate clause can be reused by the short form.
    stated_quotes: dict[date, set[Decimal]] = {}
    for clause in re.split(r"[;；\n]|,(?!\d)", _calendar_text(question)):
        match = _DATE.match(clause.strip())
        if match is None or not (match["iso"] or match["month"] or match["unit"]):
            continue
        day, _ = draft._quote_date(match)
        basis, quote = _quote_terms(clause.strip()[match.end():])
        if day in draft.eligible and isinstance(quote, Decimal) and quote > 0 and basis in {None, common.get("basis")}:
            stated_quotes.setdefault(day, set()).add(quote)
    for day, quotes in stated_quotes.items():
        if len(quotes) == 1:
            draft.by_date[day] = next(iter(quotes)), draft.by_date.get(day, (None, None))[1]
        else:
            draft.by_date[day] = None, draft.by_date.get(day, (None, None))[1]
            draft.conflicting_quotes.add(day)
    return draft
