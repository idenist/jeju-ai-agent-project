# 실제 모델 평가 INT-04 실패 분석 및 도구 선택 개선 — STEP 7-2

## 관측된 실패

2026-11-10 모델 `gpt-6-luna`의 첫 번째 12문항·16턴 평가에서 11문항·15턴 통과했습니다. `INT-04`만 실패했습니다.

- 사용자는 정확한 세부 품종을 모르지만 **7일 간격의 과거 경락가격 비교**를 요청했습니다.
- 모델은 `get_price_history` 대신 `explore_data`를 호출해 `variety=감귤`이라는 통칭으로 후보를 찾았습니다.
- 결과가 `NO_DATA`여서 실제 필요한 세부 품종 확인 질문이 나오지 않았습니다.
- 평가 코드의 `missing_tool:get_price_history`는 이를 실패로 판정했습니다.
- 이 실패는 계산 엔진·원천 데이터의 오류가 아닌 **도구 선택 우선순위의 충돌**입니다.

## 수정 범위

1. 과거 가격쌍 조회가 명시된 질문은 세부 품종이 모호하더라도 `get_price_history`를 우선 호출하도록 시스템 지침을 명확히 했습니다.
2. 통칭 감귤·귤·밀감을 `조생귤` 또는 `감귤(일반)`으로 자동 매핑하지 않고, 기존 Python 조회 도구가 세부 품종을 되묻게 합니다.
3. `get_price_history` 도구 설명에도 누락조건 확인 경로를 명시했습니다.
4. 신규 단위 테스트에서 통칭에 대한 `NEEDS_INPUT` 응답 및 지침 존재를 검증합니다.

원천 자료, 계산 엔진, 기존 통합 에이전트의 활성 도구 목록, 평가셋 및 채점 규칙은 변경하지 않습니다. **정적 지침 검수로 실제 모델의 도구 선택 성공을 보증하지는 않습니다.**

## Windows CMD 재검증

```bat
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_integration_price_history_intent.py" -v
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m scripts.evaluate_integrated_agent --validate
```

유료 모델 평가(실제 API 사용료 발생 가능):

```bat
.\.venv\Scripts\python.exe -m scripts.evaluate_integrated_agent --live --case INT-04 --repeat 3
```

기존 실패 기록(`11/12`)은 보존합니다. 새 결과는 새 실행 폴더에 기록해 코드·데이터 SHA-256과 함께 비교합니다. `INT-04`가 안정적으로 통과하는지 확인한 뒤 전체 12문항을 다시 실행하고, 통과율이 개선됐는지 판단합니다.
