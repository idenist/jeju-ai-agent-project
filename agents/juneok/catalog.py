"""Bounded discovery of observed joint conditions; never a cartesian product."""
from collections import defaultdict
from datetime import date
from decimal import Decimal
import calendar
import re
from .config import PROJECT_ROOT
from .evidence import _market_rows, _shipment_rows, _shipment_source
from .data_refresh import resolve_market_root

MARKET = ("market_code", "variety", "grade", "size", "package", "package_kg")
SHIPMENT = ("shipment_variety", "year", "target_month")


def explore_data(source, filters=None, offset=0, limit=20, root=PROJECT_ROOT):
    try:
        if source not in {"market", "shipment"} or not isinstance(filters, (dict, type(None))):
            raise ValueError("자료 종류와 선택 조건을 확인하세요.")
        filters = {k: v for k, v in (filters or {}).items() if v is not None}
        fields = MARKET if source == "market" else SHIPMENT
        if set(filters) - set(fields) or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("허용된 선택 조건·페이지 범위를 확인하세요.")
        from .comparison import decimal
        for k, v in filters.items():
            if k == "package_kg":
                if decimal(v, k) <= 0: raise ValueError("포장중량은 양수여야 합니다.")
            elif k in {"year", "target_month"}:
                if type(v) is not int or not 1 <= v <= (12 if k == "target_month" else 9999): raise ValueError("조회 연도·월을 확인하세요.")
            elif not isinstance(v, str) or not v.strip() or len(v) > 200:
                raise ValueError("조회 조건은 비어 있지 않은 문자열이어야 합니다.")
        if source == "market" and re.search(r"\d\s*(kg|㎏|킬로|그램)", filters.get("package", ""), re.I):
            raise ValueError("포장 종류와 중량을 분리하세요. 5kg 상자는 package='상자', package_kg=5입니다. 값을 확인해 다시 탐색하세요.")
    except (ValueError, TypeError, ArithmeticError) as exc:
        return {"status": "INVALID_INPUT", "message": str(exc)}
    try:
        groups = defaultdict(list)
        if source == "market":
            selected = resolve_market_root(root)
            rows = _market_rows(selected)
            for r in rows:
                key = tuple(str(Decimal(r[k]).normalize()) if k == "package_kg" else r[k] for k in fields)
                groups[key].append(r)
            meta = {"dataset": "전국 공영도매시장 경매원천정보 · 확보 CSV", "url": "https://www.data.go.kr/data/15141810/openapi.do",
                    "source_first_date": min((r["date"] for r in rows), default=None), "source_last_date": max((r["date"] for r in rows), default=None), "unit": "거래규격"}
        else:
            rows = _shipment_rows(root)
            meta, _, _ = _shipment_source(root, rows)
            for r in rows:
                groups[(r["variety"], int(r["year_month"][:4]), int(r["year_month"][5:]))].append(r)
        candidates = []
        for key, records in sorted(groups.items()):
            condition = dict(zip(fields, key))
            if not all((Decimal(condition[k]) == Decimal(str(v)) if k == "package_kg" else condition[k] == v) for k, v in filters.items()):
                continue
            warnings = sorted({w for r in records for w in r.get("quality_flag", r.get("data_warning", "")).split(";") if w and w.lower() != "ok"})
            item = {"conditions": condition, "rows_count": len(records), "warnings": warnings}
            if source == "market":
                identities = {(r["variety_code"], r["grade_code"], r["size_code"]) for r in records}
                item.update(first_observed=min(r["date"] for r in records), last_observed=max(r["date"] for r in records),
                            observed_days=len({r["date"] for r in records}), ambiguous_codes=len(identities) > 1)
                item["market_name"] = ", ".join(sorted({r.get("market", r["market_code"]) for r in records}))
            else:
                month = records[0]["year_month"]
                for boundary in [meta.get("source_first_date"), meta.get("source_last_date")]:
                    if boundary and boundary[:7] == month:
                        day = date.fromisoformat(boundary).day
                        if (boundary == meta.get("source_first_date") and day != 1) or (boundary == meta.get("source_last_date") and day != calendar.monthrange(key[1], key[2])[1]):
                            warnings.append("PARTIAL_MONTH")
                item["observed_days"] = int(records[0]["observed_days"])
            candidates.append(item)
        return {"status": "OK" if candidates else "NO_DATA", "analysis_type": "data_catalog", "catalog_source": source,
                "filters": filters, "total_candidates": len(candidates), "offset": offset,
                "next_offset": offset + limit if offset + limit < len(candidates) else None,
                "candidates": candidates[offset:offset + limit], "source": meta,
                "available_filters": {field: sorted({c["conditions"][field] for c in candidates}) for field in fields},
                "message": "확보 자료에 실제 존재하는 조건입니다." if candidates else "선택 조건에 맞는 확보 자료가 없습니다. 거래조건과 단위를 확인하세요.",
                "warnings": ["자료가 있는 조건이며 실제 농가 조건과 일치한다는 뜻은 아닙니다. 선택을 확인하세요.", "출하 품종·경매 품종을 자동 매핑하지 않으며 경락가격을 제시가격에 대입하지 않습니다.", "후보 개수는 날짜별 원천 거래 건수가 아닙니다. 관측일 수와 코드 모호성을 확인하세요."],
                "is_future_forecast": False}
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
        return {"status": "DATA_ERROR", "message": "탐색 자료와 품질보고서를 확인하세요.", "error_kind": type(exc).__name__}
