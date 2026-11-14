"""Simple CLI to validate #855 historical timing without Streamlit/API credentials."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from agents.seunghyuk.shipment_timing import analyze_shipment_timing, format_timing_report


def main():
    parser = argparse.ArgumentParser(description="제주데이터허브 과거 출하시기 A/B 분석")
    parser.add_argument("--variety", default="노지온주")
    parser.add_argument("--a", default="2026-11-20", help="A안 출하 예정일 YYYY-MM-DD")
    parser.add_argument("--b", default="2026-11-27", help="B안 출하 예정일 YYYY-MM-DD")
    parser.add_argument("--json", default=None, help="결과 JSON 저장 경로")
    args = parser.parse_args()
    result = analyze_shipment_timing({"shipment_variety": args.variety, "option_a_date": args.a,
                                     "option_b_date": args.b}, ROOT)
    print(format_timing_report(result))
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\nJSON: {out}")
    return 0 if result["status"] == "OK" else 2


if __name__ == "__main__":
    sys.exit(main())
