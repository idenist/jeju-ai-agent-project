"""Status by default; explicitly collect or stage public auction CSVs."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys
from agents.juneok.config import PROJECT_ROOT
from agents.juneok.data_refresh import market_status, stage_snapshot, activate_snapshot, snapshot_dir, resolve_market_root


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, nargs="+")
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--name", default=datetime.now(timezone(timedelta(hours=9))).strftime("market-%Y%m%dT%H%M%S"))
    parser.add_argument("--activate", help="Choose a previously validated, newer snapshot ID")
    args = parser.parse_args()
    try:
        if args.activate:
            if args.collect or args.input:
                parser.error("활성화는 별도 명령으로 실행하세요.")
            output = activate_snapshot(args.activate)
        elif args.collect:
            if args.input or not args.start or not args.end:
                parser.error("수집에는 --start와 --end만 지정하세요.")
            from dotenv import dotenv_values
            values = dotenv_values(PROJECT_ROOT / ".env", interpolate=False)
            key = str(os.environ.get("DATA_GO_KR_KEY", values.get("DATA_GO_KR_KEY") or "")).strip()
            if not key:
                print(json.dumps({"status": "KEY_REQUIRED", "key_name": "DATA_GO_KR_KEY", "network_called": False}))
                return 1
            from urllib.parse import unquote
            from scripts.collect_auction_raw import dates_between, make_session, collect_one_day, export_all_cached
            dates = dates_between(args.start, args.end)
            if len(dates) > 7:
                parser.error("한 번에 최대 7일씩 수집하세요.")
            current = market_status()
            if dates[0] <= current["observed_last_date"]:
                parser.error("중복 수집을 피하려면 현재 마지막 관측일 다음 날부터 지정하세요.")
            target = snapshot_dir(PROJECT_ROOT, args.name + "-collection")
            target.mkdir(parents=True, exist_ok=False)
            session = make_session()
            try:
                # Collector exceptions or provider messages must not expose request URLs/keys.
                with contextlib.redirect_stdout(io.StringIO()):
                    for day in dates:
                        collect_one_day(session, unquote(key), "110001", day, target, 0.25, 30, False)
                    export_all_cached(target, target / "auction.csv")
            finally:
                session.close()
            selected = resolve_market_root()
            if selected == PROJECT_ROOT:
                prior_inputs = [PROJECT_ROOT / "data/raw/auction_trades.csv"]
            else:
                prior = json.loads((selected / "manifest.json").read_text(encoding="utf-8"))
                prior_inputs = [selected / p for p in prior["raw_files"]]
            from scripts.collect_auction_raw import read_cache
            collection = {"queried_dates": dates, "market_code": "110001", "source": "public_api",
                          "records_by_date": {day: read_cache(target / f"110001_{day}.json")["total_count"] for day in dates}}
            output = stage_snapshot([*prior_inputs, target / "auction.csv"], args.name, collection=collection)
        elif args.input:
            output = stage_snapshot(args.input, args.name)
        else:
            output = market_status()
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "REFRESH_FAILED", "error_type": type(exc).__name__,
                          "message": "활성 자료는 유지됩니다. 경로·원본 형식·검증 명세를 확인하세요. 상세 인증정보는 출력하지 않습니다."}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
