# 제주 감귤 출하 의사결정 AI Agent

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

## 양정현 담당 · 과거 가격 비교와 조회 에이전트

팀의 날짜별 경매가격을 입력으로 받아 **같은 시장·품종·등급·크기·포장 종류·포장 중량·거래 방식**의 가격만 정확히 **3·7·14일** 간격으로 연결했습니다. 비교일 자료가 없으면 이유를 남기고, 적은 거래 건수나 미상 규격 등 품질 경고가 있는 쌍은 기본 사례와 구분합니다.

| 기여 | 코드와 결과 |
|---|---|
| 비교 데이터 생성 | [build_price_comparisons.py](scripts/build_price_comparisons.py), [비교 가능 범위 보고서](data/reports/price_comparison_coverage.md) |
| 비교 방법 실험 | [explore_price_methods.py](scripts/explore_price_methods.py). 정확히 7일 290건, 가까운 거래일 361건, 주간 가중평균 246건을 비교했습니다. |
| 자연어 조회 | [agent.py](agents/jeonghyeon/agent.py), [price_history.py](agents/jeonghyeon/price_history.py). ‘특등급·소과’를 ‘특·소’로 맞추고 자료에 있는 조건으로 조회합니다. |
| 조건 추출 평가 | [평가 스크립트](scripts/evaluate_price_extraction.py), [21턴 평가셋](scripts/price_extraction_cases.json). 미지정 조건 추가, 변경 미반영, 기존 조건 유실을 따로 집계합니다. |

실제 시연에서는 조생귤·특·소·상자 5kg의 **7일 비교 20쌍**을 조회했고, “간격만 14일로 바꿔 줘”라는 후속 질문에 같은 규격을 유지해 **15쌍**을 보여줬습니다. [실제 평가 원본](data/reports/evaluation/extraction_result_1.json)은 `gpt-5-mini`의 응답입니다. [같은 응답의 재채점](data/reports/evaluation/extraction_result_1_replay.json)에서는 자료 표기를 맞춘 뒤 정답과 일치한 턴이 **9/21 → 19/21**로 바뀌었습니다.

### 아키텍처

```text
데이터 준비
  팀의 일별 경매가격 CSV → build_price_comparisons.py → 3·7·14일 비교 CSV

자연어 조회
  사용자 질문 → agent.py → OpenAI Responses API
    → get_price_history 함수 호출 인자 → price_history.py
    → 조건 검증·표기 변환 → 비교 CSV 조회
    → Python이 결과·품질 경고·자료 부족 사유를 출력
```

### Tool Calling 사용 방식

1. **질문에서 조건 추출.** `agent.py`는 Responses API에 `get_price_history` 함수 스키마를 등록하고 호출할 도구로 지정합니다. 스키마에 품종·등급·크기·포장·기간 등 조회 조건과 요청 의도를 정의합니다. 사용자가 말하지 않은 값은 `null`로 전달하도록 지시합니다.
2. **호출 인자 확인 후 로컬 조회.** 모델이 반환한 함수명과 JSON 필드를 검사합니다. 과거 가격 조회 요청이면 Python에서 도구를 실행합니다. `price_history.py`는 자료 표기에 맞춰 규격을 바꾸고, 동일 조건의 비교 자료를 조회합니다. 필요한 조건이 빠졌으면 사용자에게 되묻습니다.
3. **조회 결과를 화면에 표시.** Python이 비교 건수, 변동률, 품질 경고를 포맷해 답변합니다. 후속 질문에서는 같은 대화 기록을 함께 전달해 “간격만 14일로” 같은 변경을 해석합니다.

### 담당 기능 실행과 재현

```powershell
python -X utf8 -m agents.jeonghyeon.agent --demo
python -X utf8 -m unittest discover -s tests
python -X utf8 scripts/explore_price_methods.py
python -X utf8 -m scripts.evaluate_price_extraction --replay data/reports/evaluation/extraction_result_1.json
```

자연어 조회와 새 모델 평가는 OpenAI SDK와 `OPENAI_API_KEY` 설정이 필요합니다. 두 실행 명령의 기본 모델은 **`gpt-6-luna`**이고, 아래처럼 `--model`을 지정하면 터미널의 `OPENAI_MODEL` 설정보다 우선합니다. 자세한 방법은 [정현 에이전트 실행 안내](docs/정현_에이전트_실행안내.md)에 있습니다.

```powershell
python -m pip install openai
python -X utf8 -m agents.jeonghyeon.agent --model gpt-6-luna
python -X utf8 -m scripts.evaluate_price_extraction --model gpt-6-luna --output extraction_result_luna.json
```
