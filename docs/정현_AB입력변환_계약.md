# 미래 A·B 입력 변환 계약 — `build_future_pair_input()`

> 담당: 양정현 · 파일: `agents/jeonghyeon/comparison_input.py` 
> 호출부: `agents/common/future_pair.py` (`compare_future_pair`, 준억님 주관)
> 정현 담당분. 계약을 바꿀 때는  변경 절차(제안 → 영향 파일·테스트 명시 → 상대 리뷰 → 양측 테스트 통과)를 따른다.

---

## 1. 이 함수가 하는 일 / 하지 않는 일

| 한다 | 하지 않는다 |
|---|---|
| A안 1개 + B안 가정 3개(최저·중앙·최고)를 손익 엔진의 **대안 형식**으로 만든다 | 금액·차액·손익분기 계산 (→ `compare_future_pair`) |
| 가격마다 **근거(evidence)** 를 대안 id별로 붙인다 | 화면 표시, PDF·JSON 저장 |
| 과거 비교가 불가능하면 **직접 입력 경로**로 넘긴다 | 이전 가정 단가 재사용, 모르는 값을 0으로 채우기 |

---

## 2. 입력

```python
build_future_pair_input(args, *, option_a, option_b, **paths)
```

### 2.1 `args` — 가격 조회 조건

| 키 | 형식 | 필수 | 설명 |
|---|---|---|---|
| `variety` `grade` `size` `package` | 문자열 | 예 | 자료 표기 또는 별칭(특등급→특, 소과→소, 박스→상자). 통칭 '감귤'은 품종으로 쓰지 않고 다시 묻는다 |
| `package_kg` | 숫자/문자열 | 예 | 포장 1개 무게(kg). 포장명 속 무게와 다르면 `INVALID_INPUT` |
| `market_code` `trade_type` | 문자열 | 아니오 | 자료에 하나뿐이면 자동 적용(서울가락 110001·경매) |
| `option_a_sale_date` | `YYYY-MM-DD` | 예 | A안 예상 판매일 |
| `option_b_sale_date` | `YYYY-MM-DD` | 예 | B안 예상 판매일. A보다 늦어야 함 |
| `option_a_price_per_kg` | 숫자 | 예 | A안 **사용자 입력 가정** 단가. 없으면 `NEEDS_CLARIFICATION` |

이 밖의 키는 `INVALID_INPUT`.

### 2.2 `option_a` / `option_b` — 손익 계산용 보관 조건

| 키 | 형식 | 규칙 |
|---|---|---|
| `delay_days` | 정수 | 1~365, **B > A** |
| `loss_percent` | 숫자 또는 `None` | 0 이상 100 미만. 그 기간 **전체** 감모율 |
| `daily_storage_cost` | 숫자 또는 `None` | 0 이상(원/일) |

이 세 키 외에는 받지 않는다(수수료·별도 비용은 호출부가 엔진 입력에 직접 넣음).

### 2.3 두 간격은 서로 다른 값

| 값 | 어디서 오나 | 어디에 쓰나 |
|---|---|---|
| **가격 비교 간격** = B 판매일 − A 판매일 | `args`의 두 날짜 | 과거 동일 규격 가격쌍 조회 (3·7·14일만 지원) |
| **보관일** `delay_days` | `option_a` / `option_b` | 감모·보관비 계산 |

현재 호출부에서는 두 안이 같은 보관 시작일을 쓰므로, **판매일 차이는 B 보관일 − A 보관일과 같습니다.** 예: A 3일·B 10일이면 가격 조회 간격은 7일이고, 비용 계산에는 각 안의 3일과 10일을 적용합니다. 이 함수는 두 값을 따로 받으므로 보관 시작일이 달라지는 경우에도 계약은 그대로입니다.

---

## 3. 출력 (상태별)

### 3.1 `OK`

```text
{
  status: "OK",
  mode: "future_pair",
  comparison_input: {
    current_price_per_kg: "<A안 단가>",        # 엔진 필수 칸을 채우는 내부 기준값
    option_a:  {id:"option_a", delay_days, loss_percent, daily_storage_cost, future_price_per_kg},
    options_b: [ {id:"past_min_7d", ...}, {id:"past_median_7d", ...}, {id:"past_max_7d", ...} ]
  },
  evidence: { "option_a": {...}, "past_min_7d": {...}, ... },   # id로 연결
  cautions: [...4개 고정 문구...],
  hidden_engine_fields: ["difference_krw","break_even_price_per_kg","minimum_whole_krw_per_kg","current_proceeds_krw"],
  message: "<내부 기준값 안내>"
}
```

- B안 id는 `past_{min|median|max}_{간격}d`.
- 숫자는 소수 오차 없는 문자열(Decimal). 모르는 값은 `None` 그대로.
- **`hidden_engine_fields`**: 엔진의 '오늘 대비' 결과는 내부 기준값에 따라 바뀌므로 둘 다 미래인 경우 **화면에 보여주지 않는다**.

**evidence 항목**

| 키 | 내용 |
|---|---|
| `price_type` | 항상 `"assumption"` (관측가격·예측가격 아님) |
| `summary` | 화면·PDF 한 줄. 예: `B안 2,409.37원/kg · 동일 규격 7일 과거 20쌍의 중앙값 적용 · 가정` / `A안 2,500원/kg · 사용자 입력 가정` |
| `change_pct`, `label` | 적용한 과거 변동률과 이름 (B안만) |
| `price_interval_days`, `delay_days` | 가격 비교 간격과 보관일 (분리 표기) |
| `clean_pairs`, `base_date_range` | 사용한 경고 없는 사례 수와 기준일 범위 → **표본 크기 표시** |
| `conditions`, `source` | 표준화된 조회 조건, 비교 자료 경로·버전 |

**cautions (항상 함께 표시)**
1. B안 단가는 과거 변동을 대입한 가정이며 가격 전망이 아닙니다.
2. 최솟값·최댓값은 각각 한 사례이고, 날짜 구간이 겹쳐 사례 수를 확률로 해석할 수 없습니다. ← **날짜쌍 중복**
3. 중앙값은 사례 수가 짝수면 가운데 두 값의 평균이라 실제 한 사례가 아닐 수 있습니다.
4. 경락가격 기준이며 농가 실수취액이 아닙니다.

### 3.2 계산 입력을 반환하지 않는 상태

조회 불가 응답(`NEEDS_CLARIFICATION`, `INTERVAL_NOT_AVAILABLE`, `NO_DATA`, `NO_CLEAN_COMPARISON`, `AMBIGUOUS_CODES`)에는 `comparison_input: None`이 들어갑니다. `ERROR` 응답은 `error_kind`, `error`로 사유를 전달하며 `comparison_input` 키가 없을 수 있습니다.

| 상태 | 언제 | 추가 필드 | 호출부 동작 |
|---|---|---|---|
| `NEEDS_CLARIFICATION` | 규격·A안 단가·B 판매일 누락 | `missing_fields`, `choices`, `message` | 빠진 값을 물음 |
| `INTERVAL_NOT_AVAILABLE` | 간격이 3·7·14일이 아님 | `manual_price_required: true`, `message` | **B안 단가 직접 입력** 경로 |
| `NO_DATA` / `NO_CLEAN_COMPARISON` | 규격 사례 없음 / 경고 없는 쌍 없음 | 같음 | 같음 |
| `AMBIGUOUS_CODES` | 같은 이름에 코드가 여러 개 | 같음 | 같음 |
| `ERROR` | `INVALID_INPUT`(입력 형식·범위) / `DATA_ERROR`(가격표·비교표 버전 불일치, 파일 없음) | `error_kind`, `error` (`comparison_input` 없음) | 계산 중단, 사유 표시 |

- 직접 입력 안내 문구("B안 단가를 직접 입력하면…")는 **한 번만** 나온다.
- 상태가 OK가 아니면 **이전 가정 단가를 절대 재사용하지 않는다** (B 날짜를 바꾼 뒤 간격이 사라진 경우 포함).

---

## 4. 호출 방법 (방식 B, 통합 구현과 동일)

```text
1) r = build_future_pair_input(args, option_a=..., option_b=...)
2) r.status != OK → 위 표대로 처리
3) 엔진 공통 입력에 current_price_per_kg = r.comparison_input.current_price_per_kg
4) 엔진 호출 ① options=[option_a]          → A 계산금액
   엔진 호출 ② options=options_b (최대 3개)  → B 시나리오별 계산금액
5) 비교 함수(통합 쪽): B − A 차액, A 기준 B 손익분기 단가
6) 화면: A·B 금액 + evidence[id].summary + cautions. hidden_engine_fields는 숨김
```

검산 예 (3톤, A 3일·감모1%, B 10일·감모2%, 보관비 2만 원/일, 수수료·별도비용 0, A 2,500원): A 7,365,000원 · B 중앙 −481,452.2원 · B 손익분기 2,573.13원/kg. 내부 기준값을 2,500→9,999로 바꿔도 세 값 불변.

---

## 5. 경계값·오류 테스트 대응표

| 역할지침서 검증 항목 | 테스트 |
|---|---|
| A 1개·B 3개가 엔진 대안 형식 | `test_future_pair_input.test_one_a_and_three_b_options_in_team_format` |
| 가격 간격 ≠ 보관일 | `test_b_prices_and_storage_days_are_kept_apart`, `test_comparison_input.test_price_interval_and_storage_days_are_separate` |
| 근거가 계산 입력과 분리, id로 연결 | `test_evidence_per_id`, `test_evidence_is_kept_apart_from_calculation_input` |
| B 날짜만 변경 → 새 가정 단가 | `test_changing_only_b_date_recalculates_price`, `test_integration_future_pair.test_b_date_change_preserves_a_and_uses_new_pairs_and_costs` |
| 가격쌍 없음 → 직접 입력, 이전 가격 재사용 없음 | `test_missing_a_price_and_unsupported_gap_give_no_input`, `test_unsupported_gap_does_not_reuse_previous_price`, `test_missing_interval_allows_manual_and_exports_same_amounts` |
| 누락값을 0으로 대체 금지 | `test_unknown_costs_stay_none`, `test_unknown_costs_stay_empty` |
| 보관일·감모 경계값 (0일, B≤A, 감모 100%, 허용 외 키) | `test_bad_storage_inputs`, `test_bad_storage_days_are_input_errors` |
| A 단가 누락 → 질문 | `test_missing_a_price_asks_instead_of_building_options` |
| 3일 A / 10일 B 독립 검산 | `test_integration_future_pair.test_three_and_ten_day_comparison_uses_actual_history_and_net_amounts` |
| 근거 가격 표시 형식 | `test_summary_uses_thousands_separator_without_trailing_zero` |

---

## 6. 알려진 한계와 다음 개선 후보

- **표본 부족 경고 (제안 항목)**: 현재는 사용한 사례 수(`clean_pairs`)를 근거 문장에 표시합니다. 별도 경고 기준(예: 경고 없는 쌍 N개 미만)은 팀에서 정하기로 하고, 정해지면 `cautions`에 추가합니다.
- **지원 간격**: 3·7·14일만. 그 외는 직접 입력.
- **자료 범위**: 앱 기본 자료는 2026-10-01~11-03, 서울가락 한 시장. 시즌 자료(`data/seasons/2024-2025`, `2025-2026`)는 경로만 바꾸면 이 함수에서 그대로 동작하고 해시 검사도 통과함을 확인했다(조생귤·특·소·상자 5kg 7일 각 106쌍). 앱 연결은 별도 작업이며, 시즌을 섞지 않도록 가격표·가격쌍·보고서를 한 묶음으로 넘겨야 한다.
- **근거 문장에 기간 없음 (제안 항목)**: `base_date_range`에는 기준일 범위가 들어가지만 `summary` 한 줄에는 없다. 시즌 자료를 쓰면 어느 기간인지 문장에 붙이는 것을 제안한다.

## 7. 변경 이력

- `build_future_pair_input` 추가 (방식 B) 
- 직접 입력 안내 문구 중복 제거 (dev/juneok)
- 근거 문장 가격 천 단위 쉼표 (`2500.0` → `2,500`) 
