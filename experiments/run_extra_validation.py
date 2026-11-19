"""추가 검증: 평가 방법 자체를 믿을 수 있는지까지 확인한다.

V1 손계산 정답셋: 사람이 종이로 계산할 수 있는 작은 사례
V2 양성·음성 대조 시뮬레이션: 정답을 아는 가짜 가격으로 '신호가 있으면 잡고, 없으면 안 잡는지' 확인
V3 블록 부트스트랩: AUC 0.46이 우연 범위인지 신뢰구간으로 확인
V4 정답 정의 바꾸기: '기다린 게 나았다'의 기준을 ±1% 흔들어도 결론이 같은지
V5 연속값 검증: 맞음/틀림 대신 실제 손익 금액과의 순위 상관(Spearman)
V6 원인 확인: 직전 7일 변동과 다음 7일 변동의 상관(평균 회귀 여부)

실행: python -X utf8 -m experiments.run_extra_validation  (결과: experiments/extra_results.json)
"""
import json
import math
import random
import statistics
from collections import defaultdict
from datetime import date
from decimal import Decimal as D

from experiments.run_experiments import ROOT, SEASONS, load, spec_of
from services.past_reach import past_pairs_reaching


def auc(scores, labels):
    """순위 기반 AUC(Mann-Whitney). 동점은 평균 순위로 처리한다."""
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        for t in range(i, j + 1):
            ranks[order[t]] = (i + j) / 2 + 1
        i = j + 1
    pos = sum(labels)
    neg = len(labels) - pos
    if not pos or not neg:
        return None
    return (sum(r for r, y in zip(ranks, labels) if y) - pos * (pos + 1) / 2) / (pos * neg)


def spearman(x, y):
    rank = lambda v: [sorted(v).index(a) for a in v]
    rx, ry = rank(x), rank(y)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    return cov / math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))


# ---------------------------------------------------------------- V1 손계산 정답셋
HAND_CASES = [
    # (설명, 과거 변동률들, A단가, 손익분기, 손으로 센 충족 쌍)
    ("비용 없음: 손익분기 = A단가, 0% 이상만 충족", ["-10", "0", "5", "20"], 1000, "1000", 3),
    ("손익분기 +10%: 경계값 10은 충족, 9.99는 미달", ["9.99", "10", "10.01", "-1"], 1000, "1100", 2),
    ("손익분기가 A단가보다 낮음(-5%): 내려도 충족", ["-6", "-5", "-4.99", "3"], 2000, "1900", 3),
    ("모두 미달", ["-3", "-2", "-1"], 1500, "1600", 0),
    ("모두 충족", ["30", "40"], 1500, "1600", 2),
]


def v1_hand():
    rows = []
    for name, changes, pa, thr, expected in HAND_CASES:
        got = past_pairs_reaching(changes, pa, thr, 7)["reached"]
        rows.append({"사례": name, "손계산": expected, "시스템": got, "일치": got == expected})
    # 문서 예시(2,552.08원)도 손으로 다시 계산한다: 2,940×2,500×0.95 ÷ (2,880×0.95)
    thr = D(2940) * 2500 * D("0.95") / (D(2880) * D("0.95"))
    rows.append({"사례": "문서 예시 손익분기", "손계산": "2552.08", "시스템": str(thr.quantize(D("0.01"))),
                 "일치": str(thr.quantize(D("0.01"))) == "2552.08"})
    return {"통과": sum(r["일치"] for r in rows), "전체": len(rows), "사례": rows}


# ---------------------------------------------------------------- V2 양성·음성 대조 시뮬레이션
def simulate(kind, seasons=60, days=150, k=7, seed=11):
    """정답(가격 생성 규칙)을 아는 가짜 시즌을 만들어 같은 평가를 돌린다."""
    rng = random.Random(seed)
    scores, labels = [], []
    for _ in range(seasons):
        price, prices = 2500.0, []
        drift = rng.choice([-0.006, 0.006]) if kind == "추세 지속(신호 있음)" else 0.0
        level = math.log(2500)
        for t in range(days):
            if kind == "평균 회귀":
                # 장기 평균으로 끌려가는 가격(OU 과정)
                level += 0.15 * (math.log(2500) - level) + rng.gauss(0, 0.04)
                price = math.exp(level)
            else:
                price *= math.exp(drift + rng.gauss(0, 0.03))
            prices.append(price)
        pairs = [(t, t + k, (prices[t + k] / prices[t] - 1) * 100) for t in range(days - k)]
        for t, _, _ in pairs:
            past = [str(c) for (b, e, c) in pairs if e < t]
            if len(past) < 10:
                continue
            pa = prices[t]
            thr = pa / (1 - 0.007)  # 7일 감모 0.7%만 있는 단순 비용
            reach = past_pairs_reaching(past, pa, thr, k)
            scores.append(reach["reached"] / reach["total"])
            labels.append(prices[t + k] >= thr)
    return {"결정 수": len(scores), "AUC": round(auc(scores, labels), 3)}


def v2_controls():
    return {kind: simulate(kind) for kind in ("추세 지속(신호 있음)", "무작위 보행(신호 없음)", "평균 회귀")}


# ---------------------------------------------------------------- 실제 자료 기록 만들기
def real_records(loss_per_day=D("0.1"), margin=D(0)):
    recs = []
    for season in SEASONS:
        by = defaultdict(list)
        for r in load(ROOT / f"data/seasons/{season}/price_comparison_availability.csv"):
            if r["grade"] == "특" and D(r["package_kg"]) == 5 and int(r["interval_days"]) in (7, 14):
                by[(spec_of(r), int(r["interval_days"]))].append(r)
        for (spec, k), group in by.items():
            group.sort(key=lambda r: r["base_date"])
            for idx, r in enumerate(group):
                d = date.fromisoformat(r["base_date"])
                past_rows = [p for p in group if date.fromisoformat(p["target_date"]) < d]
                if len(past_rows) < 10:
                    continue
                pa, actual = D(r["base_price_kg"]), D(r["target_price_kg"])
                thr = pa / (1 - loss_per_day * k / 100)
                reach = past_pairs_reaching([p["price_change_pct"] for p in past_rows], pa, thr, k)
                last = max(past_rows, key=lambda p: p["target_date"])
                recs.append({"group": (season, spec, k), "score": reach["reached"] / reach["total"],
                             # 정답 정의: 실제 가격이 손익분기 × (1 + margin) 이상이면 '기다린 게 나았다'
                             "truth": actual >= thr * (1 + margin / 100),
                             "gain": float(actual - thr), "last_change": float(last["price_change_pct"]),
                             "next_change": float(r["price_change_pct"])})
    return recs


# ---------------------------------------------------------------- V3 블록 부트스트랩
def v3_bootstrap(recs, n_boot=400, seed=3):
    rng = random.Random(seed)
    groups = defaultdict(list)
    for r in recs:
        groups[r["group"]].append(r)
    keys = list(groups)
    values = []
    for _ in range(n_boot):
        # 날짜가 겹치는 결정끼리는 묶어서(규격·간격·시즌 단위) 통째로 다시 뽑는다.
        sample = [r for key in (rng.choice(keys) for _ in keys) for r in groups[key]]
        a = auc([r["score"] for r in sample], [r["truth"] for r in sample])
        if a is not None:
            values.append(a)
    values.sort()
    lo, hi = values[int(0.025 * len(values))], values[int(0.975 * len(values)) - 1]
    point = auc([r["score"] for r in recs], [r["truth"] for r in recs])
    return {"AUC": round(point, 3), "95% 신뢰구간": [round(lo, 3), round(hi, 3)],
            "0.5가 구간 안에 있나": lo <= 0.5 <= hi, "묶음 수": len(keys)}


# ---------------------------------------------------------------- V4 정답 정의 바꾸기
def v4_label_sensitivity():
    out = {}
    for margin in (D(-1), D(0), D(1)):
        for loss in (D("0.05"), D("0.1"), D("0.2")):
            recs = real_records(loss_per_day=loss, margin=margin)
            out[f"여유 {margin:+}% · 감모 {loss}%/일"] = round(
                auc([r["score"] for r in recs], [r["truth"] for r in recs]), 3)
    return out


# ---------------------------------------------------------------- V5·V6
def v5_v6(recs):
    sample = recs[::5]  # Spearman 계산량을 줄이려고 5개마다 하나만 쓴다(결정 순서는 섞이지 않음).
    rho_gain = spearman([r["score"] for r in sample], [r["gain"] for r in sample])
    rho_rev = spearman([r["last_change"] for r in sample], [r["next_change"] for r in sample])
    return {"V5 충족 비율 vs 실제 손익(Spearman)": round(rho_gain, 3),
            "V6 직전 변동 vs 다음 변동(Spearman)": round(rho_rev, 3),
            "표본 수": len(sample)}


def main():
    recs = real_records()
    results = {"V1 손계산 정답셋": v1_hand(), "V2 대조 시뮬레이션": v2_controls(),
               "V3 블록 부트스트랩": v3_bootstrap(recs), "V4 정답 정의 바꾸기": v4_label_sensitivity(),
               "V5·V6 연속값·원인": v5_v6(recs)}
    path = ROOT / "experiments/extra_results.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
