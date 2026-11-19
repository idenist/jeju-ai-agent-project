"""E3 결과(충족 비율의 예측력 없음)가 창 크기·시즌·간격에 따라 바뀌는지 확인한다."""
import statistics
from collections import defaultdict
from datetime import date
from decimal import Decimal as D

from experiments.run_experiments import ROOT, SEASONS, auc, load, spec_of
from services.past_reach import past_pairs_reaching


def records(window=None, fee=D(5), loss_per_day=D("0.1")):
    out = []
    for season in SEASONS:
        by = defaultdict(list)
        for r in load(ROOT / f"data/seasons/{season}/price_comparison_availability.csv"):
            if r["grade"] == "특" and D(r["package_kg"]) == 5 and int(r["interval_days"]) in (7, 14):
                by[(spec_of(r), int(r["interval_days"]))].append(r)
        for (spec, k), group in by.items():
            for r in group:
                d = date.fromisoformat(r["base_date"])
                past = sorted((p for p in group if date.fromisoformat(p["target_date"]) < d), key=lambda p: p["target_date"])
                if window:
                    # 앱처럼 최근 몇십 쌍만 쓰는 경우를 흉내 낸다.
                    past = past[-window:]
                if len(past) < 10:
                    continue
                pa, actual = D(r["base_price_kg"]), D(r["target_price_kg"])
                loss = loss_per_day * k / 100
                thr = pa / (1 - loss)
                reach = past_pairs_reaching([p["price_change_pct"] for p in past], pa, thr, k)
                out.append({"season": season, "k": k, "month": r["base_date"][5:7],
                            "score": reach["reached"] / reach["total"], "truth": actual >= thr})
    return out


def show(name, recs):
    a = auc([r["score"] for r in recs], [r["truth"] for r in recs])
    print(f"{name:28s} n={len(recs):5d} 기저율={statistics.mean(r['truth'] for r in recs):.3f} AUC={a:.3f}")


all_recs = records()
show("전체(확장 창)", all_recs)
show("최근 20쌍 창", records(window=20))
for s in SEASONS:
    show(f"시즌 {s}", [r for r in all_recs if r["season"] == s])
for k in (7, 14):
    show(f"간격 {k}일", [r for r in all_recs if r["k"] == k])
months = defaultdict(list)
for r in all_recs:
    months[r["month"]].append(r["truth"])
print("월별 '기다린 게 나았던 비율':", {m: round(statistics.mean(v), 2) for m, v in sorted(months.items())})
