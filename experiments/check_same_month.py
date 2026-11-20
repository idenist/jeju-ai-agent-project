"""대안 실험: 같은 시즌 과거 대신 '이전 시즌의 같은 달' 자료로 센 충족 비율은 예측력이 있는가."""
import statistics
from collections import defaultdict
from decimal import Decimal as D

from experiments.run_experiments import ROOT, SEASONS, auc, load

LOSS_PER_DAY = D("0.1")
rows_by_season = {s: [r for r in load(ROOT / f"data/seasons/{s}/price_comparison_availability.csv")
                      if r["grade"] == "특" and D(r["package_kg"]) == 5 and int(r["interval_days"]) in (7, 14)]
                  for s in SEASONS}
# 품종 표기가 시즌마다 달라(예: 2023-24의 '기타') 크기·간격·달 기준으로 묶는다.
key = lambda r: (r["size"], int(r["interval_days"]), r["base_date"][5:7])
recs = []
for i, season in enumerate(SEASONS[1:], start=1):
    prior = defaultdict(list)
    for s in SEASONS[:i]:
        for r in rows_by_season[s]:
            prior[key(r)].append(D(r["price_change_pct"]))
    for r in rows_by_season[season]:
        past = prior.get(key(r), [])
        if len(past) < 10:
            continue
        k = int(r["interval_days"])
        pa, actual = D(r["base_price_kg"]), D(r["target_price_kg"])
        thr = pa / (1 - LOSS_PER_DAY * k / 100)
        need = (thr / pa - 1) * 100
        recs.append({"score": sum(c >= need for c in past) / len(past), "truth": actual >= thr})
a = auc([r["score"] for r in recs], [r["truth"] for r in recs])
print(f"이전 시즌 같은 달: n={len(recs)} 기저율={statistics.mean(r['truth'] for r in recs):.3f} AUC={a:.3f}")
