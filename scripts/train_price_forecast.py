"""제주 감귤 경락가격 예측 모델 1차 실험.

2024~25 학습 -> 2025~26 검증. 모델 선택에 2026~27 일부 자료를 사용하지 않는다.
현재가 유지 예측을 이기지 못하면 모델을 Agent에 적용하지 않는다.

프로젝트 루트에서:
    python -m scripts.train_price_forecast
    python -m scripts.train_price_forecast --diagnostic-2026

필요: scikit-learn, numpy (scikit-learn 의존성으로 설치됨)
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "seasons" / "prediction_v2"
DEFAULT_OUTPUT = ROOT / "artifacts" / "seunghyuk" / "price_forecast_v1"
HORIZONS = (3, 7, 14)

# 데이터계약 V2에 허용된 입력만 사용한다. 정답과 미래 날짜는 입력에 넣지 않는다.
CATEGORICAL = (
    "market_code", "variety_code", "grade_code", "size_code",
    "package", "package_kg", "trade_type",
)
NUMERIC = (
    "feature_price_kg", "feature_total_kg", "feature_transaction_count",
    "feature_weekday", "feature_month", "feature_season_day_index",
    "feature_lag3_price_kg", "feature_lag7_price_kg",
    "feature_trailing7_mean_price_kg", "feature_trailing7_total_kg",
    "feature_trailing7_ok_days",
)
REQUIRED = (*CATEGORICAL, *NUMERIC, "split", "base_date", "target_date",
            "horizon_days", "target_price_kg")
KST = timezone(timedelta(hours=9))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load(path: Path, split: str):
    """허용된 컬럼만 행렬에 담고, 라벨/간격/날짜가 정상인지 검증한다."""
    import numpy as np
    values: dict[int, list[tuple]] = defaultdict(list)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        header = reader.fieldnames or []
        if len(header) != len(set(header)) or not set(REQUIRED).issubset(header):
            raise ValueError(f"{path.name}: 데이터계약 V2 필수 컬럼 누락 또는 중복")
        for line_no, row in enumerate(reader, 2):
            if None in row or any(v is None for v in row.values()):
                raise ValueError(f"{path.name} {line_no}행: CSV 컬럼 수 불일치")
            if row["split"] != split:
                raise ValueError(f"{path.name} {line_no}행: split={row['split']!r}, 예상 {split!r}")
            h = int(row["horizon_days"])
            if h not in HORIZONS:
                raise ValueError(f"{path.name} {line_no}행: 지원하지 않는 간격 {h}")
            # 시점은 계약 검증에만 사용하며 모델 입력에는 넣지 않는다.
            from datetime import date, timedelta
            base_date, target_date = date.fromisoformat(row["base_date"]), date.fromisoformat(row["target_date"])
            if target_date != base_date + timedelta(days=h):
                raise ValueError(f"{path.name} {line_no}행: 날짜 간격 불일치")
            if split == "train" and not (2024 <= base_date.year <= 2025):
                raise ValueError("학습 시즌 날짜가 예상 범위를 벗어났습니다")
            if split == "validation" and not (2025 <= base_date.year <= 2026):
                raise ValueError("검증 시즌 날짜가 예상 범위를 벗어났습니다")
            # 범주형 코드는 01처럼 선행 0이 의미 있으므로 문자열로 유지한다.
            nums = [float(row[k]) if row[k].strip() else np.nan for k in NUMERIC]
            current, target = nums[0], float(row["target_price_kg"])
            if (not np.isfinite(current) or current <= 0
                    or not np.isfinite(target) or target <= 0
                    or any(not np.isfinite(n) for n in nums if not np.isnan(n))):
                raise ValueError(f"{path.name} {line_no}행: 유효하지 않은 가격·특징")
            cats = [row[k] for k in CATEGORICAL]
            if any(not c.strip() for c in cats):
                raise ValueError(f"{path.name} {line_no}행: 규격 코드 또는 값이 비어 있습니다")
            values[h].append((cats, nums, current, target, base_date.isoformat(), target_date.isoformat()))
    if any(not values[h] for h in HORIZONS):
        raise ValueError(f"{path.name}: 3/7/14일 중 표본 없는 간격이 있습니다")
    return values


def matrix(rows, np):
    """범주를 임의의 크기 순서로 해석하지 않도록 One-Hot으로 분리한다."""
    num = np.asarray([r[1] for r in rows], dtype=np.float64)
    cat = np.asarray([r[0] for r in rows], dtype=object)
    cur = np.asarray([r[2] for r in rows], dtype=np.float64)
    target = np.asarray([r[3] for r in rows], dtype=np.float64)
    return num, cat, cur, target


def create_model(kind: str):
    from sklearn.compose import ColumnTransformer
    from sklearn.ensemble import HistGradientBoostingRegressor, ExtraTreesRegressor
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    num_indices = list(range(len(NUMERIC)))
    cat_indices = list(range(len(NUMERIC), len(NUMERIC) + len(CATEGORICAL)))
    num_steps = [
        ("missing", SimpleImputer(strategy="median", add_indicator=True)),
        ("scale", StandardScaler()),
    ]
    features = ColumnTransformer([
        ("numeric", Pipeline(num_steps), num_indices),
        ("categorical", OneHotEncoder(handle_unknown="ignore", sparse_output=False), cat_indices),
    ], sparse_threshold=0)
    # 고정된 소수의 모델만 비교한다. 검증 결과에 맞춰 끝없이 파라미터를 조정하지 않는다.
    if kind == "ridge":
        estimator = Ridge(alpha=100.0)
    elif kind == "hist_gbdt":
        estimator = HistGradientBoostingRegressor(
            max_iter=120, max_leaf_nodes=9, min_samples_leaf=25,
            learning_rate=0.04, l2_regularization=100.0, random_state=42,
        )
    elif kind == "extra_trees":
        estimator = ExtraTreesRegressor(
            n_estimators=100, max_depth=7, min_samples_leaf=8,
            max_features=0.8, random_state=42, n_jobs=1,
        )
    else:
        raise ValueError(kind)
    return Pipeline([("prepare", features), ("regressor", estimator)])


def pack(rows, np):
    num, cat, cur, target = matrix(rows, np)
    x = np.column_stack([num, cat]).astype(object)
    return x, cur, target


def metrics(actual, prediction, current, np):
    error = prediction - actual
    actual_change = np.sign(actual - current)
    predicted_change = np.sign(prediction - current)
    return {
        "mae_krw_per_kg": round(float(np.mean(np.abs(error))), 2),
        "mean_error_krw_per_kg": round(float(np.mean(error)), 2),
        "direction_accuracy": (None if np.all(predicted_change == 0)
                               else round(float(np.mean(predicted_change == actual_change)), 4)),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="제주 감귤 미래 경락가격 모델 1차 학습·검증")
    parser.add_argument("--train", type=Path, default=DATA / "2024-2025_train.csv")
    parser.add_argument("--validation", type=Path, default=DATA / "2025-2026_validation.csv")
    parser.add_argument("--diagnostic-2026", action="store_true",
                        help="2026년 일부 시즌 참고 평가 (모델 선정에 절대 사용하지 않음)")
    parser.add_argument("--evaluation", type=Path, default=DATA / "2026-2027_partial_evaluation.csv")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        import joblib
        import numpy as np
        import sklearn
    except ImportError:
        print("필요한 패키지가 없습니다: python -m pip install scikit-learn", file=sys.stderr)
        return 2

    # 이전 결과를 덮어쓰지 않도록 버전별 하위 폴더를 생성한다.
    folder = args.out_dir / datetime.now(KST).strftime("%Y%m%d_%H%M%S_%f")
    paths = {"train": args.train, "validation": args.validation}
    if args.diagnostic_2026:
        paths["evaluation"] = args.evaluation

    try:
        print("[1/5] 학습·검증 CSV 계약 확인", flush=True)
        sets = {kind: load(path, kind) for kind, path in paths.items()}
        input_hashes = {kind: sha256(path) for kind, path in paths.items()}

        print("[2/5] 현재 가격 유지 기준선 평가", flush=True)
        score = {name: {} for name in sets}
        for name, parts in sets.items():
            for horizon in HORIZONS:
                x, current, target = pack(parts[horizon], np)
                score[name][str(horizon)] = {
                    "n": len(target),
                    "persistence": metrics(target, current, current, np),
                    "candidates": {},
                }

        # 3/7/14일별로 각각 학습한다. 정답이 있는 관측쌍만 평가한 수치임을 기록한다.
        print("[3/5] Ridge / HistGBDT / ExtraTrees 실제 학습", flush=True)
        trained = {}
        for horizon in HORIZONS:
            x_train, current_train, y_train = pack(sets["train"][horizon], np)
            changes = y_train - current_train
            for name in ("ridge", "hist_gbdt", "extra_trees"):
                model = create_model(name)
                model.fit(x_train, changes)
                trained[horizon, name] = model
                for split_name, parts in sets.items():
                    x, current, actual = pack(parts[horizon], np)
                    # 학습 모델은 기준일 대비 변화량을 예측한다.
                    guess = np.maximum(0.0, current + model.predict(x))
                    score[split_name][str(horizon)]["candidates"][name] = metrics(actual, guess, current, np)

        print("[4/5] 검증 시즌 기준 모델 비교", flush=True)
        selection = {}
        for horizon in HORIZONS:
            key = str(horizon)
            v = score["validation"][key]
            baseline = v["persistence"]["mae_krw_per_kg"]
            ranked = sorted(v["candidates"].items(), key=lambda item: item[1]["mae_krw_per_kg"])
            choice, best = ranked[0]
            improvement = round((baseline - best["mae_krw_per_kg"]) / baseline * 100, 2)
            # '안전한 출하 권고'가 아닌 후보 선정을 위한 최소 오차 기준이다.
            # 한 시즌 검증에 불과하므로 통과해도 바로 배포하지 않는다.
            candidate_better = improvement > 0
            selection[key] = {"best_candidate": choice,
                              "baseline_mae": baseline,
                              "candidate_mae": best["mae_krw_per_kg"],
                              "mae_improvement_pct": improvement,
                              "better_than_persistence": candidate_better,
                              "approved_for_agent": False}
            print(f"  {horizon:2}일: 기준선 {baseline:,.2f} / 최선 모델({choice}) "
                  f"{best['mae_krw_per_kg']:,.2f}원/kg · 차이 {improvement:+.2f}%", flush=True)

        print("[5/5] 연구용 모델·평가 기록 저장", flush=True)
        folder.mkdir(parents=True, exist_ok=False)
        for horizon in HORIZONS:
            name = selection[str(horizon)]["best_candidate"]
            joblib.dump(trained[horizon, name], folder / f"candidate_{horizon}d.joblib")
        result = {
            "status": "EXPERIMENTAL_NOT_DEPLOYED",
            "training_season": "2024-2025",
            "validation_season": "2025-2026",
            "2026_partial_is_diagnostic_only": args.diagnostic_2026,
            "selection_basis": "2025-2026 validation MAE only; do not use partial evaluation to tune",
            "target": "target_price_kg - feature_price_kg (원/kg 변화량)",
            "model_inputs": {"numeric": list(NUMERIC), "categorical": list(CATEGORICAL)},
            "not_used_as_input": ["target_date", "base_date", "target_price_kg", "split",
                                  "market", "variety", "grade", "size"],
            "input_sha256": input_hashes,
            "versions": {"python": sys.version.split()[0], "scikit_learn": sklearn.__version__},
            "scores": score,
            "selected_candidates": selection,
            "deployment_warning": (
                "독립적인 학습 시즌은 1개뿐이며, 정확한 대상일 가격이 관측된 사례에 한정한 평가임. "
                "2026 일부 시즌은 모델 선정에 사용하지 않음. "
                "모든 모델은 연구용으로 저장했으며 Agent 자동 연결을 금지함."),
        }
        (folder / "metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"  기록 폴더: {folder}", flush=True)
        print("  판정: 모델은 연구용으로만 저장. Agent와 운영 데이터 변경 없음.", flush=True)
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"[학습 실패] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
