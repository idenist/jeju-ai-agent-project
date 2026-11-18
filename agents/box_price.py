"""User confirmation of a P2 box quote before it becomes a unit price."""

import re
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP


_NUMBER = r"[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?"
_KOREAN_DIGITS = "일이삼사오육칠팔구"
_KOREAN_UNITS = {"천": 1000, "백": 100, "십": 10}
_KOREAN_NUMBER = rf"[{_KOREAN_DIGITS}천백십만](?:\s*[{_KOREAN_DIGITS}천백십만])*"
_PRICE_AMOUNT = rf"(?<![\d,.])(?:{_NUMBER}\s*(?:만\s*)?|{_KOREAN_NUMBER}\s*)원"
_AMOUNT = re.compile(_PRICE_AMOUNT)
_ARABIC_AMOUNT = re.compile(rf"({_NUMBER})\s*(만\s*)?원")
_KOREAN_PART = re.compile(rf"([{_KOREAN_DIGITS}]?)([천백십])|([{_KOREAN_DIGITS}])")
_MASS = re.compile(rf"(?<![\d,.])({_NUMBER})\s*(?:kg|킬로그램)", re.I)
_PACK = r"(?:상자|박스|포장)"
_PACK_COUNT = r"(?:한\s*개|하나|1\s*개)"
_PACK_WITH_WEIGHT = rf"{_NUMBER}\s*(?:kg|킬로그램)\s*(?:짜리\s*)?{_PACK}"
_BOX_PRICE = re.compile(
    rf"(?:{_PACK_WITH_WEIGHT}(?:\s*{_PACK_COUNT})?\s*(?:당|에)?"
    rf"|{_PACK}\s*(?:{_PACK_COUNT}\s*)?(?:당|에)|한\s*{_PACK}(?:당|에)?)"
    rf"\s*(?:가격\s*(?:은|이)?\s*)?{_PRICE_AMOUNT}|{_PRICE_AMOUNT}\s*/\s*{_PACK}", re.I,
)
_SENTENCE_BREAK = re.compile(r"[!?。;；]|(?<!\d)\.|\.(?!\d)")
_LIST_BREAK = re.compile(r"[，、\n]|(?<!\d),|,(?!\d)")
_HISTORY_CONTEXT = re.compile(
    r"(?:과거|예전)\s*(?:경락\s*)?(?:가격|시세|경락가|자료|데이터|거래)"
    r"|(?:시세|경락\s*가격|가격)\s*조회|조회\s*(?:용|규격|조건|는|할|기준|에선|에서는)"
)
_SALE_WEIGHT_CONTEXT = re.compile(
    rf"(?:실제\s*(?:(?:판매|출하)(?:용|할)?\s*)?|(?:판매|출하)(?:용|할)?\s*|[AB]\s*안(?:의|은|는)?\s*)"
    rf"(?:{_PACK}|무게|중량|판매가|출하\s*단가)", re.I,
)
_SIDE_A = re.compile(r"(?:a\s*안|먼저\s*(?:출하|팔))", re.I)
_SIDE_B = re.compile(r"(?:b\s*안|나중\s*(?:출하|팔))", re.I)
_PACK_WEIGHT = (
    re.compile(rf"{_PACK}\s*(?:{_PACK_COUNT}\s*)?(?:무게\s*(?:는|가)?\s*|당\s*|에\s*|는\s*|가\s*|\s*)({_NUMBER})\s*(?:kg|킬로그램)", re.I),
    re.compile(rf"({_NUMBER})\s*(?:kg|킬로그램)\s*(?:짜리\s*)?{_PACK}", re.I),
)
_WEIGHT_REPLY = re.compile(
    rf"^(?:아니요[, ]*)?\s*(?:(?:실제\s*)?(?:(?:판매|출하)(?:용|할)?\s*)?{_PACK}"
    rf"|{_NUMBER}\s*(?:kg|킬로그램))", re.I,
)
_ALTERNATE_WEIGHT = re.compile(rf"(?:kg|킬로그램)\s*(?:인지|이나|또는|아니면|혹은|[~/～–-])\s*({_NUMBER})\s*(?:kg|킬로그램)", re.I)
_WEIGHT_RANGE = re.compile(rf"{_NUMBER}\s*[-~～–]\s*{_NUMBER}\s*(?:kg|킬로그램)", re.I)
_UNCERTAIN = re.compile(r"모르|몰라|인지|일지|애매|헷갈|정도|가량|쯤|아마|듯|[~～–]")
_YES = re.compile(r"(?:(?:네|예|응)[, ]*)?(?:맞(?:아(?:요)?|습니다)|그대로(?:\s*(?:계산|비교)(?:해\s*(?:줘|주세요))?)?|확인(?:했어요|했습니다)?|적용해\s*(?:줘|주세요))|네|예|응")
_NO = re.compile(r"아니(?:요|에요)?|아니에요|아닙니다|취소(?:해\s*(?:줘|주세요))?")


def mentions_box_price(text):
    return bool(_BOX_PRICE.search(text))


def _quote_amount(text):
    amount = _AMOUNT.search(text).group()
    if found := _ARABIC_AMOUNT.fullmatch(amount):
        return Decimal(found[1].replace(",", "")) * (10000 if found[2] else 1)

    parts = "".join(amount[:-1].split()).split("만")
    if len(parts) > 2:
        return None
    amounts = []
    for part in parts:
        total, last_unit = 0, 10000
        for digit, unit, ones in _KOREAN_PART.findall(part):
            value = _KOREAN_UNITS[unit] if unit else 1
            if value >= last_unit:
                return None
            coefficient = _KOREAN_DIGITS.index(digit or ones) + 1 if digit or ones else 1
            total += coefficient * value
            last_unit = value
        amounts.append(total)
    return (amounts[0] or 1) * 10000 + amounts[1] if len(amounts) == 2 else amounts[0]


def _sale_weight_text(text):
    """Keep lookup lists separate while preserving uncertainty about a quoted sale weight."""
    sales = []
    for sentence in _SENTENCE_BREAK.split(text):
        in_history = False
        for clause in _LIST_BREAK.split(sentence):
            if history := _HISTORY_CONTEXT.search(clause):
                in_history = True
                before_lookup = clause[:history.start()]
                if _BOX_PRICE.search(before_lookup):
                    sales.extend(match.group() for match in _BOX_PRICE.finditer(before_lookup))
                    for pattern in (_UNCERTAIN, _ALTERNATE_WEIGHT, _WEIGHT_RANGE):
                        if uncertainty := pattern.search(before_lookup):
                            sales.append(uncertainty.group())
            elif (not in_history or _SALE_WEIGHT_CONTEXT.search(clause)
                  or (_BOX_PRICE.search(clause) and (_SIDE_A.search(clause) or _SIDE_B.search(clause)))):
                in_history = False
                sales.append(clause)
    return "\n".join(sales).strip()


def _weight(text, *, reply=False):
    matches = [found for pattern in _PACK_WEIGHT for found in pattern.findall(text)]
    if reply and not matches:
        matches = _MASS.findall(text)
    distinct = {Decimal(value.replace(",", "")) for value in [*matches, *_ALTERNATE_WEIGHT.findall(text)]}
    if len(distinct) != 1 or _UNCERTAIN.search(text) or _WEIGHT_RANGE.search(text):
        return None
    value = distinct.pop()
    return value if Decimal("0.001") <= value <= 1000 else None


def proposed_box_price(text, model_quote):
    """Use the model only to locate the quote and identify its side."""
    if not mentions_box_price(text):
        return None
    # The model points to the literal quote, so other prices or storage fees do not become box prices.
    quote = model_quote if isinstance(model_quote, dict) else {}
    quotes = list(_BOX_PRICE.finditer(text))
    if len(quotes) != 1:
        return None
    match = quotes[0]
    phrase = quote.get("phrase")
    matches_phrase = isinstance(phrase, str) and phrase in text and mentions_box_price(phrase)
    amount = _quote_amount(match.group())
    if amount is None or not 0 < amount <= 10**12:
        return None
    side = quote.get("side") if matches_phrase else None
    clause = re.split(r"[.!?。\n]", text[:match.start()])[-1]
    explicit_sides = [label for label, pattern in (("A", _SIDE_A), ("B", _SIDE_B)) if pattern.search(clause)]
    if len(explicit_sides) == 1:
        side = explicit_sides[0]
    return BoxPrice(Decimal(amount), side if side in {"A", "B"} else None, _weight(_sale_weight_text(text)))


def pending_box_arguments(question, arguments):
    """Separate the quote from numeric tool inputs until the user confirms it."""
    args = dict(arguments)
    quote = proposed_box_price(question, args.pop("box_price", None))
    patch = dict(args["patch"])
    for field, side in (("price_a", "A"), ("price_b", "B")):
        if quote is None or quote.side in {None, side}:
            patch[field] = None
    if quote is not None and quote.side == "B" and patch.get("price_mode") is None:
        patch["price_mode"] = "manual"
    if patch.get("price_mode") == "manual":
        patch["history_filters"] = None
    args.update(patch=patch, finish_turn=True)
    return args, quote


def _number(value):
    return f"{value.normalize():,f}"


@dataclass(frozen=True)
class BoxPrice:
    amount: Decimal
    side: str | None
    weight: Decimal | None

    @property
    def unit_price(self):
        return (self.amount / self.weight).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def follow_up(self, text):
        clean = text.strip().rstrip(".!?。！？」").strip()
        if _NO.fullmatch(clean):
            return "cancel", self
        if self.side is None and re.fullmatch(r"(?:a안|b안|먼저(?:\s*출하)?|나중(?:\s*출하)?)(?:이에요|예요|입니다)?", clean.casefold()):
            side = "B" if clean.casefold().startswith(("b", "나중")) else "A"
            return "clarify", replace(self, side=side)
        sale_text = _sale_weight_text(clean)
        if (_WEIGHT_REPLY.match(sale_text) and _MASS.search(sale_text) and len(clean) <= 55
                and not _AMOUNT.search(clean) and not re.search(r"[?？]", text)):
            weight = _weight(sale_text, reply=True)
            return "clarify", replace(self, weight=weight)
        if _YES.fullmatch(clean):
            return ("confirm" if self.side and self.weight and self.unit_price > 0 else "clarify"), self
        return None, self

    def question(self):
        amount = _number(self.amount)
        if not self.side:
            return f"상자당 {amount}원은 먼저 출하하는 A안 가격인가요, 나중 출하하는 B안 가격인가요?"
        if self.weight is None:
            return f"{self.side}안 상자당 {amount}원에서 실제 판매할 상자 한 개의 감귤은 정확히 몇 kg인가요?"
        if self.unit_price == 0:
            return f"{self.side}안 환산 단가가 0.01원/kg 미만입니다. 상자 가격과 무게를 다시 알려주세요."
        weight = _number(self.weight)
        unit = _number(self.unit_price)
        return (f"{self.side}안 상자당 {amount}원 ÷ {weight}kg = **{unit}원/kg**입니다. "
                f"{self.side}안 단가로 적용할까요? 맞으면 ‘네’, 무게가 다르면 정확한 kg을 알려주세요.")
