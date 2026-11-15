# 제주 감귤 출하 의사결정 AI Agent

이 저장소는 **관측된 경매가격·제주 출하통계**와 **사용자가 입력한 비용·감모·단가 가정**을 구분하여, 출하 대안의 조건부 손익을 계산하는 Streamlit MVP입니다.

> 기준 브랜치: `refactor/lean-mvp` (패치 기준 커밋 `0de631a`). 예측이나 최적 출하일 보증이 아니라 **사용자가 정한 조건의 비교**입니다. 경락가격은 농가 실수취액이 아니며, 통계상의 출하량은 실시간 공급량이 아닙니다.

## Windows에서 실행

프로젝트 루트에서 다음을 실행합니다.

```cmd
setup-center.cmd
run-juneok.cmd
```

- `setup-center.cmd`: Python 3.13 가상환경 및 잠금 의존성 설치. 기존 `.env`는 덮어쓰지 않습니다.
- `run-juneok.cmd`: 로컬 Streamlit 실행.
- 자연어 상담을 사용하려면 프로젝트 루트의 `.env`에서 `OPENAI_API_KEY`를 설정하세요. **키가 없어도** 직접 입력·CSV 조회·조건부 계산·보고서 기능을 쓸 수 있습니다.
- 원천 API 갱신은 별도 운영 작업이며, 실행만으로 기존 공용 원본을 덮어쓰지 않습니다.

## 핵심 기능과 코드 위치

| 기능 | 주 구현 |
|---|---|
| 화면·사용자 흐름 | `app.py`, `agents/juneok/future_pair_ui.py` |
| 대화·상태·도구 호출 | `agents/juneok/agent.py`, `workspace.py`, `tools.py`, `state.py`, `future_pair_state.py` |
| 독립 손익 계산 | `agents/juneok/comparison.py`, `agents/common/calculation_service.py` |
| 두 미래 출하일 조건 비교 | `agents/common/future_pair.py` |
| 과거 동일 규격 가격·가격 변동 | `agents/jeonghyeon/price_history.py`, `price_scenario.py` |
| 과거 출하량·시기별 조회 | `agents/seunghyuk/shipment_timing.py`, `shipment_timing_bundle.py` |
| 시즌 가격·손익기준 비교 | `agents/common/seasonal_prices.py`, `seasonal_threshold.py` |
| 결과·PDF/JSON | `agents/juneok/reporting.py`, `pdf_report.py`, `market_export.py` |
| 데이터 무결성 검사 | `agents/common/data_contract.py`, `result_contract.py` |

기본 실행에 쓰이는 `agents/juneok/` 구성 요소는 서로 연결되어 있습니다. 사용하지 않는 이름으로 보인다는 이유만으로 삭제하지 마세요. `agents/seunghyuk/forecast_*`는 **실험 모드로 격리된 연구용 코드**이며 일반 UI의 예측 기능이 아닙니다.

## 자료

| 경로 | 용도 | 처리 원칙 |
|---|---|---|
| `data/raw/` | 수집 원천 | 변경·삭제하지 않음 |
| `data/processed/` | 현재 서비스의 기본 가격·출하량·가격쌍 | 묶음의 버전 일치 확인 |
| `data/metadata/` 및 `data/reports/` | 처리·무결성·품질 근거 | 데이터와 함께 유지 |
| `data/seasons/` | 별도 시즌별 관측자료 및 연구 데이터 | 현재 상담의 기본 가격쌍과 자동 혼합 금지 |
| `assets/fonts/`, `assets/licenses/` | PDF 한글 글꼴·외부 라이선스 | 배포 전 고지 조건 확인 |

원천 재생성: `scripts/preprocess_auction.py`, `scripts/preprocess_shipments.py`, `scripts/build_price_comparisons.py`, `scripts/run_auction_pipeline.py`. 추가 원본 수집: `scripts/collect_auction_raw.py`.

## 검증

```cmd
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
.venv\Scripts\python.exe -m scripts.evaluate_integrated_agent --validate
.venv\Scripts\python.exe -m scripts.verify_integration_handoff
```

위 명령은 로컬 회귀·검증용입니다. `--live`를 명시하는 실제 모델 평가는 별도 API 비용이 발생할 수 있습니다. 통합 평가 사례는 `evals/integration_e2e_cases.json`이며 사용법은 `evals/README.md`에서 확인하세요. **패치 자체만으로 테스트가 통과했다고 주장하지 않습니다.**

## 문서와 정리 이력

- `docs/01`~`docs/06`: 기존 팀의 상세 명세·운영·테스트 문서. 문서의 일부 커밋·브랜치 표기는 작성 당시의 역사적 기준입니다.
- `docs/정현_AB입력변환_계약.md`: 입력 변환 계약 검토 자료.
- `docs/임준억/제출/데이터활용명세.md`, `외부라이선스목록.md`: 출처·배포 관련 참고 자료로 보존.
- 개인 작업일지, 지난 시연·검증 결과, 폐기 문서 및 구형 독립 평가기는 Git 이력에서 복원 가능합니다. **Git 이력이나 원본 데이터는 지우지 않습니다.**

로컬에만 존재하는 `dialogue_engine.py`, `sale_window.py` 등의 미커밋 변경 사항은 이 원격 기준 패치의 검증 대상이 아닙니다.
