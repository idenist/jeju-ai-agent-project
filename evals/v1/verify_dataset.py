#!/usr/bin/env python3
"""V1 신규 기능 골드셋의 JSON/계산/데이터 버전을 확인한다. API 호출 없음.

V1 생산 코드의 함수를 가져다 쓰지 않고 원본 가공 CSV의 총금액과 중량으로
정답을 다시 계산한다. V1 Agent 성능을 평가하는 프로그램은 아니다.
"""
from __future__ import annotations
import csv
import json
import hashlib
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING
from pathlib import Path
from collections import Counter, defaultdict

DATA_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DATA_DIR.parents[1]
CSV = PROJECT_ROOT / "data" / "processed" / "daily_prices.csv"
D = Decimal
Q = D("1")

def money(value):return str(value.quantize(Q,rounding=ROUND_HALF_UP))
def digits(value,d=2):return str(value.quantize(D('1').scaleb(-d),rounding=ROUND_HALF_UP))

def list_observations(args):
    grouped=defaultdict(lambda:{'amount':D('0'),'kg':D('0'),'count':0,'flags':set()})
    with CSV.open(encoding='utf-8-sig',newline='') as fp:
        for x in csv.DictReader(fp):
            if (x['market_code'],x['variety'],x['grade'],x['size'],x['package'],x['trade_type']) != (args['market_code'],args['variety'],args['grade'],args['size'],'상자','경매'):continue
            if D(x['package_kg'])!=D(str(args['package_kg'])):continue
            if args.get('as_of_date') and x['date']>args['as_of_date']:continue
            z=grouped[x['date']];z['amount']+=D(x['total_amount_krw']);z['kg']+=D(x['total_kg']);z['count']+=int(x['transaction_count'])
            z['flags'].update([] if x['quality_flag']=='OK' else x['quality_flag'].split(';'))
    return {k:{**v,'price':v['amount']/v['kg']} for k,v in sorted(grouped.items())}

def historical_gold(args, include_pairs=()):
    obs=list_observations(args); d=args['delay_days']; min_count=args['min_transactions_per_day']
    qty=D(str(args['quantity_kg']));cost=D(str(args['daily_storage_cost_krw']));loss=D(str(args['loss_percent']))
    candidates=[];valid=[];excluded=[]
    for a,x in obs.items():
        b=(date.fromisoformat(a)+timedelta(days=d)).isoformat()
        y=obs.get(b)
        if y is None:continue
        diff=qty*(D(1)-loss/D(100))*y['price']-cost*d-qty*x['price']
        row={'start_date':a,'end_date':b,'start_price_krw_per_kg':digits(x['price']), 'end_price_krw_per_kg':digits(y['price']), 'start_transactions':x['count'],'end_transactions':y['count'],'difference_krw':money(diff),'price_change_percent':digits((y['price']/x['price']-1)*100)}
        candidates.append(row)
        if x['count']<min_count or y['count']<min_count:excluded.append(row)
        else: valid.append((diff,row))
    diffs=sorted(d for d,_ in valid)
    n=len(valid)
    if n%2==1: med=diffs[n//2] if n else None
    else: med=(diffs[n//2-1]+diffs[n//2])/2 if n else None
    vals={'candidate_pair_count':len(candidates),'excluded_low_sample_pair_count':len(excluded),'valid_pair_count':n,
          'profit_pair_count':sum(d>0 for d,_ in valid),'loss_pair_count':sum(d<0 for d,_ in valid),'tie_pair_count':sum(d==0 for d,_ in valid),
          'min_difference_krw':money(diffs[0]) if n else None,'median_difference_krw':money(med) if n else None,'max_difference_krw':money(diffs[-1]) if n else None}
    latest=max(obs) if obs else None
    vals.update({'last_observed_date':latest,'days_since_last_observation':(date.fromisoformat(args['analysis_date'])-date.fromisoformat(latest)).days if latest else None})
    qstatus='unavailable' if n==0 else 'limited' if n<5 else 'descriptive_only'
    flags=['limited_history','not_actual_storage','not_forecast']
    if len(candidates)>1:flags.append('overlapping_windows_possible')
    if excluded:flags.append('low_sample_pairs_excluded')
    if latest and vals['days_since_last_observation']>=3:flags.append('stale_price')
    if n<5:flags.append('insufficient_valid_pairs')
    result={'numeric':vals,'status':qstatus,'required_warnings':flags}
    if include_pairs:
        index={r['start_date']:r for r in candidates}
        result['reference_pairs']=[{**index[k], 'included': index[k] not in excluded} for k in include_pairs]
    return result

def price_from_obs(args):
    x=list_observations(args)
    if not x: raise ValueError('가격 관측값 없음')
    return x[max(x)]['price']

def options_gold(args):
    current = D(str(args['current_price_per_kg'])) if args.get('current_price_per_kg') is not None else price_from_obs(args)
    qty=D(str(args['quantity_kg']))
    out=[]
    for op in args['options']:
        days=op['days'];pct=D(str(op['loss_percent']));fee=D(str(op['daily_storage_cost_krw']))
        remain=qty*(1-pct/100);cost=days*fee; breakeven=(qty*current+cost)/remain
        out.append({'days':days,'loss_percent':str(pct),'daily_storage_cost_krw':str(fee),
                    'remaining_kg':digits(remain),'extra_storage_cost_krw':money(cost),
                    'break_even_price_krw_per_kg':digits(breakeven),
                    'minimum_integer_price_krw_per_kg':str(breakeven.quantize(Q,rounding=ROUND_CEILING)),
                    'required_increase_percent':digits((breakeven/current-1)*100)})
    return {'current_price_source':'user' if args.get('current_price_per_kg') is not None else 'csv','current_price_krw_per_kg':digits(current),'options':out}

def sensitivity_gold(args):
    current=D(str(args['current_price_per_kg'])) if args.get('current_price_per_kg') is not None else price_from_obs(args)
    qty=D(str(args['quantity_kg']));days=args['delay_days']
    cells=[]
    for cost_v in args['storage_costs_krw_per_day']:
        for loss_v in args['loss_rates_percent']:
            cost=D(str(cost_v));loss=D(str(loss_v));remain=qty*(1-loss/100)
            price=(qty*current+days*cost)/remain
            cells.append({'daily_storage_cost_krw':str(cost),'loss_percent':str(loss),
                         'break_even_price_krw_per_kg':digits(price),
                         'required_increase_percent':digits((price/current-1)*100),
                         'minimum_integer_price_krw_per_kg':str(price.quantize(Q,rounding=ROUND_CEILING))})
    prices=[D(c['break_even_price_krw_per_kg']) for c in cells]
    return {'current_price_krw_per_kg':digits(current),'grid_cell_count':len(cells),'grid':cells,
            'min_break_even_krw_per_kg':digits(min(prices)),'max_break_even_krw_per_kg':digits(max(prices))}


def verify() -> None:
    ds_path = DATA_DIR / "dataset.jsonl"
    manifest = json.loads((DATA_DIR / "manifest.json").read_text(encoding="utf-8"))
    blob = ds_path.read_bytes()
    ds_sha = hashlib.sha256(blob).hexdigest()
    if ds_sha != manifest["dataset_sha256"]:
        raise AssertionError("데이터셋 SHA-256 불일치: 평가 질문 또는 정답이 변경됐습니다.")
    raw = CSV.read_bytes().replace(b"\r\n", b"\n")
    sha = hashlib.sha1(f"blob {len(raw)}\0".encode("ascii") + raw).hexdigest()
    if sha != manifest["price_data_git_blob_sha"]:
        raise AssertionError("CSV Git blob SHA가 변경됐습니다. 기준 데이터를 확인하세요.")
    data = [json.loads(line) for line in blob.decode("utf-8-sig").splitlines() if line.strip()]
    assert len(data) == manifest["case_count"] == 20, "문항 수 불일치"
    assert len({c["id"] for c in data}) == 20, "중복 문항 ID"
    assert [x["id"] for x in data] == [f"V1-EVAL-{i:03d}" for i in range(1,21)], "문항 ID 순서 오류"
    assert Counter(x["category"] for x in data) == Counter(manifest["category_counts"]), "범주별 문항 수 불일치"
    assert Counter(x["split"] for x in data) == Counter(manifest["split_counts"]), "dev/holdout 분포 불일치"
    assert sum(len(x["turns"]) for x in data) == manifest["turn_count"] == 20
    count = 0
    for c in data:
        turns = c["turns"]
        assert len(turns) == 1, f"{c['id']}: 예상하지 않은 멀티턴"
        t = turns[0]
        assert isinstance(t["question"], str) and t["question"].strip(), f"{c['id']}: 질문 누락"
        e = t["expected"]
        assert e["action"] in {'historical','no_data','options','sensitivity','quality','ask'}, c['id']
        assert isinstance(e.get('grading',{}).get('final_answer_required'),list) and e['grading']['final_answer_required']
        if e['action']=='ask':
            assert isinstance(e.get('missing'),list) and e['missing'], c['id']
            assert 'gold' not in e, c['id']
            continue
        args=e['args']
        assert args['market_code']=='110001' and args['package_kg']>0, c['id']
        assert isinstance(e.get('primary_tool'),str), c['id']
        if e['action'] in {'historical','no_data','quality'}:
            refs=[p['start_date'] for p in e['gold'].get('reference_pairs',[])]
            check=historical_gold(args,refs)
        elif e['action']=='options':check=options_gold(args)
        elif e['action']=='sensitivity':check=sensitivity_gold(args)
        else:raise AssertionError(f"{c['id']}: 알 수 없는 동작")
        if check != e['gold']:
            for key in set(check)|set(e['gold']):
                if check.get(key)!=e['gold'].get(key):
                    raise AssertionError(f"{c['id']}: 골드값 불일치({key})\n기대: {e['gold'].get(key)}\n재계산: {check.get(key)}")
        count += 1
    # 대표적인 실제 가격/손익은 별도의 수동 기준값으로 한 번 더 검사한다.
    e1=data[0]['turns'][0]['expected']['gold']['numeric']
    assert (e1['candidate_pair_count'],e1['valid_pair_count'],e1['profit_pair_count'],e1['loss_pair_count'])==(21,20,7,13)
    x=data[3]['turns'][0]['expected']['gold']['reference_pairs'][0]
    assert (x['start_date'],x['end_date'],x['difference_krw'],x['price_change_percent'])==('2026-10-07','2026-10-14','-173791','1.54')
    g=data[8]['turns'][0]['expected']['gold']['options']
    assert [v['break_even_price_krw_per_kg'] for v in g] == ['2491.73','2544.36','2645.98']
    assert data[7]['turns'][0]['expected']['gold']['numeric']['candidate_pair_count']==0
    print(f"[정상] 총 {len(data)}문항 / JSON·SHA·CSV 버전·골드 정답 {count}건 검증 완료 (API 호출 0회)")
    print("[참고] 아직 V1 Agent 성능을 측정한 결과는 아닙니다.")


if __name__ == '__main__':
    verify()
