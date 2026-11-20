"""과거 가격 미리보기에 붙이는 자료 기준 한 줄."""

MARKET_NAMES = {"110001": "서울가락"}


def data_basis_line(preview):
    """미리보기 한 줄 요약: 어떤 자료·관측기간·몇 쌍으로 계산했는지."""
    source = preview.get("source") or {}
    conditions = preview.get("conditions") or {}
    code = conditions.get("market_code")
    market = " ".join(filter(None, (MARKET_NAMES.get(code, code), conditions.get("trade_type"))))
    file = str(source.get("comparison_file") or "").replace("\\", "/")
    # 시즌 비교표(data/seasons/<시즌>/...)를 쓰면 시즌명을, 아니면 기본 자료로 표시한다.
    season = file.split("data/seasons/")[1].split("/")[0] if "data/seasons/" in file else None
    parts = [f"{market} {f'{season} 시즌' if season else '기본'} 자료".strip()]
    period = source.get("observed_period") or ()
    if len(period) == 2 and all(period):
        # 연도가 같으면 끝 날짜의 연도는 줄여 짧게 보여준다.
        end = period[1][5:] if period[0][:4] == period[1][:4] else period[1]
        parts.append(f"{period[0]}~{end} 관측")
    parts.append(f"같은 규격 {preview['interval_days']}일 {preview['clean_pairs']}쌍")
    return " · ".join(parts)
