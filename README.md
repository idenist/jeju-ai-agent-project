# 제주 감귤 출하 대안 비교 — Lean MVP

이 저장소는 과거 경매가격/출하량을 근거로 출하 **가정의 경제성**을 비교하는 Streamlit 앱입니다.
관측 자료를 미래 가격 예측 또는 확정적 출하 권고로 해석하지 않습니다.

## 실행

```cmd
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-juneok.lock
.venv\Scripts\python.exe -m streamlit run app.py
```

모델 API 키가 없으면 대화 기능이 제한되지만 CSV 조회와 Python 계산은 별도로 확인할 수 있습니다.

## 구조

- `app.py`: Streamlit 진입점만 담당
- `ui/application.py`: 전체 화면 구성
- `ui/context.py`: 공유 Streamlit 세션 동작
- `ui/chat.py`: 대화형 화면
- `ui/panels.py`: 비교 카드 및 자료/도구 화면
- `ui/`: 보고서, PDF, 화면 표시와 기타 UI 도우미
- `agents/`: 대화 엔진, 도구 라우팅 및 대화 상태/하네스
- `core/`: 공통 설정·입력 규약
- `presenters/`: 텍스트·표·보고서 출력 규칙
- `services/`: 가격 조회, 경제성 계산, 출하량, 정산 및 데이터 계약
- `experiments/`: 운영 MVP 밖의 실험용 미래가격 모델
- `data/raw/`, `data/processed/`, `data/seasons/`: 원천/가공/시즌별 데이터
- `scripts/`: 데이터 수집, 전처리, 갱신 및 통합 검증
- `evals/`: 대화 평가셋, 평가 도구 및 하네스 검증
- `tests/`: 계산·데이터·도구·UI 회귀 검증

각 팀원의 이름을 패키지 구조로 사용하지 않습니다. import 경로는 기능 중심으로 통합되었습니다.

## 검증

```cmd
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
.venv\Scripts\python.exe -m scripts.verify_integration_handoff --offline-only
```

실제 모델 E2E 평가는 API 키 및 호출비용이 필요하므로 별도 수행합니다.

## 데이터 및 주의사항

기존 데이터의 바이트 단위 무결성과 가정/관측 구분을 보존합니다.
특히 경매 가격은 실수취액이나 미래 가격을 자동으로 의미하지 않으며,
서로 다른 거래일의 관측가격 차이는 동일 감귤의 보관 수익이 아닙니다.
라이선스 고지·데이터 원본·정규화 보고서는 정리 대상에서 제외했습니다.

이전 버전의 상세 설명과 개발 과정은 Git 기록 및 `docs/`의 기술 문서에서 확인할 수 있습니다.

2차 구조·검증 기준은 [`docs/07_2차_리팩토링_운영기준.md`](docs/07_2차_리팩토링_운영기준.md)을 참조하세요.
