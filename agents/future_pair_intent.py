"""Recognize clear two-future-shipment requests before model tool selection."""

import re
from datetime import date
from decimal import Decimal


_SHIPMENT = re.compile(r"출하|팔[지까면때고]|팔\s|판매(?!\s*수수료)")
_CHOICE = re.compile(r"비교|고민|아니면|언제|\b중\b|나을|낫|유리|대비|팔지|팔까")
_OTHER_PURPOSE = re.compile(
    r"정산|검산|출하량|출하\s*통계|공급\s*집중도|제주\s*데이터\s*허브|#\s*855\b|"
    r"민감도|역산|뜻|의미|설명|보고서만|과거\s*(?:경락)?가격만")
_TODAY_SALE = re.compile(r"(?:오늘|지금|당장|즉시|바로)\s*(?:출하|판매|팔)|(?<!\d)0\s*일\s*(?:뒤|후)")
_DAY_NAMES = {name: days for days, name in enumerate(
    ("하루", "이틀", "사흘", "나흘", "닷새", "엿새", "이레", "여드레", "아흐레", "열흘"), 1)}
_WEEK_NAMES = {"일": 1, "한": 1, "두": 2, "세": 3, "네": 4}
_RELATIVE_DAY = re.compile(
    rf"(?<![\d.])(?:([1-9]\d{{0,2}})\s*일|({'|'.join(_DAY_NAMES)})|"
    rf"({'|'.join(_WEEK_NAMES)}|[1-9]\d?)\s*주(?:일)?)\s*(?:뒤|후)|"
    r"(?<![\d.])([1-9]\d{0,2})\s*일\s*보관(?:한|하고|했다가)?\s*(?:뒤|후|출하|판매|팔)")
_SHARED_RELATIVE_DAY = re.compile(
    rf"(?<![\d.])([1-9]\d{{0,2}}\s*일|{'|'.join(_DAY_NAMES)})\s*(?:·|ㆍ|/|,|와|과|하고|랑|및)\s*"
    rf"(?:[1-9]\d{{0,2}}\s*일|{'|'.join(_DAY_NAMES)})\s*(?:뒤|후)"
)
_CALENDAR_DAY = re.compile(r"(?<!\d)(?:(\d{4})\s*년\s*)?(\d{1,2})\s*월\s*(\d{1,2})\s*일|(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_DATED_STORAGE = re.compile(r"(?<![\d/])(?:(\d{4})\s*년\s*)?(\d{1,2})\s*(?:월\s*|/)\s*(\d{1,2})\s*(?:일\s*)?부터\s*보관")
_WEIGHT = re.compile(r"(?<![\d.,\-−])([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)\s*(kg|킬로그램|톤|t)(?![a-z])")
_PACK_BEFORE = re.compile(
    r"(?:상자|박스|포장|봉지|포대|팩)(?:\s*(?:한\s*개|하나|1\s*개))?\s*"
    r"(?:(?:의\s*)?(?:감귤\s*)?무게\s*)?(?:당|에는|에|은|는|이|가)?\s*$")
_PACK_AFTER = re.compile(r"\s*(?:짜리|당|씩|상자|박스|포장|봉지|포대|팩)")
_STORAGE_START = re.compile(r"(?:보관\s*시작(?:일|은|이|을)?|입고일)\s*(?:은|는|이|가|을|를)?\s*$")
_NEW_COMPARISON = re.compile(
    r"(?:^|[.!?\n])\s*(?:(?:이번엔|이번에는|그럼|이제|이전과\s*별개로|기존과\s*별개로|아까와\s*별개로)\s*)*"
    r"(?:새(?:로운|로)?\s*(?:(?:출하|손익)\s*)?비교|비교를\s*새로|처음부터\s*(?:다시\s*)?(?:출하\s*)?비교)"
    r"(?!(?:는|를|가)?\s*(?:말고|아니|하지\s*말|결과|뜻|방법))"
)
_PRICE_BASIS = re.compile(
    r"수수료\s*(?:(?:를|는|가)\s*)?(?:이미\s*)?"
    r"(?:(?:차감|공제|제외|빼기|떼기)\s*(?:(?:하기|한)\s*)?(?:전|후|뒤)|뺀|빼고|떼고|뗀|빠진|포함|제외한)|실수취\s*(?:단가|가격|금액)"
)
_PRICE_BASIS_UNCERTAIN = re.compile(r"^\s*(?:(?:금액|단가|가격)\s*)?(?:인지|일지|이냐|또는|/|·)")
_WINDOW_START = re.compile(r"^\s*(?:부터|이후|[~～–])")
_WINDOW_LIMIT = re.compile(
    r"(?:^|부터\s*|[~～–]\s*)(?:(\d{1,2})\s*월\s*)?(\d{1,2}\s*일|말일)\s*까지"
    r"(?:\s*(?:는\s*)?(?:출하(?:할\s*수\s*있|\s*가능하)?|팔\s*수\s*있|할\s*수\s*있|가능해)?"
    r"(?:어요|습니다|해요|합니다|요)?)?[.!\s]*$"
)
_CONTINUE_COMPARISON = re.compile(
    r"(?:기존|현재|직전|아까(?:의)?)\s*(?:출하\s*)?비교|(?:나중\s*출하일|B안\s*(?:날짜|단가))만"
    r"|(?:앞서|방금)\s*(?:확인한|본)\s*(?:과거\s*)?(?:가격|단가)\s*가정"
)
_LATER_DATE_ONLY = re.compile(r"(?:나중\s*출하일|B\s*안\s*(?:출하일|날짜))만", re.I)
_LATER_STORAGE_EDIT = re.compile(
    r"\s*(?:(?P<calendar>나중\s*출하일|B\s*안\s*(?:출하일|날짜))|"
    r"B\s*안(?:\s*보관\s*(?:일|기간))?)\s*(?:만|은|을)?\s*(?P<days>\d{1,3})\s*일"
    r"(?P<storage>\s*보관\s*(?:하는\s*날(?:짜)?)?)?\s*으?로\s*(?:바꿔|변경해|수정해)"
    r"\s*(?:줘(?:요)?|주세요)?[.!\s]*",
    re.I,
)
_HISTORICAL_PRICE_EXCLUSION = re.compile(
    r"(?:과거\s*(?:경락\s*)?(?:가격|시세|자료|변동)|경락\s*가격|시장\s*(?:시세|자료)|도매\s*시세|과거가격|자료)\s*"
    r"(?:을|를|은|는|도|와|과)?\s*(?:조회\s*(?:하지\s*말|안\s*(?:해|봐|하고)|하지\s*않)|"
    r"안\s*(?:보고|보|쓰고|쓰|사용하고|사용|참고하고|참고|조회하고|조회)|"
    r"(?:보지|쓰지|사용하지|참고하지|비교하지|조회하지|활용하지|적용하지|반영하지)\s*(?:말고|말|마|않)|"
    r"필요\s*없|없이|빼고|제외)"
)
_LATER_PRICE_UNKNOWN = re.compile(
    r"\s*(?:(?:B\s*안|나중(?:\s*출하)?)\s*(?:의\s*)?(?:단가|가격|견적)(?:은|는|을|를|이|가)?\s*)?"
    r"(?:아직\s*|잘\s*)?(?:모르겠어요|모릅니다|몰라요|(?:견적(?:을)?\s*)?못\s*받았어요|"
    r"(?:견적(?:이)?\s*)?안\s*나왔어요)[.!?\s]*", re.I,
)
_BREAK_EVEN_REQUEST = re.compile(
    r"\s*(?:(?:그럼|그러면|과거\s*자료\s*없이)\s*)?"
    r"(?:(?:기다리려면|늦게\s*팔려면|나중에\s*팔려면|B\s*안은)\s*얼마(?:\s*이상)?\s*받아야\s*"
    r"(?:해(?:요)?|돼(?:요)?|하나요|되나요)|(?:B\s*안\s*)?손익분기(?:\s*(?:단가|가격))?"
    r"(?:를|만|부터)?\s*(?:계산해|알려|보여|봐)(?:\s*(?:줘(?:요)?|주세요))?|"
    r"손익분기(?:\s*(?:단가|가격))?(?:이|가|는)?\s*얼마(?:예요|인가요|죠)|"
    r"얼마(?:나\s*받아야|면)\s*손해(?:가)?\s*(?:없나요|없어요|안\s*나나요|안\s*나요))"
    r"[.!?\s]*", re.I,
)
_MARKET_THRESHOLD_REQUEST = re.compile(
    r"\s*(?:과거\s*시장\s*근거(?:도|를)?\s*(?:보여\s*줘(?:요)?|보여\s*주세요|보기)|"
    r"(?:과거에도|예전에도)\s*(?:그\s*(?:가격|단가)|손익분기\s*(?:가격|단가))(?:을|를|이|가)?\s*"
    r"(?:넘었(?:어요|나요)|넘은\s*(?:날|사례)(?:이|가)?\s*있(?:어요|나요)))[.!?\s]*", re.I,
)


def requests_market_threshold_observation(question: str) -> bool:
    return _MARKET_THRESHOLD_REQUEST.fullmatch(question) is not None


def requests_break_even_without_later_quote(question: str, *, waiting_for_quote: bool) -> bool:
    """Recognize a price deferral or a standalone threshold request for a saved pair."""
    if _BREAK_EVEN_REQUEST.fullmatch(question):
        return True
    clauses = re.split(r"[.!?]\s*", question.strip().rstrip(".!?"))
    if len(clauses) == 2 and _BREAK_EVEN_REQUEST.fullmatch(clauses[1]):
        question = clauses[0]
    return bool(_LATER_PRICE_UNKNOWN.fullmatch(question) and (
        waiting_for_quote or re.search(r"B\s*안|나중|견적|못\s*받", question, re.I)
    ))


def forbids_historical_pair_price(question: str) -> bool:
    """True only when the user explicitly excludes historical price evidence."""
    return isinstance(question, str) and bool(_HISTORICAL_PRICE_EXCLUSION.search(question))


def without_historical_price_exclusion(question: str) -> str:
    """Omit the source restriction when checking for declined condition writes."""
    return _HISTORICAL_PRICE_EXCLUSION.sub("", question)


def requests_historical_pair_price(question: str) -> bool:
    """Whether the user explicitly names historical price evidence."""
    text = question.casefold()
    if forbids_historical_pair_price(text):
        return False
    return any(word in text for word in (
        "과거 가격", "과거 경락", "과거 시세", "과거 변동", "경락가격", "경락 가격",
        "가격쌍", "시장 시세", "도매 시세", "과거 자료", "과거 근거", "과거가격",
    ))


def requests_manual_price(question: str) -> bool:
    if any(word in question for word in ("직접 입력", "직접입력", "직접 넣", "정한 단가", "내가 입력")):
        return True
    return bool(re.search(r"(?:가격|과거|시장)?\s*(?:자료|데이터)\s*없이\s*(?:손익분기|계산|비교)", question))


def starts_new_comparison(question: str) -> bool:
    return _NEW_COMPARISON.search(question) is not None


def states_price_basis(question: str) -> bool:
    return any(not _PRICE_BASIS_UNCERTAIN.match(question[match.end():]) for match in _PRICE_BASIS.finditer(question))


def continues_comparison(question: str) -> bool:
    return _CONTINUE_COMPARISON.search(question) is not None


def changes_later_date_only(question: str) -> bool:
    return _LATER_DATE_ONLY.search(question) is not None


def explicit_later_storage_period(question: str) -> int | None:
    match = _LATER_STORAGE_EDIT.fullmatch(question)
    if match and (not match["calendar"] or match["storage"]):
        return int(match["days"])
    return None


_SHIPMENT_AVAILABILITY = re.compile(
    r"(?<!\d)(?:(?P<year>\d{4})\s*년\s*)?(?P<month>\d{1,2})\s*(?:월|/)\s*"
    r"(?P<first>\d{1,2})\s*일?\s*(?:부터|[~～–])\s*"
    r"(?:(?P<end_month>\d{1,2})\s*(?:월|/)\s*)?(?P<last>\d{1,2})\s*일?\s*(?:까지|에)?\s*"
    r"(?:는\s*)?(?:출하|납품|인수|팔|낼|보낼)\s*(?:할\s*)?(?:수\s*있|가능)"
)
_SHIPMENT_UNAVAILABLE = re.compile(
    r"(?<![\d/])(?:(?P<year>\d{4})\s*년\s*)?(?:(?P<month>\d{1,2})\s*(?:월|/)\s*)?"
    r"(?P<day>\d{1,2})\s*일\s*(?:에는|은|이|에|을|만)?\s*"
    r"(?:(?:(?:출하|납품|인수|선과|수거)(?:가|는|를)?\s*)?"
    r"(?:못\s*(?:해|하|받|내|팔)|불가|안\s*(?:돼|되|받|해|하))|(?:빼|제외해)\s*(?:줘|주|주세요))"
    r"(?:[가-힣]*)(?=\s*(?:[,.!;。]|$))"
)
_SHIPMENT_DATE_REPLY = re.compile(
    r"\s*(?:(?P<side>[AB])\s*안\s*(?:날짜|출하일)?\s*(?:만|은|을)?\s*)?"
    r"(?:(?P<year>\d{4})\s*년\s*)?(?:(?P<month>\d{1,2})\s*(?:월|/)\s*)?(?P<day>\d{1,2})\s*(?P<day_unit>일)?\s*"
    r"(?:이요|입니다|(?:로|으로)?\s*(?:바꿔|변경해|해|가능해|가능합니다)(?:\s*(?:줘|주세요|요))?)?[.!\s]*",
    re.I,
)


def shipment_availability(question, today, *, anchor=None):
    """Read affirmative availability and explicit exceptions for calendar days."""
    window = None
    if match := _SHIPMENT_AVAILABILITY.search(question):
        year, month = int(match["year"] or today.year), int(match["month"])
        last_month = int(match["end_month"] or month)
        try:
            start = date(year, month, int(match["first"]))
            end = date(year + (last_month < month), last_month, int(match["last"]))
            if today <= start <= end and (end - today).days <= 365:
                window = start, end
        except ValueError:
            pass
    anchor = window[0] if window else anchor or today
    excluded = set()
    for match in _SHIPMENT_UNAVAILABLE.finditer(question):
        month = int(match["month"] or anchor.month)
        year = int(match["year"] or (anchor.year + (anchor.month == 12 and month == 1)))
        try:
            excluded.add(date(year, month, int(match["day"])))
        except ValueError:
            continue
    return window, excluded


def without_shipment_exclusions(question):
    return _SHIPMENT_UNAVAILABLE.sub("", question)


def shipment_date_reply(question, anchor, *, pending_side=None):
    match = _SHIPMENT_DATE_REPLY.fullmatch(question)
    if not match or not (match["month"] or match["day_unit"]) or not (side := (match["side"] or pending_side)):
        return None
    month = int(match["month"] or anchor.month)
    year = int(match["year"] or (anchor.year + (anchor.month == 12 and month == 1)))
    try:
        return side.upper(), date(year, month, int(match["day"]))
    except ValueError:
        return None


_COST_AMOUNT = r"\d+(?:,\d{3})*(?:\.\d+)?\s*(?:만|천)?\s*(?:원)?"
_STORAGE_COST_RANGE = re.compile(
    rf"\s*(?:(?P<side>B\s*안|나중(?:\s*출하)?)\s*(?:의\s*)?)?"
    rf"(?:(?P<period>하루|일일|\d{{1,3}}\s*일(?:치)?|{'|'.join(_DAY_NAMES)}(?:치)?|전체|총)\s*)?"
    rf"(?:보관(?:비|료|\s*비용)(?:는|가|은|이|를)?\s*)?(?:총\s*)?"
    rf"(?:최소\s*)?(?P<lower>{_COST_AMOUNT})\s*(?:[~～–-]|(?:부터|에서))\s*(?:최대\s*)?(?P<upper>{_COST_AMOUNT})"
    r"\s*(?:사이|정도|까지|선)?\s*(?:예요|이에요|입니다|이요|이고|이며|으로\s*봐\s*줘|으로\s*봐\s*주세요|들어요)?[.!\s]*",
    re.I,
)


def _cost_amount(raw):
    number, suffix = re.fullmatch(r"([\d,.]+)\s*([만천]?)\s*원?", raw.strip()).groups()
    return Decimal(number.replace(",", "")), suffix


_COST_SCALE = {"": 1, "만": 10000, "천": 1000}


def later_storage_cost_range(question, *, waiting_for_later_storage=False):
    """Keep the user's unit and both endpoints. A range is never a fixed cost."""
    match = _STORAGE_COST_RANGE.fullmatch(question)
    if not match or "원" not in question:
        return None
    period = (match["period"] or "").replace(" ", "").removesuffix("치")
    days = _DAY_NAMES.get(period)
    if re.fullmatch(r"\d{1,3}일", period):
        days = int(period[:-1])
    if not waiting_for_later_storage and not ((match["side"] or days) and "보관" in question):
        return None
    lower, scale_a = _cost_amount(match["lower"])
    upper, scale_b = _cost_amount(match["upper"])
    if not scale_a and scale_b and "원" not in match["lower"] and lower <= upper:
        scale_a = scale_b
    daily = period in {"하루", "일일"} or (not period and not match["side"] and waiting_for_later_storage)
    return lower * _COST_SCALE[scale_a], upper * _COST_SCALE[scale_b], daily, None if daily else days


def stated_storage_range(question):
    for clause in re.split(r"[;\n]|,(?!\d)|(?<=\.)\s+", question):
        if result := later_storage_cost_range(clause):
            return result
    return None


def confirmed_total_storage_cost(question):
    match = re.fullmatch(rf"\s*(?:총\s*)?({_COST_AMOUNT})\s*(?:입니다|이에요|예요|들어요)?[.!\s]*", question)
    if not match or "원" not in question:
        return None
    amount, scale = _cost_amount(match[1])
    return amount * _COST_SCALE[scale]


def shipment_choice_question(question: str) -> bool:
    """Distinguish a shipping decision from a historical query or report."""
    return bool(_SHIPMENT.search(question) and _CHOICE.search(question)
                and not (_OTHER_PURPOSE.search(question) or _TODAY_SALE.search(question)))


def shipment_window_start(question: str, today: date) -> date | None:
    if not shipment_choice_question(question):
        return None
    matches = list(_CALENDAR_DAY.finditer(question))
    if not matches or not _WINDOW_START.match(question[matches[0].end():]):
        return None
    if len(matches) == 1 or (len(matches) == 2 and question[matches[1].end():].lstrip().startswith("까지")):
        return _calendar_day(matches[0], today.year)
    return None


def shipment_window_limit(question: str, first: date) -> date | None:
    match = _WINDOW_LIMIT.search(question.strip())
    if match is None:
        return None
    from calendar import monthrange

    month = int(match[1]) if match[1] else first.month
    year = first.year + (month < first.month)
    try:
        day = monthrange(year, month)[1] if match[2] == "말일" else int(match[2][:-1])
        return date(year, month, day)
    except ValueError:
        return None


def _calendar_day(match, default_year: int) -> date | None:
    year, month, day = match.groups()[:3] if match[2] else match.groups()[3:]
    try:
        return date(int(year or default_year), int(month), int(day))
    except ValueError:
        return None


def dated_shipment_query(question: str, today: date, varieties) -> dict | None:
    """Resolve an explicit comparison of two calendar days of past shipment volume."""
    if (not re.search(r"과거\s*(?:신고\s*)?출하량", question)
            or not re.search(r"비교|조회|보여|봐\s*줘|알려", question)
            or re.search(r"가격|단가|손익|감모|비용|보관비|수수료|정산|검산|시세|경락|예측|확률|보고서|pdf|"
                         r"작년|재작년|올해|기존|방금|아까|바꿔|수정|말고|제외|하지\s*마|아니면|또는", question, re.I)):
        return None
    matched_varieties = [variety for variety in varieties if variety in question]
    if len(matched_varieties) != 1:
        return None
    matches = list(_CALENDAR_DAY.finditer(question))
    if len(matches) not in (1, 2):
        return None
    dates = [_calendar_day(match, today.year) for match in matches]
    if dates[0] is not None and len(dates) == 1:
        tail = question[matches[0].end():]
        second = re.match(r"\s*(?:과|와|하고|랑|및|,)\s*(\d{1,2})\s*일", tail)
        if second is None:
            return None
        try:
            dates.append(dates[0].replace(day=int(second[1])))
        except ValueError:
            return None
    if any(day is None or day.year < today.year for day in dates):
        return None
    # A separately stated source year must still constrain the model's lookup.
    if any(not any(match.start() <= year.start() < match.end() for match in matches)
           for year in re.finditer(r"\d{4}\s*년", question)):
        return None
    earlier, later = sorted(dates)
    if not 1 <= (later - earlier).days <= 365:
        return None
    return {"shipment_variety": matched_varieties[0], "option_a_date": earlier.isoformat(),
            "option_b_date": later.isoformat()}


def is_future_pair_request(question: str, today: date) -> bool:
    text = question.casefold()
    if not shipment_choice_question(text):
        return False
    # Remove the date of a stated storage start before counting shipment dates.
    dates = []
    for match in _CALENDAR_DAY.finditer(text):
        if (_STORAGE_START.search(text[:match.start()])
                or re.match(r"\s*(?:부터|에\s*보관\s*시작)", text[match.end():])):
            continue
        shipment_day = _calendar_day(match, today.year)
        if shipment_day is not None and shipment_day > today:
            dates.append(shipment_day)
    periods = _relative_ship_days(text)
    future_days = periods | {(shipment_day - today).days for shipment_day in dates}
    return len(future_days) == 2


def _relative_ship_days(text: str) -> set[int]:
    periods = set()
    for days, day_name, weeks, storage_days in _RELATIVE_DAY.findall(text):
        if weeks:
            periods.add(7 * (int(weeks) if weeks.isdigit() else _WEEK_NAMES[weeks]))
        else:
            periods.add(_DAY_NAMES[day_name] if day_name else int(days or storage_days))
    for day in _SHARED_RELATIVE_DAY.findall(text):
        periods.add(_DAY_NAMES[day] if day in _DAY_NAMES else int(day[:-1]))
    return periods


def dated_storage_periods(question: str, today: date) -> tuple[date, int, int] | None:
    """Read two relative shipment dates with one explicitly stated storage start."""
    matches = list(_DATED_STORAGE.finditer(question))
    if len(matches) != 1:
        return None
    match = matches[0]
    periods = sorted(_relative_ship_days(question[match.end():]))
    if len(periods) != 2 or periods[1] > 365:
        return None
    try:
        start = date(int(match[1] or today.year), int(match[2]), int(match[3]))
    except ValueError:
        return None
    return start, *periods


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
