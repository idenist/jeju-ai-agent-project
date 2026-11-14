"""Recognize clear two-future-shipment requests before model tool selection."""

import re
from datetime import date
from decimal import Decimal


_SHIPMENT = re.compile(r"출하|팔[지까면때고]|팔\s|판매")
_CHOICE = re.compile(r"비교|고민|아니면|언제|\b중\b|나을|낫|유리|대비|팔지|팔까")
_OTHER_PURPOSE = re.compile(
    r"정산|검산|출하량|출하\s*통계|공급\s*집중도|제주\s*데이터\s*허브|#\s*855\b|"
    r"민감도|역산|예측|뜻|의미|설명|보고서만|과거\s*(?:경락)?가격만")
_TODAY_SALE = re.compile(r"(?:오늘|지금|당장|즉시|바로)\s*(?:출하|판매|팔)|(?<!\d)0\s*일\s*(?:뒤|후)")
_DAY_NAMES = {name: days for days, name in enumerate(
    ("하루", "이틀", "사흘", "나흘", "닷새", "엿새", "이레", "여드레", "아흐레", "열흘"), 1)}
_WEEK_NAMES = {"일": 1, "한": 1, "두": 2, "세": 3, "네": 4}
_RELATIVE_DAY = re.compile(
    rf"(?<![\d.])(?:([1-9]\d{{0,2}})\s*일|({'|'.join(_DAY_NAMES)})|"
    rf"({'|'.join(_WEEK_NAMES)}|[1-9]\d?)\s*주(?:일)?)\s*(?:뒤|후)|"
    r"(?<![\d.])([1-9]\d{0,2})\s*일\s*보관(?:한|하고|했다가)?\s*(?:뒤|후|출하|판매|팔)")
_CALENDAR_DAY = re.compile(r"(?<!\d)(?:(\d{4})\s*년\s*)?(\d{1,2})\s*월\s*(\d{1,2})\s*일|(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_WEIGHT = re.compile(r"(?<![\d.,\-−])([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)\s*(kg|킬로그램|톤|t)(?![a-z])")
_PACK_BEFORE = re.compile(
    r"(?:상자|박스|포장|봉지|포대|팩)(?:\s*(?:한\s*개|하나|1\s*개))?\s*"
    r"(?:(?:의\s*)?(?:감귤\s*)?무게\s*)?(?:당|에는|에|은|는|이|가)?\s*$")
_PACK_AFTER = re.compile(r"\s*(?:짜리|당|씩|상자|박스|포장|봉지|포대|팩)")
_STORAGE_START = re.compile(r"(?:보관\s*시작(?:일|은|이|을)?|입고일)\s*(?:은|는|이|가|을|를)?\s*$")


def requests_manual_price(question: str) -> bool:
    return any(word in question for word in ("직접 입력", "직접입력", "직접 넣", "정한 단가", "내가 입력"))


def is_future_pair_request(question: str, today: date) -> bool:
    text = question.casefold()
    if not (_SHIPMENT.search(text) and _CHOICE.search(text)) or _OTHER_PURPOSE.search(text) or _TODAY_SALE.search(text):
        return False
    # Remove the date of a stated storage start before counting shipment dates.
    dates = []
    for match in _CALENDAR_DAY.finditer(text):
        if (_STORAGE_START.search(text[:match.start()])
                or re.match(r"\s*(?:부터|에\s*보관\s*시작)", text[match.end():])):
            continue
        year, month, day = match.groups()[:3] if match[2] else match.groups()[3:]
        try:
            shipment_day = date(int(year or today.year), int(month), int(day))
        except ValueError:
            continue
        if shipment_day > today:
            dates.append(shipment_day)
    periods = set()
    for days, day_name, weeks, storage_days in _RELATIVE_DAY.findall(text):
        if weeks:
            periods.add(7 * (int(weeks) if weeks.isdigit() else _WEEK_NAMES[weeks]))
        else:
            periods.add(_DAY_NAMES[day_name] if day_name else int(days or storage_days))
    future_days = periods | {(shipment_day - today).days for shipment_day in dates}
    return len(future_days) == 2


def explicit_shipment_quantity(question: str) -> float | None:
    """Use an unambiguous total weight, excluding weights used for prices or packs."""
    text = question.casefold()
    weights = set()
    for match in _WEIGHT.finditer(text):
        before, after = text[:match.start()], text[match.end():]
        value, unit = match.groups()
        if unit in {"kg", "킬로그램"} and (_PACK_BEFORE.search(before) or _PACK_AFTER.match(after)):
            continue
        if (re.search(r"\d\s*[/~～–-]\s*$", before)
                or re.match(r"\s*(?:[/~～–-]|아니|말고|인지|일지|가량|정도|미만|이하|이상|초과)", after)):
            return None
        kg = Decimal(value.replace(",", "")) * (1000 if unit in {"톤", "t"} else 1)
        if 0 < kg <= 10**12:
            weights.add(kg)
    return float(weights.pop()) if len(weights) == 1 else None
