#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""제주 감귤 Agent용 경매원천정보 API 원본 수집기.

공공데이터포털: https://www.data.go.kr/data/15141810/openapi.do
API: https://apis.data.go.kr/B552845/katOrigin/trades

조회 조건: 도매시장 + 거래일 + 중분류코드 14 (단일 품목 조건).
저장 조건: 과실류(06) / 감귤(14). 추가 중분류 코드만으로 들어온 타 품목은 제외.
※ 감귤 거래의 원산지·품종·등급·거래방식·이상치 및 개별 값은 가공하지 않음.
※ API에서 받은 전체 응답은 날짜별 JSON 캐시에 그대로 보관.

실행 예시 (프로젝트 루트에서):
  python -m pip install requests
  # Windows PowerShell: $env:DATA_GO_KR_KEY = "서비스키"
  python scripts/collect_auction_raw.py --start 2026-10-01 --end 2026-11-03 --markets 110001

출력: data/raw/auction_trades.csv (API 응답 컬럼을 그대로 CSV로 펼친 파일)
캐시: data/.auction_api_cache_mclsf14/ (요청 조건별 분리; 중단 후 재실행 가능)
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta
import json
import math
import os
from pathlib import Path
import sys
import time
from urllib.parse import unquote
from collections import Counter

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 외부 API는 원자료 수집 전용이다. 실행 중인 Agent가 호출하는 실시간 가격 API가 아니다.
API_URL = "https://apis.data.go.kr/B552845/katOrigin/trades"
PAGE_SIZE = 100  # 이미 실험으로 정상 동작이 검증된 크기


# 요청 범위 오류를 수집 시작 전에 차단한다. 미래 날짜는 조회하지 않는다.
def dates_between(start: str, end: str) -> list[str]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise ValueError("--start는 --end보다 늦을 수 없습니다.")
    if last > date.today():
        raise ValueError("미래 날짜는 수집할 수 없습니다.")
    return [(first + timedelta(days=i)).isoformat()
            for i in range((last - first).days + 1)]


# HTTP 오류나 일시적인 통신 실패를 제한적으로 재시도하는 세션을 구성한다.
def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = "JejuCitrusResearchRawCollector/1.0"
    return session


# 날짜·시장별 페이지를 요청한다. 중분류 14만 API에서 필터링하고 과실류 여부는 로컬에서 확인한다.
def get_page(session: requests.Session, key: str, market: str,
             day: str, page: int, timeout: int) -> tuple[list[dict], int]:
    params = {
        "serviceKey": key,
        "returnType": "JSON",
        "pageNo": page,
        "numOfRows": PAGE_SIZE,
        "cond[whsl_mrkt_cd::EQ]": market,
        "cond[trd_clcln_ymd::EQ]": day,
        # 대분류까지 동시에 API 필터링하면 응답이 지연될 수 있어 중분류 조건만 전달한다.
        "cond[gds_mclsf_cd::EQ]": "14",  # 단독 조건은 실제 0.4초 응답 확인
    }
    # 연결 대기(10초)와 API 응답 대기(--timeout)를 분리한다.
    # 주의: requests 예외 원문에는 serviceKey가 포함된 URL이 노출될 수 있다.
    # 따라서 예외 메시지는 절대로 그대로 출력하지 않는다.
    try:
        resp = session.get(API_URL, params=params, timeout=(10, timeout))
        resp.raise_for_status()
    except requests.RequestException as exc:
        detail = str(exc).lower()
        kind = "응답 시간 초과" if "timed out" in detail or isinstance(exc, requests.Timeout) else type(exc).__name__
        status = exc.response.status_code if exc.response is not None else "확인 불가"
        raise RuntimeError(
            f"API 통신 실패: 시장={market} 날짜={day} page={page} "
            f"유형={kind} HTTP={status} / 응답 제한={timeout}초 "
            "(인증키를 포함한 요청 URL은 출력하지 않음)"
        ) from None
    # API가 반환하는 totalCount와 페이지별 실제 행 수의 일치 여부도 확인한다.
    try:
        payload = resp.json()
        api_response = payload["response"]
        header = api_response["header"]
        code = str(header.get("resultCode", ""))
        if code not in {"0", "00", "0000"}:
            raise RuntimeError(f"API 오류 {code}: {header.get('resultMsg', '')}")
        body = api_response["body"]
        total = int(body["totalCount"])
        if total < 0:
            raise ValueError("totalCount가 음수입니다.")
        items_block = body.get("items") or {}
        items = items_block.get("item", []) if isinstance(items_block, dict) else []
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
            raise ValueError("items.item 자료형이 예상과 다릅니다.")
        # 잘못된 필터/응답이 조용히 섞이는 일을 막는다.
        for row in items:
            if (str(row.get("whsl_mrkt_cd")) != market
                or str(row.get("trd_clcln_ymd")) != day
                or str(row.get("gds_mclsf_cd")) != "14"):
                raise ValueError("요청 시장·날짜·중분류 조건과 다른 거래가 반환됐습니다.")
        return items, total
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"응답 해석 실패: {market} {day} page={page}; "
                           f"응답 앞부분={resp.text[:180]}") from exc


# 하루·시장 단위 파일명을 일정하게 유지해 실패한 날짜만 재수집할 수 있도록 한다.
def cache_file(cache_dir: Path, market: str, day: str) -> Path:
    return cache_dir / f"{market}_{day}.json"


# 파일 존재만으로 완료 처리하지 않고, 기록 건수와 쿼리 조건까지 검증한다.
def read_cache(file: Path) -> dict:
    result = json.loads(file.read_text(encoding="utf-8"))
    if (result.get("query_filter") != "gds_mclsf_cd=14"
            or result.get("completed") is not True
            or not isinstance(result.get("records"), list)
            or len(result["records"]) != result.get("total_count")):
        raise RuntimeError(f"완료되지 않았거나 손상된 캐시: {file}")
    return result


# 하루의 모든 페이지가 완성되기 전에는 캐시를 정상 완료로 저장하지 않는다.
def collect_one_day(session: requests.Session, key: str, market: str,
                    day: str, cache_dir: Path, gap: float, timeout: int,
                    refresh: bool) -> dict:
    path = cache_file(cache_dir, market, day)
    # 기존 캐시가 유효하면 API를 재호출하지 않아 시간과 할당량을 아낀다.
    if path.exists() and not refresh:
        cached = read_cache(path)
        print(f"[캐시] {market} {day}: {cached['total_count']:,}건")
        return cached

    # 날짜 하나가 모두 성공한 다음에만 캐시에 기록한다.
    first_page, total = get_page(session, key, market, day, 1, timeout)
    records = list(first_page)
    pages = math.ceil(total / PAGE_SIZE) if total else 1
    if len(records) > total:
        raise RuntimeError(f"첫 페이지 행 수가 totalCount보다 큽니다: {market} {day}")
    for page in range(2, pages + 1):
        if gap:
            time.sleep(gap)
        rows, latest_total = get_page(session, key, market, day, page, timeout)
        if latest_total != total:
            raise RuntimeError(f"수집 중 totalCount 변경: {market} {day}, "
                               f"{total} → {latest_total}; 다시 실행하세요.")
        records.extend(rows)
    # 페이지 누락이 있는 날짜는 이후 전처리에서 정상적인 0건으로 처리하면 안 된다.
    if len(records) != total:
        raise RuntimeError(f"페이지 누락: {market} {day}, "
                           f"기대={total:,}, 실제={len(records):,}")

    result = {
        "query_filter": "gds_mclsf_cd=14",
        "completed": True,
        "market": market,
        "date": day,
        "total_count": total,
        "page_count": pages,
        "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "records": records,  # API 각 거래 객체를 가공 없이 보존
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)  # 중단된 날짜는 완료 처리되지 않음
    print(f"[완료] {market} {day}: {total:,}건 / {pages}페이지")
    return result


# 중분류 코드 14는 다른 대분류에도 나타나므로 대분류 06까지 함께 검사한다.
def is_citrus(row: dict) -> bool:
    """중분류코드 14는 다른 대분류에도 존재하므로 과실류 06을 추가 확인."""
    return (str(row.get("gds_lclsf_cd")) == "06"
            and str(row.get("gds_mclsf_cd")) == "14")


# 캐시 JSON에는 API 응답 전체를 보존하고, 분석용 원본 CSV로는 감귤 행만 내보낸다.
def export_all_cached(cache_dir: Path, out: Path) -> tuple[int, int, int]:
    files = sorted(cache_dir.glob("*.json"))
    if not files:
        raise RuntimeError("수집 완료된 날짜 캐시가 없습니다.")

    # 대부분의 원천 응답은 컬럼이 동일하나, 변경된 필드도 누락 없이 내보낸다.
    columns: list[str] = []
    seen_cols: set[str] = set()
    total_rows = 0
    citrus_rows = 0
    # 미리 모든 캐시의 컬럼을 확인하여 날짜별 응답 스키마 차이로 값이 누락되지 않게 한다.
    for file in files:
        cached = read_cache(file)
        total_rows += len(cached["records"])
        for record in cached["records"]:
            if not is_citrus(record):
                continue
            citrus_rows += 1
            for col in record:
                if col not in seen_cols:
                    seen_cols.add(col)
                    columns.append(col)

    if not columns:
        print("[주의] 모든 날짜의 감귤 거래가 0건입니다. CSV에는 헤더만 기록합니다.")
        columns = ["trd_clcln_ymd", "whsl_mrkt_cd", "corp_gds_item_nm"]

    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_name(out.name + ".tmp")
    # 최종 CSV도 임시 파일을 모두 작성한 다음 교체하여 쓰기 중단 손상을 줄인다.
    try:
        with temp.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction="raise")
            writer.writeheader()
            for file in files:
                for record in read_cache(file)["records"]:
                    if is_citrus(record):
                        writer.writerow(record)
        temp.replace(out)
    finally:
        if temp.exists():
            temp.unlink()
    return len(files), total_rows, citrus_rows


# API 키·기간·캐시 위치를 입력받고 날짜별 수집 → 전체 CSV 재생성 순으로 수행한다.
def main() -> int:
    ap = argparse.ArgumentParser(description="공영도매시장 감귤 경매 원본 CSV 자동 수집")
    ap.add_argument("--start", required=True, help="첫 날짜 YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="마지막 날짜 YYYY-MM-DD")
    ap.add_argument("--markets", nargs="+", default=["110001"],
                    help="도매시장 코드 (기본값 서울가락 110001)")
    ap.add_argument("--output", default="data/raw/auction_trades.csv",
                    help="최종 원본 CSV 위치")
    ap.add_argument("--cache-dir", default="data/.auction_api_cache_mclsf14",
                    help="날짜별 완료 원본 캐시 위치")
    ap.add_argument("--gap", type=float, default=0.25,
                    help="같은 날짜의 페이지 호출 사이 간격(초)")
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--refresh", action="store_true", help="기존 날짜도 API에서 다시 수집")
    args = ap.parse_args()

    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if not key:
        ap.error("DATA_GO_KR_KEY 환경변수에 공공데이터포털 서비스키를 넣어주세요.")
    # 서비스키는 인코딩 형태로 발급될 수 있으므로 중복 URL 인코딩을 피한다.
    key = unquote(key)  # 인코딩 키를 넣었어도 requests가 중복 인코딩하지 않도록
    days = dates_between(args.start, args.end)
    if not days:
        ap.error("날짜 범위가 비어 있습니다.")
    if len(days) * len(args.markets) > 90:
        print("[안내] 요청 범위가 큽니다. 트래픽 한도를 확인하세요. "
              "완료된 날짜는 캐시되어 중단 후 다시 실행할 수 있습니다.")

    cache_dir = Path(args.cache_dir)
    session = make_session()
    failed = False
    try:
        for market in args.markets:
            for day in days:
                try:
                    collect_one_day(session, key, market, day, cache_dir,
                                    args.gap, args.timeout, args.refresh)
                except (requests.RequestException, RuntimeError, ValueError) as exc:
                    print(f"[중단] {market} {day}: {exc}", file=sys.stderr)
                    failed = True
                    break  # 실패를 0건으로 처리하거나 다음 날짜로 넘어가지 않음
            if failed:
                break
    finally:
        session.close()

    if any(cache_dir.glob("*.json")):
        count_dates, count_rows, citrus_rows = export_all_cached(cache_dir, Path(args.output))
        print(f"\nCSV 생성: {Path(args.output).resolve()}")
        print(f"수집 완료: {count_dates}개 날짜·시장 / API 원본 {count_rows:,}건 / CSV 감귤 거래 {citrus_rows:,}건")
        print("※ 원본 JSON 캐시는 모든 응답 행을 보존합니다. CSV에서는 과실류(06)·감귤(14)만 선별합니다.")
        print("※ 제주산/경매 여부 및 거래의 수치·품종·등급은 임의 수정하거나 제외하지 않았습니다.")
    # 일부 날짜 수집이 실패하면 종료 코드를 1로 돌려줘 성공과 구분한다.
    if failed:
        print("실패한 날짜를 확인하고 같은 명령어를 다시 실행하세요. "
              "완료된 날짜는 재수집하지 않습니다.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError) as e:
        print(f"오류: {e}", file=sys.stderr)
        sys.exit(1)
