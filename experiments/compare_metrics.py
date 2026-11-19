"""같은 백테스트 기록으로 여러 지표를 계산해 비교한다 (정확도·F1·MCC·로그 손실 등)."""
import math
import statistics

from experiments.run_extra_validation import auc, real_records


def confusion(pred, truth):
    tp = sum(p and t for p, t in zip(pred, truth))
    fp = sum(p and not t for p, t in zip(pred, truth))
    fn = sum(not p and t for p, t in zip(pred, truth))
    tn = sum(not p and not t for p, t in zip(pred, truth))
    return tp, fp, fn, tn


def scores(pred, truth):
    tp, fp, fn, tn = confusion(pred, truth)
    n = len(truth)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    denom = math.sqrt((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
    mcc = (tp * tn - fp * fn) / denom if denom else 0.0
    # 균형 정확도: '기다린 게 나았던 날'과 '아니었던 날'을 같은 무게로 본다.
    spec = tn / (tn + fp) if tn + fp else 0.0
    return {"정확도": round((tp + tn) / n, 3), "균형 정확도": round((rec + spec) / 2, 3),
            "정밀도": round(prec, 3), "재현율": round(rec, 3), "F1": round(f1, 3), "MCC": round(mcc, 3),
            "기다림 횟수": tp + fp}


recs = real_records()
truth = [r["truth"] for r in recs]
score = [r["score"] for r in recs]
print("결정 수", len(recs), "기저율", round(statistics.mean(truth), 3))
print("\n[정책별 분류 지표]")
policies = {"항상 기다림": [True] * len(recs), "항상 지금 판매": [False] * len(recs),
            "충족 비율 ≥ 0.5": [s >= .5 for s in score]}
for name, pred in policies.items():
    print(f"{name:14s}", scores(pred, truth))
print("\n[기준선을 바꾸면]")
for cut in (.3, .4, .5, .6, .7):
    s = scores([x >= cut for x in score], truth)
    print(f"충족 비율 ≥ {cut}", {k: s[k] for k in ("정확도", "균형 정확도", "F1", "MCC", "기다림 횟수")})
print("\n[확률처럼 읽었을 때]")
clip = lambda p: min(max(p, 1e-3), 1 - 1e-3)
logloss = lambda ps: -statistics.mean(math.log(clip(p)) if t else math.log(1 - clip(p)) for p, t in zip(ps, truth))
zero_one = sum(s in (0, 1) for s in score)
print("충족 비율이 0 또는 1인 결정 수:", zero_one)
print("로그 손실(충족 비율, 0.001로 자름):", round(logloss(score), 4))
print("로그 손실(항상 0.5):", round(logloss([.5] * len(recs)), 4))
print("로그 손실(항상 기저율 0.645):", round(logloss([statistics.mean(truth)] * len(recs)), 4))
print("AUC(충족 비율):", round(auc(score, truth), 3), "/ AUC(항상 같은 값):", auc([.5] * len(recs), truth))
