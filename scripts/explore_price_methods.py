"""같은 규격의 감귤 가격을 세 방식으로 비교하는 학습 실험."""
from argparse import ArgumentParser
from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median

from build_price_comparisons import LABEL_FIELDS, fixed, load_prices


ROOT = Path(__file__).resolve().parents[1]
METHODS = ("정확히 7일", "가까운 거래일", "일주일 가중평균")


def weighted_price(rows):
    # 거래량이 다른 날의 평균을 다시 단순 평균하면 kg당 전체 가격과 달라진다.
    amount = sum(Decimal(r.values["total_amount_krw"]) for r in rows)
    return amount / sum(r.kg for r in rows)


def compare(prices, tolerance, min_days):
    index = {(p.day, p.key): p for p in prices}
    market_days = defaultdict(set)
    for p in prices:
        market_days[p.values["market_code"]].add(p.day)
    bounds = {market: (min(days), max(days)) for market, days in market_days.items()}

    def eligible(base, other):
        return other is not None and not other.warnings and all(
            base.values[k] == other.values[k] for k in LABEL_FIELDS
        )

    for base in prices:
        if base.warnings:
            continue
        target = base.day + timedelta(days=7)
        start, end = bounds[base.values["market_code"]]
        if target > end:
            continue
        exact = index.get((target, base.key))
        # 날짜 오차가 같으면 앞선 관측일을 선택한다.
        offsets = sorted(range(-tolerance, tolerance + 1), key=lambda n: (abs(n), n))
        nearby = None
        for offset in offsets:
            candidate = index.get((target + timedelta(days=offset), base.key))
            if eligible(base, candidate):
                nearby = candidate
                break

        weeks = []
        if base.day - timedelta(days=6) >= start:
            for first in (-6, 1):
                rows = []
                for offset in range(first, first + 7):
                    row = index.get((base.day + timedelta(days=offset), base.key))
                    if eligible(base, row):
                        rows.append(row)
                weeks.append(rows)

        result = {}
        for method, row in zip(METHODS[:2], (exact, nearby)):
            if eligible(base, row):
                result[method] = ((row.price / base.price - 1) * 100,
                                  (row.day - base.day).days)
        if weeks and all(len(week) >= min_days for week in weeks):
            before, after = map(weighted_price, weeks)
            result[METHODS[2]] = ((after / before - 1) * 100, None)
        yield base, result


def report(prices, tolerance, min_days):
    results = list(compare(prices, tolerance, min_days))
    focus = [(base, methods) for base, methods in results if (
        base.values["market_code"], base.values["variety"], base.values["grade"],
        base.values["size"], base.values["package"], base.key[5], base.values["trade_type"]
    ) == ("110001", "조생귤", "특", "소", "상자", Decimal(5), "경매")]
    focus_keys = {base.key for base, _ in focus}
    if len(focus_keys) > 1:
        raise ValueError("대표 조건에 여러 코드 조합이 있습니다.")
    lines = [
        "# 감귤 가격 비교 방법 실험",
        "",
        f"입력 {len(prices):,}행. 관측 범위 {prices[0].day} ~ {prices[-1].day}.",
        "기준 가격에 품질 경고가 없고, 정확히 7일 뒤가 시장 관측기간 안인 행을 공통 분모로 삼았다.",
        "세 방법 모두 시장·품종·등급·크기·포장 종류·포장 중량·거래 방식이 같은 자료만 사용했다.",
        "",
        "| 방법 | 계산 규칙 | 답할 수 있는 질문 |",
        "|---|---|---|",
        "| 정확히 7일 | 기준일과 7일 뒤의 원/kg 가격 비교 | 정확히 7일 사이 얼마가 달라졌나? |",
        f"| 가까운 거래일 | 목표일 앞뒤 {tolerance}일 이내, 경고 없는 가장 가까운 관측일. 동률이면 앞선 날 | 목표일 근처에 가격이 어땠나? |",
        f"| 일주일 가중평균 | 기준일 포함 직전 7일과 다음 7일 각각 총금액 ÷ 총중량. 주마다 최소 {min_days}개 관측일 필요 | 두 주간 거래 가격 수준이 어떻게 달랐나? |",
        "",
        "| 범위 | 기준 행 | 방법 | 비교 가능 건수 | 변동률 중앙값 | 최소 ~ 최대 |",
        "|---|---:|---|---:|---:|---:|",
    ]
    for scope, sample in [("전체", results), ("서울가락 조생귤·특·소·상자 5kg 경매", focus)]:
        for method in METHODS:
            changes = [m[method][0] for _, m in sample if method in m]
            mid = f"{fixed(median(changes), 2)}%" if changes else "—"
            span = f"{fixed(min(changes), 2)}% ~ {fixed(max(changes), 2)}%" if changes else "—"
            lines.append(f"| {scope} | {len(sample):,} | {method} | {len(changes):,} | {mid} | {span} |")
    exact, nearby, weekly = METHODS
    gaps = Counter(m[nearby][1] for _, m in focus if nearby in m)
    both = [(b, m) for b, m in focus if exact in m and weekly in m]
    flipped = [(b, m) for b, m in both if m[exact][0] * m[weekly][0] < 0]
    lines += ["", "대표 규격의 가까운 거래일 비교 실제 간격은 " +
              (", ".join(f"{n}일 {count}건" for n, count in sorted(gaps.items())) or "자료 없음") + ".",
              f"대표 규격에서 정확히 7일과 일주일 평균을 모두 계산한 기준일은 {len(both)}개였다. 그중 상승·하락 방향이 반대인 기준일은 {len(flipped)}개였다."]
    if flipped:
        b, m = flipped[0]
        lines.append(f"예를 들어 {b.day} 기준으로 정확히 7일 비교는 {fixed(m[exact][0], 2)}%, 일주일 가중평균 비교는 {fixed(m[weekly][0], 2)}%였다.")
    lines += [
        "",
        "## 직접 바꿔 보기",
        "",
        "`python scripts/explore_price_methods.py --tolerance 1 --min-days 4`",
        "",
        "가까운 거래일 범위를 줄이면 추가 확보되는 사례가 몇 건인지 확인해 보자.",
        "일주일에 필요한 관측일을 늘리면 결과가 얼마나 줄어드는지도 비교해 보자.",
        "",
        "총금액 ÷ 총중량을 쓰는 이유는 거래량을 반영하기 위해서다. 100원/kg으로 1kg, "
        "200원/kg으로 9kg이 거래됐다면 단순 평균은 150원/kg, 가중평균은 190원/kg이다.",
        "",
        "출하 시나리오에서 기간이 정확히 7일이라는 조건을 유지하려면 정확한 날짜 비교가 적합하다. "
        "가까운 날의 거래는 실제 간격을 붙여 참고로 표시하고, 두 주의 평균은 기간별 흐름 설명에 쓴다.",
        "방법마다 비교 가능한 기준 행이 다르므로 전체 범위의 차이를 방법의 효과로 해석하기 어렵다. "
        "한 시장의 관측 자료이며 기준일 구간이 겹친다. 결과는 과거 사례 설명에 사용한다.",
    ]
    return "\n".join(lines) + "\n"


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data/processed/daily_prices.csv")
    parser.add_argument("--tolerance", type=int, choices=range(4), default=2)
    parser.add_argument("--min-days", type=int, choices=range(1, 8), default=3)
    args = parser.parse_args()
    prices, _ = load_prices(args.input)
    print(report(prices, args.tolerance, args.min_days), end="")


if __name__ == "__main__":
    main()
