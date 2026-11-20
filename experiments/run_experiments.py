"""판단 돕기 기능 실험 모음 (API 키 없이 실행).

E1 계산 정답셋: CSV에서 직접 계산한 독립 구현과 시스템 출력이 같은지
E2 성질(메타모픽) 검증: 입력을 정해진 방향으로 바꾸면 결과도 그 방향으로만 움직이는지
E3 판단 백테스트: 그날까지 알 수 있던 과거 쌍만으로 계산한 '충족 비율'이 실제 결과(정답)와 맞는지
E4 가격 범위 검증: 지속 기준선·과거 중앙값 오차와 과거 범위 적중률
E5 유효 표본: 날짜가 겹치지 않는 쌍이 실제로 몇 개인지

실행: python -X utf8 -m experiments.run_experiments  (결과: experiments/results.json)
"""
import csv
import json
import random
import statistics
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from agents.future_pair_state import FuturePairState
from services.past_reach import past_pairs_reaching

ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data/processed/price_comparison_availability.csv"
SEASONS = ("2023-2024", "2024-2025", "2025-2026")
SPEC_KEYS = ("variety", "grade", "size", "package", "package_kg")
D = Decimal


def load(path):
    with open(path, encoding="utf-8-sig") as f:
        return [r for r in csv.DictReader(f)
                if r["comparison_status"] == "COMPARABLE" and r["market_code"] == "110001" and r["trade_type"] == "경매"]


def spec_of(r):
    return (r["variety"], r["grade"], r["size"], r["package"], str(D(r["package_kg"]).normalize()))


# ---------------------------------------------------------------- 공통: 독립 구현(정답 계산기)
def reference(changes, q, pa, a, b):
    """시스템 코드를 쓰지 않고 공식대로 다시 계산한 정답."""
    a_kg, b_kg = q * (1 - a["loss"] / 100), q * (1 - b["loss"] / 100)
    a_amt = a_kg * pa * (1 - a["fee"] / 100) - (a["storage"] * a["days"] + a["other"])
    b_cost = b["storage"] * b["days"] + b["other"]
    per_price = b_kg * (1 - b["fee"] / 100)
    threshold = (a_amt + b_cost) / per_price
    need = (threshold / pa - 1) * 100
    reached = sum(1 for c in changes if c >= need)
    diffs = [per_price * (pa * (1 + c / 100)).quantize(D("0.01"), ROUND_HALF_UP) - b_cost - a_amt for c in changes]
    gains = [x for c, x in zip(changes, diffs) if c >= need]
    losses = [x for c, x in zip(changes, diffs) if c < need]
    won = lambda v: v.quantize(D("1"), ROUND_HALF_UP)
    return {"threshold": threshold, "reached": reached, "total": len(changes),
            "gain_median": won(statistics.median(gains)) if gains else None,
            "loss_median": won(statistics.median(losses)) if losses else None,
            "best": won(max(diffs)), "worst": won(min(diffs))}


def run_system(spec, interval, q, pa, a, b):
    variety, grade, size, package, kg = spec
    pair = FuturePairState()
    opt = lambda o, days: {"loss_percent": float(o["loss"]), "daily_storage_cost": float(o["storage"]),
                           "other_cost_krw": float(o["other"]), "fee_percent": float(o["fee"]), "delay_days": days}
    return pair.apply("new", pair.version, {
        "quantity": float(q), "price_basis": "gross", "start_date": "2026-11-17", "price_a": float(pa),
        "option_a": opt(a, a["days"]), "option_b": opt(b, b["days"]), "price_mode": "history",
        "history_filters": {"variety": variety, "grade": grade, "size": size, "package": package, "package_kg": kg}})


COST_PROFILES = {
    "비용 없음": ({"loss": D(0), "fee": D(5), "storage": D(0), "other": D(0)},
              {"loss": D(0), "fee": D(5), "storage": D(0), "other": D(0)}),
    "기본 예시": ({"loss": D(2), "fee": D(5), "storage": D(0), "other": D(0)},
              {"loss": D(4), "fee": D(5), "storage": D(0), "other": D(0)}),
    "보관비 있음": ({"loss": D(1), "fee": D(7), "storage": D(20000), "other": D(50000)},
               {"loss": D(3), "fee": D(7), "storage": D(20000), "other": D(80000)}),
}


# ---------------------------------------------------------------- E1 계산 정답셋
def e1_golden():
    rows = load(PROCESSED)
    groups = defaultdict(list)
    for r in rows:
        groups[(spec_of(r), int(r["interval_days"]))].append(D(r["price_change_pct"]))
    cases, fails = 0, []
    for (spec, interval), changes in sorted(groups.items()):
        for pa in (D(1800), D(2500), D(3600)):
            for name, (a, b) in COST_PROFILES.items():
                a, b = {**a, "days": 3}, {**b, "days": 3 + interval}
                q = D(3000)
                result = run_system(spec, interval, q, pa, a, b)
                ref = reference(changes, q, pa, a, b)
                cases += 1
                reach = result.get("past_reach")
                got = None if not reach else {
                    "reached": reach["reached"], "total": reach["total"],
                    "threshold": D(result["break_even_vs_a_krw_per_kg"]),
                    **{k: (None if reach["outcomes"][src] is None else D(reach["outcomes"][src]))
                       for k, src in (("gain_median", "gain_median_krw"), ("loss_median", "loss_median_krw"),
                                      ("best", "best_krw"), ("worst", "worst_krw"))}}
                ok = got is not None and got["reached"] == ref["reached"] and got["total"] == ref["total"] \
                    and abs(got["threshold"] - ref["threshold"]) <= D("0.01") \
                    and all((got[k] is None and ref[k] is None) or
                            (got[k] is not None and ref[k] is not None and abs(got[k] - ref[k]) <= 1)
                            for k in ("gain_median", "loss_median", "best", "worst"))
                if not ok:
                    fails.append({"spec": spec, "interval": interval, "price_a": str(pa), "profile": name,
                                  "status": result.get("status"), "system": str(got), "reference": str(ref)})
    return {"cases": cases, "passed": cases - len(fails), "failures": fails[:10],
            "criteria": "충족 쌍 수·전체 쌍 수 일치, 손익분기 차이 0.01원 이하, 금액(중앙값·최대·최소) 차이 1원 이하"}


# ---------------------------------------------------------------- E2 성질(메타모픽) 검증
def e2_metamorphic(samples=300, seed=7):
    rng = random.Random(seed)
    rows = load(PROCESSED)
    groups = defaultdict(list)
    for r in rows:
        groups[int(r["interval_days"])].append(D(r["price_change_pct"]))
    pools = [v for v in groups.values() if len(v) >= 10]
    checks = defaultdict(lambda: [0, 0])  # [통과, 전체]

    def reach(changes, q, pa, a, b):
        ref = reference(changes, q, pa, a, b)
        return ref["reached"], ref["threshold"]

    for _ in range(samples):
        changes = rng.choice(pools)
        pa = D(rng.randint(1200, 4500))
        q = D(rng.choice([1000, 3000, 8000]))
        a = {"loss": D(rng.randint(0, 3)), "fee": D(rng.choice([4, 5, 7])), "storage": D(rng.randint(0, 30000)),
             "other": D(rng.randint(0, 100000)), "days": 3}
        b = {**a, "loss": a["loss"] + D(rng.randint(0, 4)), "days": 10}
        base_r, base_t = reach(changes, q, pa, a, b)

        def check(name, cond):
            checks[name][0] += bool(cond)
            checks[name][1] += 1

        # 성질 1: B안 보관비가 늘면 손익분기는 오르고 충족 쌍은 줄거나 같다.
        r1, t1 = reach(changes, q, pa, a, {**b, "storage": b["storage"] + 10000})
        check("B 보관비 증가 → 충족 쌍 감소 또는 유지", r1 <= base_r and t1 > base_t)
        # 성질 2: B안 감모율이 늘면 충족 쌍은 줄거나 같다.
        r2, _ = reach(changes, q, pa, a, {**b, "loss": b["loss"] + 2})
        check("B 감모 증가 → 충족 쌍 감소 또는 유지", r2 <= base_r)
        # 성질 3: 비용이 0이고 수수료가 같으면, 수수료가 바뀌어도 결과는 같다(분자·분모에서 약분).
        zero_a = {**a, "storage": D(0), "other": D(0)}
        zero_b = {**b, "storage": D(0), "other": D(0)}
        r3a, _ = reach(changes, q, pa, zero_a, zero_b)
        r3b, _ = reach(changes, q, pa, {**zero_a, "fee": D(9)}, {**zero_b, "fee": D(9)})
        check("비용 0일 때 수수료 변경 → 결과 불변", r3a == r3b)
        # 성질 4: 비용이 0이면 A안 단가를 2배로 해도 필요한 변동률이 같아 결과가 같다.
        r4, _ = reach(changes, q, pa * 2, zero_a, zero_b)
        check("비용 0일 때 A 단가 2배 → 결과 불변", r4 == r3a)
        # 성질 5: 시스템 함수(past_pairs_reaching)와 독립 구현이 같은 답을 낸다.
        sys_r = past_pairs_reaching([str(c) for c in changes], pa, base_t, 7)["reached"]
        check("시스템 함수 = 독립 구현", sys_r == base_r)
    return {name: {"passed": p, "total": t} for name, (p, t) in checks.items()}


# ---------------------------------------------------------------- E3 판단 백테스트 (정답 = 실제 결과)
SCENARIOS = {
    # 하루 감모와 kg당 하루 보관비만 바꾼 단순 시나리오. 실제 농가 비용은 다를 수 있다.
    "가벼운 비용": {"loss_per_day": D("0.1"), "storage_per_kg_day": D(0), "fee": D(5)},
    "보통 비용": {"loss_per_day": D("0.2"), "storage_per_kg_day": D(3), "fee": D(5)},
}


def auc(scores, labels):
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def e3_backtest(min_past=10):
    out = {}
    for scen_name, s in SCENARIOS.items():
        records = []
        for season in SEASONS:
            rows = load(ROOT / f"data/seasons/{season}/price_comparison_availability.csv")
            by = defaultdict(list)
            for r in rows:
                if r["grade"] == "특" and D(r["package_kg"]) == 5:
                    by[(spec_of(r), int(r["interval_days"]))].append(r)
            for (spec, k), group in by.items():
                if k not in (7, 14):
                    continue
                group.sort(key=lambda r: r["base_date"])
                for r in group:
                    d = date.fromisoformat(r["base_date"])
                    # 정보 누출 방지: 결정일 d 이전에 결과(target)가 이미 나온 쌍만 '과거'로 쓴다.
                    past = [D(p["price_change_pct"]) for p in group if date.fromisoformat(p["target_date"]) < d]
                    if len(past) < min_past:
                        continue
                    pa, actual = D(r["base_price_kg"]), D(r["target_price_kg"])
                    loss = s["loss_per_day"] * k / 100
                    # kg 기준: A는 지금 판매, B는 k일 보관 후 판매.
                    a_amt = pa * (1 - s["fee"] / 100)
                    b_cost = s["storage_per_kg_day"] * k
                    threshold = (a_amt + b_cost) / ((1 - loss) * (1 - s["fee"] / 100))
                    reach = past_pairs_reaching([str(c) for c in past], pa, threshold, k)
                    realized = (1 - loss) * actual * (1 - s["fee"] / 100) - b_cost - a_amt
                    need = (threshold / pa - 1) * 100
                    past_gain = statistics.mean(
                        float((1 - loss) * pa * (1 + c / 100) * (1 - s["fee"] / 100) - b_cost - a_amt) for c in past)
                    last = max((p for p in group if date.fromisoformat(p["target_date"]) < d),
                               key=lambda p: p["target_date"])
                    records.append({"season": season, "spec": spec, "k": k, "date": r["base_date"],
                                    "score": reach["reached"] / reach["total"], "n_past": reach["total"],
                                    "truth": actual >= threshold, "realized": float(realized),
                                    "past_gain": past_gain,
                                    "momentum": D(last["price_change_pct"]) >= need})
        out[scen_name] = summarize_backtest(records)
    return out


def summarize_backtest(records):
    n = len(records)
    truth = [r["truth"] for r in records]
    score = [r["score"] for r in records]
    base = sum(truth) / n
    brier = lambda ps: sum((p - t) ** 2 for p, t in zip(ps, truth)) / n
    bins = []
    for lo, hi in ((0, .25), (.25, .5), (.5, .75), (.75, 1.01)):
        sel = [r for r in records if lo <= r["score"] < hi]
        if sel:
            bins.append({"충족 비율 구간": f"{lo:.2f}~{min(hi, 1):.2f}", "결정 수": len(sel),
                         "실제로 기다린 게 나았던 비율": round(sum(r["truth"] for r in sel) / len(sel), 3),
                         "평균 충족 비율": round(statistics.mean(r["score"] for r in sel), 3)})
    policies = {
        "항상 지금 판매": [0.0] * n,
        "항상 기다림": [r["realized"] for r in records],
        "충족 비율 ≥ 0.5면 기다림": [r["realized"] if r["score"] >= .5 else 0.0 for r in records],
        "과거 평균 손익 > 0이면 기다림": [r["realized"] if r["past_gain"] > 0 else 0.0 for r in records],
        "직전 쌍이 기준 넘었으면 기다림(모멘텀)": [r["realized"] if r["momentum"] else 0.0 for r in records],
        "정답을 아는 경우(상한)": [max(r["realized"], 0.0) for r in records],
    }
    by_k = defaultdict(int)
    for r in records:
        by_k[r["k"]] += 1
    # 날짜가 겹치지 않게 k일마다 하나씩만 고른 부분 표본 수(실질적인 독립 표본 크기에 가깝다).
    eff = 0
    groups = defaultdict(list)
    for r in records:
        groups[(r["season"], r["spec"], r["k"])].append(r)
    for (_, _, k), g in groups.items():
        last = None
        for r in sorted(g, key=lambda x: x["date"]):
            d = date.fromisoformat(r["date"])
            if last is None or d >= last + timedelta(days=k):
                eff += 1
                last = d
    return {
        "결정 수": n, "간격별": dict(by_k), "겹치지 않는 결정 수(근사)": eff,
        "실제로 기다린 게 나았던 비율(기저율)": round(base, 3),
        "Brier(충족 비율)": round(brier(score), 4), "Brier(항상 기저율, 사후값)": round(brier([base] * n), 4),
        "Brier(항상 0.5)": round(brier([.5] * n), 4),
        "AUC(충족 비율)": None if auc(score, truth) is None else round(auc(score, truth), 3),
        "AUC(과거 평균 손익)": None if auc([r["past_gain"] for r in records], truth) is None
        else round(auc([r["past_gain"] for r in records], truth), 3),
        "구간별 보정": bins,
        "정책별 kg당 평균 실현 손익(원)": {k: round(statistics.mean(v), 1) for k, v in policies.items()},
        "정책별 기다린 횟수": {"충족 비율 ≥ 0.5면 기다림": sum(r["score"] >= .5 for r in records),
                         "과거 평균 손익 > 0이면 기다림": sum(r["past_gain"] > 0 for r in records)},
    }


# ---------------------------------------------------------------- E4 가격 범위 검증
def e4_ranges(min_past=10):
    errs = defaultdict(list)
    covered = defaultdict(int)
    n = 0
    for season in SEASONS:
        rows = load(ROOT / f"data/seasons/{season}/price_comparison_availability.csv")
        by = defaultdict(list)
        for r in rows:
            if r["grade"] == "특" and D(r["package_kg"]) == 5 and int(r["interval_days"]) == 7:
                by[spec_of(r)].append(r)
        for group in by.values():
            for r in group:
                d = date.fromisoformat(r["base_date"])
                past = sorted(float(p["price_change_pct"]) for p in group if date.fromisoformat(p["target_date"]) < d)
                if len(past) < min_past:
                    continue
                pa, actual = float(r["base_price_kg"]), float(r["target_price_kg"])
                n += 1
                errs["지속 기준선(7일 뒤 = 오늘)"].append(abs(actual - pa) / actual)
                errs["과거 중앙값 변동 적용"].append(abs(actual - pa * (1 + statistics.median(past) / 100)) / actual)
                lo, hi = pa * (1 + past[0] / 100), pa * (1 + past[-1] / 100)
                covered["과거 최저~최고 범위 안"] += lo <= actual <= hi
                q = statistics.quantiles(past, n=10)
                covered["과거 10~90% 범위 안"] += pa * (1 + q[0] / 100) <= actual <= pa * (1 + q[-1] / 100)
    return {"결정 수": n,
            "평균 절대 백분율 오차(MAPE)": {k: round(statistics.mean(v) * 100, 2) for k, v in errs.items()},
            "범위 적중률": {k: round(v / n, 3) for k, v in covered.items()},
            "해석": "10~90% 범위의 목표 적중률은 약 0.8, 최저~최고 범위는 그보다 높아야 정직한 범위다."}


# ---------------------------------------------------------------- E5 유효 표본
def e5_overlap():
    rows = [r for r in load(PROCESSED)
            if spec_of(r) == ("조생귤", "특", "소", "상자", "5") and r["interval_days"] == "7"]
    rows.sort(key=lambda r: r["target_date"])
    chosen, end = 0, None
    for r in rows:
        if end is None or date.fromisoformat(r["base_date"]) >= end:
            chosen += 1
            end = date.fromisoformat(r["target_date"])
    return {"전체 쌍": len(rows), "날짜가 겹치지 않게 고를 수 있는 최대 쌍": chosen}


def main():
    results = {"E1 계산 정답셋": e1_golden(), "E2 성질 검증": e2_metamorphic(),
               "E3 판단 백테스트": e3_backtest(), "E4 가격 범위 검증": e4_ranges(), "E5 유효 표본": e5_overlap()}
    path = ROOT / "experiments/results.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
