"""Check quantitative historical claims against the current structured lookup."""

import re
from decimal import Decimal, ROUND_HALF_UP
from agents.box_price import package_weight_offsets

_LOOKUPS = {"analyze_shipment_timing", "get_historical_shipment_context",
            "get_price_history", "get_price_overview", "get_market_evidence"}
_CONTEXT = re.compile(r"출하량|경락|가격쌍|관측|조회|기록|자료|몇\s*해|몇\s*년|중앙값|거래중량")
_CLAUSE = re.compile(r"[!?。\n]|\.(?!\d)")
_YEAR = re.compile(r"(?<!\d)(20\d{2})\s*년")
_YEAR_RANGE = re.compile(r"(?<!\d)(20\d{2})\s*(?:년\s*)?[~～–-]\s*(20\d{2})\s*년")
_PLANNED_SHIPMENT = re.compile(
    r"(?P<side>[AB])\s*안\s*(?:\(\s*|출하\s*예정일\s*(?:은|이|:)?\s*)"
    r"(?P<day>(?:20\d{2}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일|20\d{2}-\d{2}-\d{2}))"
    r"(?=\s*(?:주변|전후|출하|\)|로|입니다))", re.I,
)
_COUNT = re.compile(r"(?<![\d.,])(\d[\d,]*)\s*(개\s*연도|개년|년간|쌍)")
_RATIO = re.compile(r"(\d[\d,]*)\s*(?:개(?:년)?|년)\s*중\s*(\d[\d,]*)\s*(?:개(?:년)?|년|해)|"
                    r"(?<![\d/])(\d+)\s*/\s*(\d+)\s*(?:개(?:년)?|년|해)")
_MASS = re.compile(r"(?<![\d.])([+-]?\d[\d,]*(?:\.\d+)?)\s*(톤|kg|킬로그램)(?![A-Za-z])", re.I)
_FARM_MASS = re.compile(
    r"(?:비교|입력|보유|가정|(?:우리|저희|제)\s*(?:농가의?\s*)?)\s*(?:물량|출하량|판매량|보관량)"
    r"\s*(?:은|는|이|가|을|를|:|=)?\s*$"
)
_OBSERVATION_MASS = re.compile(r"출하량|중앙값|관측|거래\s*(?:중량|물량)|반입량")
_EXAMPLE = re.compile(r"예를\s*들|예시")
_OBSERVED_STATUSES = {"PARTIAL", "PARTIAL_DATA", "INSUFFICIENT_DATA", "OK"}
_SIDE = re.compile(r"([AB])\s*안|먼저\s*(?:출하|날짜)|나중\s*(?:출하|날짜)", re.I)
_MEDIAN = re.compile(r"중앙값|중앙\s*수치")
_MORE = re.compile(r"더\s*많|더\s*높|우세|많았|넘|높았")
_PROBABILITY_NUMBER = r"(?:\d[\d,.]*\s*(?:%|퍼센트)|\d+\s*(?:/|분의)\s*\d+)"
_PROBABILITY = re.compile(
    rf"(?:확률|가능성)[^%.!?\n]{{0,22}}{_PROBABILITY_NUMBER}|"
    rf"{_PROBABILITY_NUMBER}[^.!?\n]{{0,18}}(?:확률|가능성)"
)


def latest_lookup(entries):
    for entry in reversed(entries):
        if entry["tool"] in _LOOKUPS or (
                entry["tool"] == "compare_future_pair" and
                (entry["result"].get("history_preview") or entry["result"].get("historical_price_lookup") == "used")):
            return entry
    return None


def lookup_revision(entry):
    from services.task_context import data_revision

    revision = data_revision()
    if entry["tool"] not in {"analyze_shipment_timing", "get_historical_shipment_context"}:
        from services.future_pair import price_revision
        return price_revision()
    from services.shipment_timing_bundle import source_fingerprint

    shipment_revision = (*revision, *source_fingerprint())
    if entry["tool"] == "analyze_shipment_timing":
        return shipment_revision
    from core.config import PROJECT_ROOT
    from services.market_snapshot import digest

    try:
        return (*shipment_revision, digest(PROJECT_ROOT / "data/processed/monthly_shipments.csv"))
    except OSError:
        return (*shipment_revision, "missing_monthly_shipments")


def _integer(value):
    return int(value.replace(",", ""))


def _years(result):
    observed = {row["reference_year"] for row in result.get("years", ())}
    observed.update(result.get("years_covered", ()))
    observed.update(int(month[:4]) for month in result.get("coverage", {}).get("observed_months", ()))
    if result.get("observed_date"):
        observed.add(int(result["observed_date"][:4]))
    return observed


def _side_of(text):
    sides = set()
    for match in _SIDE.finditer(text):
        sides.add(match[1].upper() if match[1] else "A" if match[0].startswith("먼저") else "B")
    return sides.pop() if len(sides) == 1 else None


def _count_mismatch(clause, result, historical, observed):
    count = result.get("paired_year_count", len(observed))
    summary = result.get("summary", {})
    side = _side_of(clause)
    higher = {summary[key] for key in ("a_higher_years", "b_higher_years") if key in summary}
    if side and _MORE.search(clause):
        higher.intersection_update({summary.get(f"{side.lower()}_higher_years")})
    for match in _RATIO.finditer(clause) if "paired_year_count" in result else ():
        total, part = (_integer(match[1]), _integer(match[2])) if match[1] else (_integer(match[4]), _integer(match[3]))
        if total != count or part not in higher:
            return True
        percent = re.match(r"\s*\(\s*(\d{1,3}(?:\.\d{1,6})?)\s*[%％]\s*\)", clause[match.end():])
        if percent:
            claimed = Decimal(percent[1])
            step = Decimal(1).scaleb(claimed.as_tuple().exponent)
            expected = (Decimal(part) * 100 / total).quantize(step, rounding=ROUND_HALF_UP) if total else None
            if claimed != expected:
                return True
    # Once a ratio is checked, its two numbers need no independent match.
    remaining = _RATIO.sub("", clause)
    for match in _COUNT.finditer(remaining):
        claimed = _integer(match[1])
        if match[2] == "쌍":
            if historical.get("clean_pairs") is not None and claimed != historical["clean_pairs"]:
                return True
        elif "paired_year_count" in result or "years_covered" in result or observed:
            describes_sample = re.match(r"\s*(?:의\s*)?(?:비교|자료|기록|관측|범위)", remaining[match.end():])
            expected = higher if higher and _MORE.search(clause) and not describes_sample else {count}
            if claimed not in expected:
                return True
    return False


def _mass_values(result, clause, side, named_years):
    summary = result.get("summary", {})
    if result.get("analysis_type") == "shipment_timing":
        field = ("a_tons", "b_tons") if side is None else (f"{side.lower()}_tons",)
        if _MEDIAN.search(clause):
            keys = (f"{s}_median_7day_tons" for s in ("a", "b") if side is None or side.lower() == s)
            return [Decimal(summary[key]) * 1000 for key in keys if summary.get(key) is not None]
        return [Decimal(row[key]) * 1000 for row in result.get("years", ())
                if not named_years or row["reference_year"] in named_years for key in field]
    if "series" in result and "years_covered" in result:
        if _MEDIAN.search(clause):
            value = summary.get("median")
            return [Decimal(value) * 1000] if value is not None else []
        return [Decimal(row["shipment_tons"]) * 1000 for row in result["series"]
                if not named_years or row["year"] in named_years]
    if "groups" in result or "total_kg" in result:
        return [Decimal(row["total_kg"]) for row in result.get("groups", [result]) if row.get("total_kg") is not None]
    return None


def _observed_masses(clause):
    box_weights = package_weight_offsets(clause)
    for match in _MASS.finditer(clause):
        before = clause[:match.start()]
        if match.start(1) in box_weights or _FARM_MASS.search(before):
            continue
        if _EXAMPLE.search(before) and not _OBSERVATION_MASS.search(before):
            continue
        yield match


def _observation_year_text(clause, result):
    if result.get("analysis_type") != "shipment_timing":
        return clause
    dates = result.get("input", {})

    def distinguish_planned(match):
        year, month, day = (int(part) for part in re.findall(r"\d+", match["day"]))
        shown_date = f"{year:04d}-{month:02d}-{day:02d}"
        expected = dates.get(f"option_{match['side'].lower()}_date")
        if shown_date == expected:
            return match[0].replace(match["day"], "출하 예정일")
        return match[0]

    return _PLANNED_SHIPMENT.sub(distinguish_planned, clause)


def rejected_evidence_claim(text, question, entry):
    """Return the reason for rejecting model prose, never application results."""
    text = text.replace("**", "").replace("__", "").replace("`", "")
    if _PROBABILITY.search(text) and (entry is not None or re.search(r"가격|출하량|경락|자료", question + " " + text)):
        return "historical_probability"
    if entry is None or not _CONTEXT.search(question + " " + text):
        return None
    result = entry["result"]
    observed = _years(result)
    historical = result.get("history_preview") or next(
        (item for item in result.get("evidence", {}).values() if "clean_pairs" in item), result
    )
    available = result.get("status", "OK") in _OBSERVED_STATUSES or bool(result.get("history_preview"))
    for clause in _CLAUSE.split(text):
        masses = tuple(_observed_masses(clause))
        if not available:
            if (masses and _OBSERVATION_MASS.search(clause)) or any(
                    _integer(match[1]) != 0 for match in _COUNT.finditer(clause)):
                return "missing_observation"
            continue
        year_text = _observation_year_text(clause, result)
        named_years = {int(match[1]) for match in _YEAR.finditer(year_text)}
        for match in _YEAR_RANGE.finditer(year_text):
            named_years.update(range(int(match[1]), int(match[2]) + 1))
        if (named_years and (observed or "paired_year_count" in result or "years_covered" in result)
                and not named_years <= observed):
            return "observed_year"
        if _count_mismatch(clause, result, historical, observed):
            return "sample_count"
        previous_end = 0
        for match in masses:
            local_side = _side_of(clause[previous_end:match.start()]) or _side_of(clause)
            previous_end = match.end()
            values = _mass_values(result, clause, local_side, named_years)
            if values is None:
                continue
            scale = Decimal(1000 if match[2] == "톤" else 1)
            claimed = Decimal(match[1].replace(",", ""))
            step = Decimal(1).scaleb(claimed.as_tuple().exponent)
            if claimed.adjusted() > 15 or claimed.as_tuple().exponent < -12 or not any(
                    (value / scale).quantize(step, rounding=ROUND_HALF_UP) == claimed for value in values):
                return "observed_mass"
    return None
