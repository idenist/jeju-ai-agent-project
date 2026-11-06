# 제주 감귤 출하 의사결정 AI Agent

제주 감귤의 **즉시 출하와 지연 출하**를 비교하는 팀 프로젝트입니다. 도매시장 경매가격과 사용자 입력 조건(물량·보관비·감량률)을 활용하여 **지연 출하 손익분기 단가**를 계산합니다.

> **현재 구현 범위:** `seunghyuk` 브랜치에는 Python 기반 계산 베이스라인과 OpenAI Responses API **Tool Calling Agent**가 구현되어 있습니다. 미래 가격 예측 모델이나 출하 시점의 확정적인 추천 기능은 아직 없습니다. **경락가격은 농가 실제 수취가격과 다릅니다.**

## 문서 읽는 순서

| 순서 | 문서 | 대상·내용 |
|---|---|---|
| **01 (먼저)** | [Agent 기술 설명서](docs/01_Agent_기술설명서.md) | 전 팀원: 목적, 구조, 도구, 실제 시연, 한계 |
| **02 (그다음)** | [Agent 실행 및 검증 안내](docs/02_Agent_실행_및_검증.md) | 개발·시연 담당: CMD 실행, `.env`, 테스트, 베이스라인 단독 실행, 수치 일관성 |
| 03 (필요할 때) | [경매가격 전처리 안내](docs/03_경매가격_전처리.md) | 데이터 담당: 경매 원본, 필터링, 집계, 품질 검증 |
| 04 (필요할 때) | [제주 출하량 전처리 안내](docs/04_제주_출하량_전처리.md) | 데이터 담당: 출하량 원본, 누락 처리, 월별·일별 집계 |

**처음 온 팀원은 01 → 02만 읽으면 Agent 구조를 파악하고 실행할 수 있습니다.** 03·04는 원본 데이터나 전처리 규칙을 확인할 때 찾아보세요. 이전의 `베이스라인_실행안내.md`, `수치_일관성_수정_안내.md` 내용은 02에 통합했습니다.

## 빠른 실행 (Windows CMD)

명령어는 프로젝트 최상위 폴더에서 실행합니다. Python 3.10 이상 권장.

```bat
cd /d "C:\SKN projects\jeju"
python -m pip install -r requirements.txt
if not exist .env copy .env.example .env
notepad .env
python -m unittest discover -s tests -v
python agents\agent.py --show-tools
```

- Conda를 사용하는 경우 설치 전에 원하는 환경을 먼저 활성화합니다. 예: `conda activate test_env`.
- `.env`에 **OpenAI 플랫폼의 실제 `OPENAI_API_KEY`**를 설정합니다. 모델은 `OPENAI_MODEL`로 지정합니다.
- `.env`는 Git 추적 대상이 아닙니다. **실제 키를 커밋하지 마세요.** `.env.example`만 공유합니다.
- API로 새 경매자료를 수집할 때만 `DATA_GO_KR_KEY`가 별도로 필요합니다.
- 상세 명령·인증 오류·베이스라인 실행법은 [02 실행 및 검증](docs/02_Agent_실행_및_검증.md)을 참고합니다.

## 프로젝트 구조

```text
jeju/
├── agents/
│   ├── agent.py                 # 자연어 질문·Tool Calling·결과 표시
│   └── baseline.py              # 경매가격 조회·손익분기 계산
├── data/
│   ├── raw/
│   │   ├── auction_trades.csv   # 경매원천정보 API 수집본
│   │   └── jeju_shipments.csv  # 제주데이터허브 #855 원본
│   ├── processed/
│   │   ├── daily_prices.csv         # 동등 거래 조건별 일일 경매가격
│   │   ├── daily_variety_prices.csv # 품종별 일일 가격 동향(참고용)
│   │   ├── daily_shipments.csv      # 품종별 일일 출하량
│   │   └── monthly_shipments.csv    # 품종별 월간 출하량
│   └── reports/                  # 품질 보고서·날짜 누락 기록
├── scripts/
│   ├── collect_auction_raw.py
│   ├── preprocess_auction.py
│   └── preprocess_shipments.py
├── tests/                       # 베이스라인·Tool Calling·환경설정 테스트
├── docs/
│   ├── 01_Agent_기술설명서.md
│   ├── 02_Agent_실행_및_검증.md
│   ├── 03_경매가격_전처리.md
│   └── 04_제주_출하량_전처리.md
├── .env.example                 # 인증키가 없는 환경설정 예시
├── .gitignore
└── requirements.txt
```

## 데이터 및 적용 범위

| 데이터 | 범위·용도 | 주의사항 |
|---|---|---|
| `daily_prices.csv` | 서울가락시장 2026-10-01 ~ 2026-11-03 경매자료, 동등 조건 가격 조회 | 현재 Agent가 실제 사용하는 가격 데이터. 실시간 가격 아님 |
| `daily_variety_prices.csv` | 품종별 가격 동향·거래량 참고 | 크기·등급이 혼합되어 단독 손익 판단 근거로 쓰지 않음 |
| `daily_shipments.csv`, `monthly_shipments.csv` | 2018-01-01 ~ 2025-09-17 과거 제주 신고소별 출하량 | 전처리 완료, **현재 개인 Agent에는 미연결** |
| 사용자 입력 | 물량, 지연일수, 보관비, 전체 감량률, 선택적 가정가격 | 실제 미래 가격을 임의로 생성하지 않음 |

가격은 동일 시장·품종·등급·크기·포장중량 조건에서 **총낙찰금액 ÷ 총중량(kg)**으로 산출합니다. 제주 출하량 자료의 단위는 **톤**이고, 한라봉 날짜 누락 기록은 추정하지 않고 따로 보관합니다. 경매 품종명과 출하량 품종명은 자동으로 일대일 대응시키지 않습니다.

## 팀 개발 원칙 및 출처

- `data/raw/` 원본을 수정하지 않습니다. 전처리 규칙 변경 시 스크립트 및 처리 결과를 함께 검증합니다.
- Agent에는 `data/processed/`를 사용하고, `data/reports/`는 전처리 품질 확인에 사용합니다.
- 인증키, `.env`, 개인 API 응답 캐시는 커밋하지 않습니다.
- 경매 원본: [공공데이터포털 전국 공영도매시장 경매원천정보 API](https://www.data.go.kr/data/15141810/openapi.do)
- 출하량 원본: 제주데이터허브 #855 「신고소별 감귤류 및 만감류 출하량 통계」
