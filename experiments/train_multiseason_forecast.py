"""Fit and persist research-only citrus price models from all available seasons.

This creates one HistGradientBoosting model for each 3/7/10/14 calendar-day
horizon. It uses exact-spec auction prices and volumes, saves a versioned local
bundle, and does not change the normal Agent or select a horizon for advice.

Run from the project root:
    python -m experiments.train_multiseason_forecast
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from experiments.evaluate_multiseason_inputs import (
    BASE_NUMERIC,
    PROVISIONAL_VARIETIES,
    ROOT,
    SPEC_FIELDS,
    _make_pipeline,
    _matrix,
    build_samples,
    load_price_data,
)

DEFAULT_OUTPUT = ROOT / "artifacts" / "seunghyuk" / "price_forecast_multiseason"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fit_bundle(output_root: Path = DEFAULT_OUTPUT) -> Path:
    import joblib
    import numpy as np
    import sklearn

    clean_by_season, season_info, input_hashes = load_price_data()
    clean_counts = Counter()
    for rows in clean_by_season.values():
        for row in rows:
            if row["spec"][1] not in PROVISIONAL_VARIETIES:
                clean_counts[row["spec"]] += 1
    eligible_specs = {spec for spec, count in clean_counts.items() if count >= 10}
    if not eligible_specs:
        raise ValueError("학습에 쓸 정상 규격이 없습니다.")

    # 출하량은 최신 시즌에 관측되지 않으므로 예측 피처에 넣지 않는다.
    samples = build_samples(clean_by_season, season_info, shipments={})
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f_utc")
    bundle = output_root / timestamp
    bundle.mkdir(parents=True, exist_ok=False)

    models = {}
    season_training_counts = {}
    for season, horizon_rows in samples.items():
        season_training_counts[season] = {}
        for horizon, rows in horizon_rows.items():
            season_training_counts[season][str(horizon)] = sum(
                row["spec"] in eligible_specs for row in rows
            )

    for horizon in (3, 7, 10, 14):
        rows = [
            row for horizon_rows in samples.values()
            for row in horizon_rows[horizon]
            if row["spec"] in eligible_specs
        ]
        if len(rows) < 50:
            raise ValueError(f"{horizon}일 모델의 학습 표본이 부족합니다: {len(rows)}")
        x = _matrix(rows, BASE_NUMERIC, np)
        target_change = np.asarray([row["target"] - row["current"] for row in rows], dtype=float)
        model = _make_pipeline("hist_gbdt", BASE_NUMERIC)
        model.fit(x, target_change)
        model_path = bundle / f"candidate_{horizon}d.joblib"
        joblib.dump(model, model_path)
        # 저장 파일을 다시 불러와 한 행 추론이 가능한지 확인한다.
        loaded = joblib.load(model_path)
        smoke_delta = float(loaded.predict(x[:1])[0])
        if not np.isfinite(smoke_delta):
            raise ValueError(f"{horizon}일 모델이 유한하지 않은 예측을 반환했습니다.")
        models[str(horizon)] = {
            "file": model_path.name,
            "train_rows": len(rows),
            "train_specs": len({row["spec"] for row in rows}),
            "base_date_range": [min(row["date"] for row in rows).isoformat(),
                                max(row["date"] for row in rows).isoformat()],
            "target_date_range": [min(row["target_date"] for row in rows).isoformat(),
                                  max(row["target_date"] for row in rows).isoformat()],
            "smoke_inference_prediction_delta_krw_per_kg": round(smoke_delta, 4),
        }

    source_files = {
        season: {
            "path": f"data/seasons/{season}/daily_prices.csv",
            "sha256": input_hashes[season],
            "observed_date_range": [season_info[season]["date_min"], season_info[season]["date_max"]],
            "quality_ok_rows": season_info[season]["clean_rows"],
        }
        for season in clean_by_season
    }
    metrics = {
        "status": "FITTED_RESEARCH_ONLY_NOT_DEPLOYED",
        "created_at_utc": timestamp,
        "model_family": "HistGradientBoostingRegressor",
        "target": "exact-spec future auction price minus base-date auction price (KRW/kg)",
        "horizons_calendar_days": [3, 7, 10, 14],
        "features": {
            "numeric": list(BASE_NUMERIC),
            "categorical": list(SPEC_FIELDS),
            "source_limit": "Jeju DataHub #855 shipment data excluded: current snapshot ends 2025-09-17 and does not overlap the latest price seasons.",
        },
        "training_policy": "all available seasons; only quality_flag == OK exact-spec target pairs; provisional variety codes excluded; at least 10 clean observations per SKU over available history",
        "training_rows_by_season_and_horizon": season_training_counts,
        "eligible_spec_count": len(eligible_specs),
        "eligible_spec_list": [
            dict(zip(SPEC_FIELDS, spec)) for spec in sorted(eligible_specs)
        ],
        "models": models,
        "source_files": source_files,
        "versions": {"python": sys.version.split()[0], "scikit_learn": sklearn.__version__},
        "evaluation_reference": "docs/14_가격예측_다중시즌_및_출하량_평가.md",
        "limitations": [
            "Candidate artifacts are fitted on all available labeled data and have no untouched post-fit evaluation.",
            "Previous walk-forward evaluation found no stable win over the trailing 7-day mean across the 7/10/14-day horizons.",
            "No prediction intervals are calibrated.",
            "Forecasts describe auction settlement prices, not farm-gate proceeds or optimal shipment dates.",
            "The normal Agent and UI are not connected to these artifacts.",
        ],
    }
    (bundle / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8")
    readme = [
        "# Multi-season citrus price model bundle",
        "",
        "Research candidate. Not connected to the normal Agent.",
        "",
        f"Model family: `{metrics['model_family']}`",
        f"Horizons: {', '.join(str(h) for h in metrics['horizons_calendar_days'])} calendar days",
        f"Eligible exact specs: {len(eligible_specs)}",
        "",
        "| Horizon | Training rows | Exact specs | Base-date range |",
        "|---:|---:|---:|---|",
    ]
    for horizon, item in models.items():
        readme.append(
            f"| {horizon} | {item['train_rows']} | {item['train_specs']} | "
            f"{item['base_date_range'][0]} ~ {item['base_date_range'][1]} |"
        )
    readme += ["", "See `metrics.json` for source hashes, feature contract, and limitations.", ""]
    (bundle / "README.md").write_text("\n".join(readme), encoding="utf-8")
    return bundle


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT,
                        help="bundle root (a timestamped subfolder is created)")
    args = parser.parse_args(argv)
    try:
        bundle = fit_bundle(args.out_dir)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"[학습 실패] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    metrics = json.loads((bundle / "metrics.json").read_text(encoding="utf-8"))
    print(f"연구용 가격 모델 묶음 저장: {bundle}")
    for horizon, item in metrics["models"].items():
        print(f"{horizon:>2}일: {item['train_rows']:,}행 / {item['train_specs']}개 규격 / {item['file']}")
    print("판정: 모델 파일은 생성됐으며, Agent 연결·운영 승인은 하지 않았습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
