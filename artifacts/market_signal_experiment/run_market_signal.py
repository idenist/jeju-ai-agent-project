#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""시장 전체 정보가 제주산 감귤 동일규격 가격 예측에 추가 신호를 주는지 탐색하는 독립 실험.

- 원본 ZIP에서 읽기만 수행; 원본/공유 저장소 수정 금지.
- 2024-25 train, 2025-26 exploratory validation. 2026-27은 학습/선정에서 제외.
- A=current price, B=existing price/history, C=B+market features. Fixed HGBR.
- at base-date END-OF-SETTLEMENT, no feature date > base date.
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import math
from pathlib import Path
import sys
import zipfile

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

TRAIN_SEASON = '2024-2025'
VALID_SEASON = '2025-2026'
HORIZONS = (3, 7, 14)
SPEC_COLUMNS = ['market_code', 'variety_code', 'grade_code', 'size_code', 'package', 'package_kg', 'trade_type']
BASE_COLUMNS = ['feature_price_kg', 'feature_total_kg', 'feature_transaction_count', 'feature_weekday',
    'feature_month', 'feature_season_day_index', 'feature_lag3_price_kg', 'feature_lag7_price_kg',
    'feature_trailing7_mean_price_kg', 'feature_trailing7_total_kg', 'feature_trailing7_ok_days']
MARKET_COLUMNS = ['market_7d_kg', 'market_prev7d_kg', 'market_kg_log_change',
    'market_price_log_change', 'market_7d_transactions', 'market_7d_observed_days',
    'market_prev7d_observed_days', 'market_variety_share_7d', 'market_variety_share_change',
    'market_variety_price_log_change', 'market_variety_kg_log_change']
ALL_COLUMNS = SPEC_COLUMNS + BASE_COLUMNS + MARKET_COLUMNS
REQUIRED = {'trd_clcln_ymd','whsl_mrkt_cd','gds_lclsf_cd','gds_mclsf_cd','gds_sclsf_cd',
    'plor_nm','trd_se','grd_cd','grd_nm','sz_cd','sz_nm','pkg_nm', 'unit_nm',
    'unit_qty','unit_tot_qty','qty','scsbd_prc','totprc'}
INVALID = {'', '.', '-', 'null', 'None', '미상'}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fp:
        for block in iter(lambda: fp.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def parse_decimal(value):
    try:
        n = Decimal(str(value).strip().replace(',', ''))
    except (InvalidOperation, ValueError):
        raise ValueError('bad_number') from None
    if not n.is_finite():
        raise ValueError('bad_number')
    return n


def split_season(name: str):
    parts = name.replace('\\', '/').split('/')
    return next((p for p in parts if p in (TRAIN_SEASON, VALID_SEASON, '2026-2027')), None)


@dataclass
class Summary:
    kg: float = 0.0
    amount: float = 0.0
    count: int = 0
    grade_known: bool = True
    size_known: bool = True

    @property
    def price(self):
        return self.amount / self.kg if self.kg > 0 else math.nan

    @property
    def quality_ok(self):
        return self.count >= 5 and self.grade_known and self.size_known


@dataclass
class ParsedData:
    spec_days: dict
    market_days: dict
    variety_days: dict
    audit: dict


def read_original_zip(zip_path: Path) -> ParsedData:
    """Repo preprocess_auction.py와 동일한 기본 거래 필터/숫자검사/중복 규칙.

    단, 코드의 라벨 표시 차이로 동일 조건이 분할되지 않도록 그룹키는 표준 '코드'로 통일.
    관찰되는 원본의 2개 시즌만 학습/검증, 일부 3번째 시즌은 읽지 않음.
    """
    spec_days = defaultdict(Summary)
    market_days = defaultdict(Summary)
    variety_days = defaultdict(Summary)
    reason = defaultdict(Counter)
    seen = set()
    with zipfile.ZipFile(zip_path) as zf:
        names = sorted(n for n in zf.namelist() if n.lower().endswith('.csv'))
        selected = [(n, split_season(n)) for n in names if split_season(n) in (TRAIN_SEASON, VALID_SEASON)]
        if not any(s == TRAIN_SEASON for _, s in selected) or not any(s == VALID_SEASON for _, s in selected):
            raise ValueError('두 시즌 (2024-2025, 2025-2026)의 CSV가 ZIP에 모두 필요합니다.')
        for i, (name, season) in enumerate(selected, 1):
            with zf.open(name) as raw:
                from io import TextIOWrapper
                reader = csv.DictReader(TextIOWrapper(raw, encoding='utf-8-sig', newline=''))
                headers = reader.fieldnames or []
                missing = REQUIRED - set(headers)
                if missing:
                    raise ValueError(f'{name} 필수 컬럼 누락: {sorted(missing)}')
                for r in reader:
                    counts = reason[season]
                    counts['raw_rows'] += 1
                    # repo의 원본 전체열 fingerprint와 같은 규칙 (정렬/열제거 없음)
                    fingerprint = hashlib.blake2b('\x1f'.join(str(r.get(k, '')).strip() for k in headers).encode('utf-8'),digest_size=16).digest()
                    if fingerprint in seen:
                        counts['exact_duplicate'] += 1
                        continue
                    seen.add(fingerprint)
                    if r['gds_lclsf_cd'].strip() != '06' or r['gds_mclsf_cd'].strip() != '14':
                        counts['not_citrus'] += 1; continue
                    if not r['plor_nm'].strip().startswith('제주특별자치도'):
                        counts['not_jeju'] += 1; continue
                    if r['trd_se'].strip() != '경매':
                        counts['not_auction'] += 1; continue
                    try:
                        day = date.fromisoformat(r['trd_clcln_ymd'].strip())
                        if day.isoformat() != r['trd_clcln_ymd'].strip():
                            raise ValueError('bad date')
                    except ValueError:
                        counts['bad_date'] += 1; continue
                    try:
                        qty, unit_qty, total_unit_qty, price, amount = (parse_decimal(r[k]) for k in
                            ('qty','unit_qty','unit_tot_qty','scsbd_prc','totprc'))
                    except ValueError:
                        counts['bad_number'] += 1; continue
                    if min(qty, unit_qty, total_unit_qty, price, amount) <= 0:
                        counts['nonpositive'] += 1; continue
                    unit = r['unit_nm'].strip().lower()
                    mult = {'kg': Decimal('1'), 'g': Decimal('0.001'), '그램': Decimal('0.001'),
                            't': Decimal('1000'), 'ton': Decimal('1000'), '톤': Decimal('1000')}.get(unit)
                    if mult is None:
                        counts['unsupported_weight_unit'] += 1; continue
                    if abs(qty * unit_qty - total_unit_qty) > Decimal('0.001'):
                        counts['weight_mismatch'] += 1; continue
                    if abs(qty * price - amount) > Decimal('1'):
                        counts['amount_mismatch'] += 1; continue
                    kg, pkg_kg = total_unit_qty * mult, unit_qty * mult
                    if kg <= 0 or pkg_kg <= 0:
                        counts['invalid_weight_kg'] += 1; continue
                    market = r['whsl_mrkt_cd'].strip()
                    variety = r['gds_sclsf_cd'].strip()
                    grade = r['grd_cd'].strip()
                    size = r['sz_cd'].strip()
                    pkg = r['pkg_nm'].strip() or '미상'
                    pkg_fixed = pkg_kg.quantize(Decimal('.001'), rounding=ROUND_HALF_UP)
                    spec = (market, variety, grade, size, pkg, format(pkg_fixed,'f'), '경매')
                    known_grade = r['grd_nm'].strip() not in INVALID
                    known_size = r['sz_nm'].strip() not in INVALID
                    # sum after validation, never unweighted average of average prices
                    for target in (spec_days[(season, day, spec)],
                                   market_days[(season, day, market)],
                                   variety_days[(season, day, market, variety)]):
                        target.kg += float(kg)
                        target.amount += float(amount)
                        target.count += 1
                    detail = spec_days[(season,day,spec)]
                    detail.grade_known = detail.grade_known and known_grade
                    detail.size_known = detail.size_known and known_size
                    counts['accepted_rows'] += 1
            print(f'[수집 {i}/{len(selected)}] {name}', flush=True)
    audit = {
        'source_zip_sha256': sha256(zip_path),
        'source_zip_files': len(names),
        'selected_files': {s:sum(k==s for _,k in selected) for s in (TRAIN_SEASON,VALID_SEASON)},
        'ignored_season': '2026-2027 (partial, excluded from training, feature selection, validation)',
        'rows_by_season': {s:dict(reason[s]) for s in (TRAIN_SEASON,VALID_SEASON)},
        'detail_group_count': {s:sum(k[0]==s for k in spec_days) for s in (TRAIN_SEASON,VALID_SEASON)},
        'detail_quality_ok_count': {s:sum(k[0]==s and v.quality_ok for k,v in spec_days.items()) for s in (TRAIN_SEASON,VALID_SEASON)},
        'market_dates_by_season': {s:len({k[1] for k in market_days if k[0]==s}) for s in (TRAIN_SEASON,VALID_SEASON)}
    }
    return ParsedData(dict(spec_days),dict(market_days),dict(variety_days),audit)


def history_window(days: dict, season: str, base_day: date, identifiers: tuple, offset_start: int, offset_end: int):
    """Both offsets <=0, latest source day cannot exceed base_day."""
    if offset_start > offset_end or offset_end > 0:
        raise ValueError('미래 자료는 피처로 사용할 수 없습니다.')
    data=[]
    for i in range(offset_start, offset_end+1):
        item=days.get((season, base_day + timedelta(days=i), *identifiers))
        if item is not None:
            data.append(item)
    return data


def sum_window(items):
    kg=sum(x.kg for x in items)
    amount=sum(x.amount for x in items)
    return {'kg':kg, 'price':amount/kg if kg>0 else math.nan,
            'count':sum(x.count for x in items),'days':len(items)}


def log_change(a, b):
    if a is None or b is None or not (math.isfinite(a) and math.isfinite(b)) or a<=0 or b<=0:
        return math.nan
    return math.log(a/b)


def get_features(parsed:ParsedData, season:str, day:date, spec:tuple):
    key=(season,day,spec)
    base=parsed.spec_days[key]
    market,variety,grade,size,pkg,pkg_kg,trade=spec
    specs={**dict(zip(SPEC_COLUMNS,spec))}
    calendar_start = date(day.year if day.month>=10 else day.year-1,10,1)
    feats = {
        **specs,
        'feature_price_kg':base.price,
        'feature_total_kg':base.kg,
        'feature_transaction_count':base.count,
        'feature_weekday':day.weekday(),
        'feature_month':day.month,
        'feature_season_day_index':(day-calendar_start).days,
    }
    for delta in (3,7):
        old=parsed.spec_days.get((season,day-timedelta(days=delta),spec))
        feats[f'feature_lag{delta}_price_kg'] = old.price if old is not None and old.quality_ok else math.nan
    hist=[parsed.spec_days[(season,day-timedelta(days=d),spec)]
          for d in range(7) if (season,day-timedelta(days=d),spec) in parsed.spec_days
          and parsed.spec_days[(season,day-timedelta(days=d),spec)].quality_ok]
    feats.update(feature_trailing7_mean_price_kg=float(np.mean([x.price for x in hist])) if hist else math.nan,
        feature_trailing7_total_kg=sum(x.kg for x in hist), feature_trailing7_ok_days=len(hist))
    # Same market/citrus, t-6..t and t-13..t-7; never use t+1 or later.
    market_cur=sum_window(history_window(parsed.market_days,season,day,(market,),-6,0))
    market_prev=sum_window(history_window(parsed.market_days,season,day,(market,),-13,-7))
    var_cur=sum_window(history_window(parsed.variety_days,season,day,(market,variety),-6,0))
    var_prev=sum_window(history_window(parsed.variety_days,season,day,(market,variety),-13,-7))
    # Season opening windows have partial history. A 7-day window with fewer than 4 actual
    # market days is not considered comparable (missing, not zero-volume trading days).
    market_valid = market_cur['days']>=4 and market_prev['days']>=4
    var_valid = var_cur['days']>=4 and var_prev['days']>=4
    cur_share = var_cur['kg']/market_cur['kg'] if market_cur['kg']>0 else math.nan
    prev_share = var_prev['kg']/market_prev['kg'] if market_prev['kg']>0 else math.nan
    market_stats = {
        'market_7d_kg':market_cur['kg'] if market_cur['days']>=4 else math.nan,
        'market_prev7d_kg':market_prev['kg'] if market_prev['days']>=4 else math.nan,
        'market_kg_log_change':log_change(market_cur['kg'],market_prev['kg']) if market_valid else math.nan,
        'market_price_log_change':log_change(market_cur['price'],market_prev['price']) if market_valid else math.nan,
        'market_7d_transactions':market_cur['count'] if market_cur['days']>=4 else math.nan,
        'market_7d_observed_days':market_cur['days'],
        'market_prev7d_observed_days':market_prev['days'],
        'market_variety_share_7d':cur_share if market_cur['days']>=4 else math.nan,
        'market_variety_share_change':cur_share-prev_share if market_valid else math.nan,
        'market_variety_price_log_change':log_change(var_cur['price'],var_prev['price']) if var_valid else math.nan,
        'market_variety_kg_log_change':log_change(var_cur['kg'],var_prev['kg']) if var_valid else math.nan,
    }
    feats.update(market_stats)
    return feats


def make_samples(parsed:ParsedData):
    features_cache={}
    result=[]
    for (season, base_day, spec), base in sorted(parsed.spec_days.items(),key=lambda z:(z[0][0],z[0][1],z[0][2])):
        if not base.quality_ok:
            continue
        for h in HORIZONS:
            future_day=base_day+timedelta(days=h)
            target=parsed.spec_days.get((season,future_day,spec))
            if target is None or not target.quality_ok:
                continue
            if (season,base_day,spec) not in features_cache:
                features_cache[(season,base_day,spec)]=get_features(parsed,season,base_day,spec)
            result.append({
                'season':season, 'base_date':base_day.isoformat(),
                'target_date':future_day.isoformat(), 'horizon':h,
                'current_price_kg':base.price, 'target_price_kg':target.price,
                **features_cache[(season,base_day,spec)]
            })
    df=pd.DataFrame(result)
    if df.empty:
        raise RuntimeError('유효한 미래 가격 쌍을 만들지 못했습니다.')
    # source window functions enforce <= t; target is deliberately not part of feature set.
    assert all(pd.to_datetime(df['target_date'])>pd.to_datetime(df['base_date']))
    assert not any(x in ALL_COLUMNS for x in ('target_price_kg','target_date','horizon'))
    return df


def fitted_model(features: list[str]):
    numbers=[c for c in features if c not in SPEC_COLUMNS]
    pre=ColumnTransformer([
        ('numeric', SimpleImputer(strategy='median', add_indicator=True, keep_empty_features=True), numbers),
        ('categorical', OneHotEncoder(handle_unknown='ignore', sparse_output=False), SPEC_COLUMNS),
    ],remainder='drop')
    model=HistGradientBoostingRegressor(loss='squared_error',max_iter=90,learning_rate=0.05,
        max_depth=3,max_leaf_nodes=8,min_samples_leaf=30,l2_regularization=10.0,
        early_stopping=False,random_state=42)
    return Pipeline([('pre',pre),('regressor',model)])


def fit_predict(train: pd.DataFrame, valid: pd.DataFrame, features:list[str]):
    # Train on log ratio, clip predictions to prevent numerical overflow; no validation tuning.
    y=np.log(train['target_price_kg'].to_numpy()/train['current_price_kg'].to_numpy())
    model=fitted_model(features)
    model.fit(train[features],y)
    residual=np.clip(model.predict(valid[features]),-1.5,1.5)
    return valid['current_price_kg'].to_numpy()*np.exp(residual)


def round_float(x, digits=3):
    if not np.isfinite(x):return None
    return round(float(x),digits)


def grouped_weekly_ci(df: pd.DataFrame, left:str, right:str, seed:int=42, iters:int=1000):
    # Paired difference (left - right), with week-level rather than row-level sampling.
    # Weeks are only an exploratory uncertainty approximation: adjacent weeks still correlate.
    df=df.copy()
    df['_week']=pd.to_datetime(df['base_date']).dt.strftime('%G-%V')
    weekly=[(len(chunk),float((chunk[left]-chunk[right]).sum())) for _,chunk in df.groupby('_week')]
    if len(weekly)<4:return {'weeks':len(weekly),'ci_low':None,'ci_high':None}
    ns=np.array([a for a,_ in weekly]); ss=np.array([b for _,b in weekly]); rng=np.random.default_rng(seed)
    draws=rng.integers(0,len(ns),size=(iters,len(ns)))
    means=ss[draws].sum(axis=1)/ns[draws].sum(axis=1)
    return {'weeks':len(weekly),'ci_low':round_float(np.quantile(means,.025)),
            'ci_high':round_float(np.quantile(means,.975))}


def profit(price, horizon_days, initial_kg=3000.0, cost_per_day=10000.0):
    # Hypothetical conditions ONLY, not realized farm profit.
    shrink,commission={3:(0.01,0.05),14:(0.04,0.07)}[horizon_days]
    return initial_kg*(1-shrink)*price*(1-commission)-cost_per_day*horizon_days


def eval_choice(predictions:pd.DataFrame):
    a=predictions[predictions.horizon==3].copy()
    b=predictions[predictions.horizon==14].copy()
    join_keys=['season','base_date']+SPEC_COLUMNS
    a=a.merge(b,on=join_keys,suffixes=('_3','_14'),validate='one_to_one')
    if a.empty:return {},pd.DataFrame()
    actual3=profit(a.target_price_kg_3.to_numpy(),3)
    actual14=profit(a.target_price_kg_14.to_numpy(),14)
    best=np.maximum(actual3,actual14)
    decisions=pd.DataFrame({'base_date':a.base_date,'season':a.season,'real_better_B':actual14>actual3})
    summary={}
    for method in ['A','B','C']:
        p3=profit(a[f'pred_{method}_3'].to_numpy(),3)
        p14=profit(a[f'pred_{method}_14'].to_numpy(),14)
        chooseB=p14>p3
        if method=='A':chooseB=np.zeros(len(a),dtype=bool) # early departure baseline
        achieved=np.where(chooseB,actual14,actual3)
        regret=best-achieved
        decisions[f'choose_B_{method}']=chooseB
        decisions[f'regret_{method}']=regret
        summary[method]={
            'pairs':len(a),'different_base_dates':a.base_date.nunique(),
            'selection_accuracy_pct':round_float(100*np.mean(chooseB==(actual14>actual3)),2),
            'mean_regret_krw':round_float(regret.mean(),0),
            'recommended_B':int(np.sum(chooseB)),
            'incorrect_B':int(np.sum(chooseB & ~(actual14>actual3))),
        }
    summary['market_regret_gain_B_minus_C']=round_float(summary['B']['mean_regret_krw']-summary['C']['mean_regret_krw'],0)
    summary['weekly_bootstrap_regret_B_minus_C']=grouped_weekly_ci(decisions,'regret_B','regret_C')
    return summary,decisions


def run_experiment(zip_path: Path, out_dir: Path):
    out_dir.mkdir(parents=True,exist_ok=True)
    parsed=read_original_zip(zip_path)
    print('[1/4] 기준 산식으로 동일 규격 가격 쌍 작성',flush=True)
    df=make_samples(parsed)
    train=df[df.season==TRAIN_SEASON].copy()
    valid=df[df.season==VALID_SEASON].copy()
    assert len(train)>0 and len(valid)>0
    assert set(train.base_date).isdisjoint(set(valid.base_date))
    preds=[]
    print(f'[2/4] 동일한 사례 고정: 학습 {len(train):,}, 탐색검증 {len(valid):,}',flush=True)
    for h in HORIZONS:
        tr=train[train.horizon==h].copy()
        va=valid[valid.horizon==h].copy()
        if min(len(tr),len(va)) < 30:
            raise RuntimeError(f'{h}일 간격 학습/검증 30건 미만; 진행하지 않습니다.')
        va['pred_A']=va.current_price_kg
        va['pred_B']=fit_predict(tr,va,SPEC_COLUMNS+BASE_COLUMNS)
        va['pred_C']=fit_predict(tr,va,ALL_COLUMNS)
        preds.append(va)
        print(f'  [학습 {h}일] {len(tr):,}건 → 검증 {len(va):,}건 / B·C 동일 모델 완료',flush=True)
    result=pd.concat(preds,ignore_index=True)
    scores={}
    result['abs_err_A']=abs(result.target_price_kg-result.pred_A)
    result['abs_err_B']=abs(result.target_price_kg-result.pred_B)
    result['abs_err_C']=abs(result.target_price_kg-result.pred_C)
    for h,part in result.groupby('horizon',sort=True):
        scores[str(h)]={
            'n':len(part),'days':part.base_date.nunique(),
            'mae_krw_kg':{p:round_float(part[f'abs_err_{p}'].mean()) for p in ['A','B','C']},
            'market_gain_B_minus_C_mae':round_float(part.abs_err_B.mean()-part.abs_err_C.mean()),
            'market_gain_B_minus_C_weekly_bootstrap_ci':grouped_weekly_ci(part,'abs_err_B','abs_err_C'),
            'by_month': {m:{'n':len(q),**{f'mae_{p}':round_float(q[f'abs_err_{p}'].mean()) for p in ['A','B','C']}}
                for m,q in part.groupby(part.base_date.str[5:7])}
        }
    decision, decision_rows=eval_choice(result)
    print('[3/4] MAE, 월별, 주 단위 부트스트랩, 3일 대 14일 가상 기회손실 계산',flush=True)
    result_columns=['season','base_date','target_date','horizon']+SPEC_COLUMNS+[
        'current_price_kg','target_price_kg','pred_A','pred_B','pred_C','abs_err_A','abs_err_B','abs_err_C']
    result[result_columns].sort_values(['horizon','base_date','market_code','variety_code']).to_csv(
        out_dir/'paired_predictions.csv',index=False,encoding='utf-8-sig',float_format='%.6f')
    if not decision_rows.empty: decision_rows.to_csv(out_dir/'choice_3v14.csv',index=False,encoding='utf-8-sig')
    report={
        'status':'EXPLORATORY_ONLY_NO_DEPLOY',
        'claim':'Same-feature-set except added market info; tests incremental predictive signal, not causal attribution.',
        'train_season':TRAIN_SEASON,'exploratory_validation_season':VALID_SEASON,
        'IMPORTANT':'2025-26 season already repeatedly inspected in V1-H6; NOT independent holdout; 2026-27 partial is excluded.',
        'forecast_asof':'end of market settlement day t, market aggregate and base price at t assumed available after finalization',
        'market_scope':'same wholesale market; 제주산 감귤 경매만 (NOT national shipment volume / supply)',
        'target':'same-spec market daily total_amount / kg on exact future calendar date when both days quality_OK',
        'sample_selection':'>=5 transactions and known grade+size on BOTH base and target dates. Missing future prices not imputed.',
        'control':'A=current price (3v14 always earliest); B=fixed HistGradientBoosting with current/spec/history; C=same HGBR with +market features',
        'fixed_model':{'name':'HistGradientBoostingRegressor','max_iter':90,'learning_rate':.05,'max_depth':3,
            'max_leaf_nodes':8,'min_samples_leaf':30,'l2_regularization':10,'random_state':42,'target':'log(future/base)'},
        'categorical_columns':SPEC_COLUMNS,'baseline_numeric_columns':BASE_COLUMNS,'added_market_numeric_columns':MARKET_COLUMNS,
        'market_missing_days':'never impute missing raw market observation as zero; 7d windows require >=4 observed days for trend; other missing values median-imputed in train only',
        'source_data_audit':parsed.audit,
        'sample_counts':{'train':len(train),'validation':len(valid),
            'train_unique_base_days':train.base_date.nunique(),'validation_unique_base_days':valid.base_date.nunique()},
        'scores':scores,
        'choice_3v14':decision,
        'hypothetical_choice_contract':{
            'quantity_kg':3000,'shrink_3d':0.01,'shrink_14d':0.04,
            'fee_3d':0.05,'fee_14d':0.07,'storage_per_day_krw':10000,
            'meaning':'arbitrary example conditions; not real farm profit or guarantee'},
        'limitations':[
            'Settlement date != physical shipping date; daily data may not have been finalized at farmer decision time.',
            'One independent train season, one repeatedly used exploratory validation season. Selection bias possible.',
            'Quality OK means >=5 transactions with grade and size known; does NOT guarantee price homogeneity.',
            'Auction corporation and shipment type are mixed by prior target contract; they may shift over time.',
            'Market-level price mixes varieties and specifications; its trend can reflect composition changes.',
            'Exact-date and both-days-OK label coverage may be selection-biased; not all potential farm cases.',
            'Weekly bootstraps are exploratory; adjacent weeks and seasons still not statistically independent.',
            'jeju_shipments(1).csv is deliberately not used; it ends Sep 2025, with missing dates.',
        ],
    }
    (out_dir/'metrics.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    lines=['# 시장 전체 정보 추가 효과: 탐색 실험 결과','',
        '> **검증 시즌은 V1~H6에서 이미 확인했던 2025~26 시즌입니다. 독립 검증으로 해석하면 안 됩니다.**','',
        f"- 원본 ZIP SHA-256: `{parsed.audit['source_zip_sha256']}`",
        f"- 학습 {len(train):,}행 ({train.base_date.nunique()}일), 검증 {len(valid):,}행 ({valid.base_date.nunique()}일)",
        '- 기준 시점: 경매 정산일 t의 마감 후. 새로 만드는 시장 피처는 t를 포함한 과거 자료만 사용.','',
        '| 미래 가격 | 검증행 | A:현재가유지 MAE | B:기존정보 MAE | C:+시장정보 MAE | B−C |',
        '|---|---:|---:|---:|---:|---:|']
    for h in HORIZONS:
        sc=scores[str(h)];values=sc['mae_krw_kg'];
        lines.append(f"| {h}일 | {sc['n']:,} | {values['A']:,.1f} | {values['B']:,.1f} | {values['C']:,.1f} | {sc['market_gain_B_minus_C_mae']:+,.1f} |")
    lines+=['','## 3일 vs 14일 출하 선택 (가상 비용 계약)',
        '| 방법 | 판단 적중률 | 평균 기회손실(원) | 14일 추천수 | 잘못된 14일 추천 |',
        '|---|---:|---:|---:|---:|']
    for m,desc in [('A','항상 3일'),('B','기존 정보'),('C','시장 정보 추가')]:
        if m in decision:
            q=decision[m]
            lines.append(f"| {desc} | {q['selection_accuracy_pct']:.2f}% | {q['mean_regret_krw']:,.0f} | {q['recommended_B']} | {q['incorrect_B']} |")
    lines+=['','## 해석 규칙','',
        '- B−C가 양수이면 C가 더 적은 가격오차를 냈다는 뜻입니다. 값이 음수면 시장정보를 추가했지만 악화된 것입니다.',
        '- 월별 성능과 날짜(주) 블록 부트스트랩을 함께 확인하세요. 한 시즌 반복검증이므로 유의한 개선을 **확정**하지 마세요.',
        '- 가격 MAE와 출하 선택의 기회손실은 서로 다른 목표입니다. 둘 중 하나만 개선되면 운영 반영 근거가 되지 않습니다.',
        '- 3일/14일 선택의 비용·물량은 임의 예시이며 실현 농가 손익이 아닙니다.',
        '- 개선 없음 ≠ 해당 정보가 무의미함을 입증. 입력 표현/학습량/분포 이동의 영향을 구분할 추가 검증이 필요합니다.',
        '- 현재가 유지 모델은 출하 선택에서는 **항상 3일 출하**라는 별도의 단순 판단 기준선입니다.',
        '- B는 기존 GRU 자체가 아니라 이전과 비슷한 입력 특성을 사용하는 **고정된 HistGradientBoosting 모델**입니다.',
        '- 이 실험의 3vs14 평가쌍은 GRU의 21일 이력 최소조건을 적용하지 않으므로 기존 H5/H6와 평가 표본 수가 다릅니다.',
        '', '## 한계','',
        '- 경매법인·출하유형을 섞는 기존 규격가격 정의를 보존했습니다. 이 축을 바꾸면 별도 실험이 필요합니다.',
        '- `jeju_shipments(1).csv`는 최신 출하량이 없으므로 학습·검증 입력에 사용하지 않았습니다.',
        '- 경매 정산정보가 실제 의사결정 시점에 공개되었다는 보장은 없습니다.',
        '', '**세부 수치와 월별 결과:** `metrics.json` / 사례별 예측 `paired_predictions.csv` / 판단 `choice_3v14.csv`']
    (out_dir/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print('[4/4] 결과 저장: '+str(out_dir.resolve()),flush=True)
    for h in HORIZONS:
        sc=scores[str(h)]; print(f"  {h}일 A/B/C MAE: "+' / '.join(f"{sc['mae_krw_kg'][k]:.1f}" for k in 'ABC')+' 원/kg')
    if decision:
        print('  3vs14 기회손실(원) A/B/C: '+ ' / '.join(f"{decision[k]['mean_regret_krw']:.0f}" for k in 'ABC'))
    return report


def main():
    root=Path(__file__).resolve().parent
    p=argparse.ArgumentParser(description='동일기준 시장 정보 추가 효과 비교 (읽기 전용)')
    p.add_argument('--input',type=Path,default=root/'data'/'경매_원본_데이터.zip')
    p.add_argument('--out-dir',type=Path,default=root/'results')
    args=p.parse_args()
    if not args.input.is_file():p.error(f'원본 ZIP을 찾지 못했습니다: {args.input}')
    run_experiment(args.input,args.out_dir)

if __name__=='__main__':
    main()
