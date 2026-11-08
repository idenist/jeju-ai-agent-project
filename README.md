# 제주 감귤 출하 의사결정 AI Agent

## 임준억 개인 MVP 실행

개인 문서는 [docs/임준억 문서안내](docs/임준억/문서안내.md)에 모았습니다. 누적 [작업기록](docs/임준억/작업기록/목차.md)과 [작성기준](docs/임준억/작업기록/작성기준.md)을 따릅니다. 최신 환경 판정은 [공개 센터 사양 기준 적합성](docs/임준억/검증/2026-11-09_공개사양과업무적합성.md)이며 실제 장비 접근을 현재 개발의 필수 조건으로 두지 않습니다.

개인 기능은 `agents/juneok/`에 있습니다. 원본·전처리는 공통 main을 보존하며 로컬 `dev/juneok` 브랜치에서 개발합니다.

- 빠른 시작: [개인 MVP 실행 안내](docs/임준억/안내/실행안내.md)
- 도구 입력·출력: [개인 도구 계약](docs/임준억/안내/도구와상태계약.md)
- 구현·검증 상태: [진행 기록](docs/임준억/현황.md)
- 환경: `.venv/`, 사용자 키 파일: 제품 루트 `.env` (둘 다 Git 제외)

```powershell
.\.venv\Scripts\python.exe -m agents.juneok.cli --check
.\.venv\Scripts\python.exe -m agents.juneok.cli --demo --save-report
.\.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
```

대화와 조건 카드가 같은 상태·버전을 사용하며 미리보기/PDF를 기본으로 제공합니다. 새 별도 환경 회귀139개, 기존 실제 모델25/25와 신규 대화→카드→대화→PDF 검증을 통과했습니다. **센터 데이터 조회는 CSV이며 경매 API는 사전 수집용**입니다. [공유 상태·PDF 검증](docs/임준억/검증/2026-11-09_공유상태와PDF.md), [현재 계획](docs/임준억/계획/개발계획.md), [센터 실행](docs/임준억/안내/센터실행안내.md)을 확인하세요. 실제 농가 효과와 실제 센터 장비 실행은 미검증이며 공개 사양 기반 실행을 개발 기준으로 삼습니다. 이전 검증은 이력으로 보존합니다. 아래는 기존 공통 데이터 안내입니다.

제주 감귤의 **즉시 출하 vs. 지연 출하**를 비교하는 팀 프로젝트입니다. 도매시장 거래가격과 과거 제주 출하량을 조회하고, 사용자에게 받은 **물량·보관비·감량률**을 바탕으로 손익분기 조건을 설명하는 AI Agent를 구현합니다.

> **주의:** 현재 프로젝트의 가격 데이터는 과거 경매 거래 결과입니다. 미래 가격이나 수익을 보장하는 예측값이 아닙니다. 도매시장 경락가격도 농가의 실제 수취가격과 다릅니다.

## 1. 프로젝트 구조

```text
jeju/
├── agents/                   # 팀원별 Agent 구현
├── data/
│   ├── raw/
│   │   ├── auction_trades.csv       # 경매원천정보 API에서 수집한 감귤 거래
│   │   └── jeju_shipments.csv      # 제주데이터허브 #855 원본
│   ├── processed/
│   │   ├── daily_prices.csv         # 동일 조건별 일일 경매가격
│   │   ├── daily_variety_prices.csv # 품종별 일일 가격 동향
│   │   ├── daily_shipments.csv      # 품종별 일일 출하량
│   │   └── monthly_shipments.csv    # 품종별 월간 출하량
│   ├── reports/
│   │   ├── auction_quality_report.json  # 경매가격 검증·제외 사유
│   │   ├── shipment_quality_report.json # 출하량 검증·누락 통계
│   │   └── shipment_unassigned_rows.csv # 날짜 누락 등 미배정 원본
│   ├── .auction_api_cache/         # 이전 방식 수집 캐시 (Git 제외)
│   └── .auction_api_cache_mclsf14/ # 현재 수집 방식 캐시 (Git 제외)
├── docs/
│   ├── 감귤_경매가격_전처리_실행안내.md
│   └── 제주_감귤_출하량_전처리_실행안내.md
├── scripts/
│   ├── collect_auction_raw.py
│   ├── preprocess_auction.py
│   └── preprocess_shipments.py
├── .gitignore
├── requirements.txt
└── README.md
```

**출력 경로:** 두 전처리 스크립트 모두 집계 CSV는 `data/processed/`, 품질 보고서·날짜 미배정 기록은 `data/reports/`에 저장합니다. `--report-dir` 옵션으로 보고서 경로를 별도로 바꿀 수도 있습니다.

## 2. 환경 설정

- **Python 3.10 이상** 권장
- 명령어는 반드시 프로젝트 최상위 폴더(`jeju/`)에서 실행

```powershell
python -m pip install -r requirements.txt
```

경매 API **수집 시에만** 공공데이터포털 서비스키가 필요합니다. PowerShell에서 해당 터미널 세션에 설정하세요.

```powershell
$env:DATA_GO_KR_KEY = "본인의_공공데이터포털_인증키"
```

인증키를 코드·문서·Git에 저장하거나 오류 로그의 요청 URL을 공유하지 마세요.

## 3. 실행 순서

### A. 이미 확보한 원본 CSV 전처리 (인터넷·API 키 불필요)

```powershell
python scripts/preprocess_auction.py
python scripts/preprocess_shipments.py
```

입력: `data/raw/auction_trades.csv`, `data/raw/jeju_shipments.csv`  
출력: 집계 CSV 4종 → `data/processed/`, 검증 보고서·날짜 미배정 기록 → `data/reports/`

### B. 경매 원본 추가 수집 (필요할 때만)

```powershell
python scripts/collect_auction_raw.py --start 2026-10-01 --end 2026-11-03 --markets 110001
python scripts/preprocess_auction.py
```

- 시장코드 `110001`은 **서울가락시장**입니다.
- 현재 수집기는 **감귤 중분류 코드 `14`만 API에 전달**하고, 반환된 거래 중 과실류(`06`)·감귤(`14`)을 골라 `auction_trades.csv`를 만듭니다.
- 전체 요청 응답 캐시는 `data/.auction_api_cache_mclsf14/`에 저장되며 Git에서는 제외합니다.
- 수집 기간을 늘린 뒤에는 반드시 경매가격 전처리를 **다시 실행**하세요.
- 기존 날짜 캐시를 삭제할 필요는 없습니다. 단, 수집기 옵션·캐시 경로를 변경한 경우 이전 데이터 포함 여부를 확인하세요.

## 4. 데이터별 Agent 활용

| 데이터 | 활용 방법 | 해석상 주의 |
|---|---|---|
| `daily_prices.csv` | 동일 **시장·품종·등급·크기·포장중량** 조건에서 일자별 경락가격 비교 | 등급·크기가 `미상`인 행은 구분하여 사용 |
| `daily_variety_prices.csv` | 품종별 전체 가격 추세·거래량 요약 | 규격 구성이 달라질 수 있어 의사결정 단독 근거로 사용하지 않음 |
| `daily_shipments.csv` | 제주 감귤류 품종별 과거 일일 출하 동향 | 시장 거래량과 같은 지표가 아님 |
| `monthly_shipments.csv` | 과거 계절별 출하 집중 시기 파악 | 2026년 실시간 출하량이 아님 |
| 사용자 입력 | 출하 물량, 보관기간, 추가 보관비, 감량·부패율 | 값을 모르면 임의의 실제 수치로 단정하지 않음 |

**가격 처리:** 유효 경매 거래의 `총낙찰금액 ÷ 총중량(kg)`으로 물량 가중평균가격을 계산합니다.  
**출하량 처리:** 신고소별 물량을 날짜·품종별로 합산합니다. 단위는 **톤(t)**입니다.

- 경매가격 원본: 2026-10-01 ~ 2026-11-03, 서울가락시장 기준 수집본
- 제주 출하량 원본: 2018-01-01 ~ 2025-09-17, 마지막 월은 부분 집계
- 출하량 원본의 **날짜가 없는 한라봉 기록**은 날짜를 추정하지 않고 별도 CSV에 보관합니다.
- `조생귤`(경매)과 `노지온주`(출하량) 등 서로 다른 분류체계를 확인 없이 일대일 매핑하지 않습니다.
- `daily_variety_prices.csv`는 등급·크기 구성이 혼합된 참고용 요약이고, 동일 조건 비교에는 `daily_prices.csv`를 우선 사용합니다.

## 5. 팀 개발·공유 규칙

1. `data/raw/`의 원본 파일은 직접 수정하지 않습니다.
2. 전처리 규칙을 바꾸면 해당 스크립트를 수정하고 `data/processed/`를 재생성합니다.
3. Agent는 가능하면 `data/processed/`의 가공 CSV를 읽고, 보관비·감량률 등은 사용자 입력으로 받습니다. `data/reports/`는 검증·오류 분석용입니다.
4. Git에 올리기 전 `.gitignore`가 적용됐는지 `git status`로 확인합니다. **API 캐시·인증키는 올리지 않습니다.**
5. 신규 팀원은 `requirements.txt` 설치 → 전처리 스크립트 실행 → `agents/`에서 개발을 시작합니다.

## 6. 데이터 출처 및 자세한 설명

- [공공데이터포털 — 전국 공영도매시장 경매원천정보 API](https://www.data.go.kr/data/15141810/openapi.do)
- 제주데이터허브 #855 **「신고소별 감귤류 및 만감류 출하량 통계」**
- 전처리 세부 규칙·컬럼 명세: `docs/감귤_경매가격_전처리_실행안내.md`, `docs/제주_감귤_출하량_전처리_실행안내.md`

### 현재 단계

경매가격·제주 출하량 **수집 및 전처리 파이프라인 구축 완료**. 다음 작업은 팀원별 Agent 프로토타입 구현과, 필요시 추가 시즌 데이터 확보입니다.

임준억의 로컬 MVP 인수 후보는 [팀 인수 안내](docs/임준억/인수/팀인수안내.md)에서 설치·사용·시연·통합 검토를 확인합니다. 공통 전처리 안내와 구분하며 팀 최종 채택·통합 완료를 뜻하지 않습니다.
