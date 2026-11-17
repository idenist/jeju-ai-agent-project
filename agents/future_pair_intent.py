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
_CALENDAR_DAY = re.compile(r"(?<!\d)(?:(\d{4})\s*년\s*)?(\d{1,2})\s*월\s*(\d{1,2})\s*일|(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
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
    r"\s*(?:나중\s*출하일|B\s*안\s*(?:출하일|날짜))만\s*(\d{1,3})\s*일\s*보관"
    r"\s*(?:하는\s*날(?:짜)?)?\s*(?:으?로)\s*(?:바꿔|변경해|수정해)\s*(?:줘(?:요)?|주세요)?[.!\s]*",
    re.I,
)
_HISTORICAL_PRICE = re.compile(r"(?:과거\s*(?:경락\s*)?(?:가격|시세|자료|변동)|경락\s*가격|시장\s*(?:시세|자료)|도매\s*시세|과거가격|자료)")
_EXCLUDE_HISTORICAL_PRICE = re.compile(
    r"(?:을|를|은|는|도|와|과)?\s*(?:조회\s*(?:하지\s*말|안\s*(?:해|봐|하고)|하지\s*않)|"
    r"안\s*(?:보고|보|쓰고|쓰|사용하고|사용|참고하고|참고|조회하고|조회)|"
    r"(?:보지|쓰지|사용하지|참고하지|비교하지|조회하지|활용하지)\s*(?:말|마|않)|"
    r"필요\s*없|없이|빼고|제외)"
)


def forbids_historical_pair_price(question: str) -> bool:
    """True only when the user explicitly excludes historical price evidence."""
    if not isinstance(question, str):
        return False
    return any(_EXCLUDE_HISTORICAL_PRICE.match(question[match.end():].lstrip())
               for match in _HISTORICAL_PRICE.finditer(question.casefold()))


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
    return int(match[1]) if match else None


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
