"""시즌 자료로 B안 가정 단가(price_scenario)를 재현·검증한다.

실행: python -X utf8 scripts/check_season_scenarios.py [--out 결과.md]
앱은 시즌 자료를 자동 조회하지 않으므로, 여기서는 시즌 경로를 직접 지정해 같은 함수를 호출한다.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.jeonghyeon.price_scenario import execute_scenario_tool  # noqa: E402

SEASONS = ("2024-2025", "2025-2026")
SPEC = {"variety": "조생귤", "grade": "특", "size": "소", "package": "상자", "package_kg": "5"}
A_PRICE = 2500
# 판매일 자체는 계산에 쓰이지 않고 간격만 쓰인다. 날짜는 임의의 고정값.
A_DATE = "2026-11-20"
B_DATES = {3: "2026-11-23", 7: "2026-11-27", 10: "2026-11-30", 14: "2026-12-04"}
SPEC_KEYS = ("market_code", "variety", "grade", "size", "package", "package_kg", "trade_type")


def paths(season: str) -> dict:
    base = ROOT / "data/seasons" / season
    return {"data_file": base / "price_comparison_availability.csv",
            "prices_file": base / "daily_prices.csv",
            "report_file": base / "보고서/price_comparison_report.json"}


def run(season: str, interval: int, spec: dict = SPEC) -> dict:
    args = {**spec, "option_a_sale_date": A_DATE, "option_b_sale_date": B_DATES[interval],
            "option_a_price_per_kg": A_PRICE}
    return execute_scenario_tool(args, **paths(season))


def recompute(season: str, interval: int, conditions: dict) -> dict:
    """도구를 거치지 않고 CSV에서 직접 다시 계산해 도구 결과와 대조한다."""
    rows = []
    with open(paths(season)["data_file"], encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            # 경고 있는 사례(COMPARABLE_WITH_WARNINGS)는 도구와 같게 제외한다.
            if r["comparison_status"] != "COMPARABLE" or int(r["interval_days"]) != interval:
                continue
            if all((Decimal(r[k]) == Decimal(v)) if k == "package_kg" else r[k] == v
                   for k, v in conditions.items()):
                rows.append(r)
    changes = sorted(Decimal(r["price_change_pct"]) for r in rows)
    price = lambda pct: (Decimal(A_PRICE) * (1 + pct / 100)).quantize(Decimal("0.01"))
    return {"pairs": len(rows),
            "range": [min(r["base_date"] for r in rows), max(r["base_date"] for r in rows)] if rows else None,
            "prices": [price(p) for p in (changes[0], median(changes), changes[-1])] if rows else None}


def spec_counts(season: str, interval: int) -> list[int]:
    """규격별 경고 없는 사례 수. 표본 부족 경고 기준을 정할 때 근거로 쓴다."""
    counter = Counter()
    with open(paths(season)["data_file"], encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r["comparison_status"] == "COMPARABLE" and int(r["interval_days"]) == interval:
                counter[tuple(r[k] for k in SPEC_KEYS)] += 1
    return sorted(counter.values())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    lines, ok = [], True
    lines += ["# 시즌 자료 B안 가정 단가 검증", "",
              f"조건: {SPEC['variety']}·{SPEC['grade']}·{SPEC['size']}·{SPEC['package']} {SPEC['package_kg']}kg, "
              f"A안 {A_PRICE:,}원/kg. 도구 결과와 CSV 직접 재계산을 대조한다.", "",
              "| 시즌 | 간격 | 상태 | 경고 없는 쌍 | 기준일 범위 | 최저 | 중앙 | 최고 | 재계산 일치 |",
              "|---|---|---|---|---|---|---|---|---|"]
    for season in SEASONS:
        for interval in (3, 7, 14):
            result = run(season, interval)
            if result["status"] != "OK":
                lines.append(f"| {season} | {interval}일 | {result['status']} | - | - | - | - | - | - |")
                ok = False
                continue
            prices = [Decimal(s["option_b_price_per_kg"]) for s in result["scenarios"]]
            check = recompute(season, interval, result["conditions"])
            same = (check["pairs"] == result["clean_pairs"] and check["range"] == result["base_date_range"]
                    and check["prices"] == prices)
            ok &= same
            start, end = result["base_date_range"]
            lines.append(f"| {season} | {interval}일 | OK | {result['clean_pairs']} | {start}~{end} | "
                         + " | ".join(f"{p:,.2f}" for p in prices) + f" | {'O' if same else 'X'} |")

    lines += ["", "## 자료가 없을 때", "",
              "| 시즌 | 경우 | 상태 | B안 단가 | 직접 입력 안내 |", "|---|---|---|---|---|"]
    for season in SEASONS:
        for label, interval, spec, expected in (
                ("10일 간격(자료에 없는 간격)", 10, SPEC, "INTERVAL_NOT_AVAILABLE"),
                ("자료에 없는 크기", 7, {**SPEC, "size": "없는크기"}, "NO_DATA")):
            result = run(season, interval, spec)
            has_price = bool(result.get("scenarios"))
            manual = bool(result.get("manual_price_allowed"))
            # 기대한 상태여야 하고, 가정 단가는 비어 있으며, 직접 입력 안내가 반드시 있어야 통과한다.
            ok &= result["status"] == expected and not has_price and manual
            lines.append(f"| {season} | {label} | {result['status']} | {'있음' if has_price else '없음'} | "
                         f"{'있음' if manual else '없음'} |")

    lines += ["", "## 규격별 경고 없는 사례 수 (표본 부족 경고 기준 참고)", "",
              "| 시즌 | 간격 | 규격 수 | 최소 | 중앙 | 최대 | 5쌍 미만 | 10쌍 미만 | 20쌍 미만 |",
              "|---|---|---|---|---|---|---|---|---|"]
    for season in SEASONS:
        for interval in (3, 7, 14):
            counts = spec_counts(season, interval)
            if not counts:
                continue
            below = lambda n: f"{sum(c < n for c in counts)} ({sum(c < n for c in counts) / len(counts):.0%})"
            lines.append(f"| {season} | {interval}일 | {len(counts)} | {counts[0]} | {median(counts):g} | "
                         f"{counts[-1]} | {below(5)} | {below(10)} | {below(20)} |")
    lines += ["", f"전체 판정: {'통과' if ok else '확인 필요'}"]
    text = "\n".join(lines) + "\n"
    print(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
