# 통합 Agent 평가

현재 MVP의 정식 자동 평가 경로는 아래 두 파일입니다.

- `integration_e2e_cases.json`: 통합 대화·도구 선택·계산 경로 평가 사례
- `scripts/evaluate_integrated_agent.py`: 평가셋 검증과 명시적으로 실행한 실제 모델 평가

프로젝트 루트에서 실행하세요.

```cmd
.venv\Scripts\python.exe -m scripts.evaluate_integrated_agent --list
.venv\Scripts\python.exe -m scripts.evaluate_integrated_agent --validate
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

`--list`/`--validate`는 모델 API를 호출하지 않습니다. **실제 모델 평가와 과금은 별도 `--live` 명시가 필요합니다.** 구형 단독 Agent용 V0 평가기, 개인 평가 사례 및 과거 실행 결과는 현재 기본 경로에서 제외되어 이 패치에서 정리했습니다.

검증 성공은 현실 농가에서의 의사결정 품질이나 미래 가격 예측 정확도를 보증하지 않습니다.
