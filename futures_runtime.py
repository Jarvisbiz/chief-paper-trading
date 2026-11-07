"""Dated U.S. nano-futures PAPER execution. Every exchange request stays read-only.

Account-wide margin guard uses the higher published initial margin as a conservative
maintenance proxy. Daily paper variation transfers are NOT official clearing prices.
"""
import json
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import practice_budget as contract

MODEL = 'US_FUTURES_BOOK_VWAP'
DISCLOSURE = ('Dated Coinbase U.S. nano-futures PAPER simulation: whole contracts, actual contract books/candles, '
 'verified FUTURE taker-fee snapshot plus published contract charge. No real orders. Higher intraday/overnight '
 'initial margin is used throughout; no intraday opt-in or offsets assumed. Account-wide margin protection '
 'uses this initial requirement as a conservative maintenance proxy, not Coinbase liquidation rules. Losses '
 'and cash deficits are uncapped. Daily 5pm New York paper variation transfers use the next fresh mid-book mark, '
 'not official exchange settlement prices or clearing timing. Dated contracts have no perpetual funding; '
 'positions exit 24h before expiry; no automatic rollover fill. Stops are sampled about every 5s, not guaranteed. '
 'Depth changes, outages and liquidation charges can worsen live results; liquidation surcharge is unmodeled. '
 'Fixed-rule forward testing, not a trained predictive model or a profitability claim.')


def fresh_spec(s, now, entry=False):
    if not s or not -5 <= now - s.get('observed_at', 0) <= 600:
        raise ValueError('Fresh U.S. futures contract specifications unavailable')
    if entry and (s['expiry'] <= now + 86400 or not s.get('market_orders_allowed', True)):
        raise ValueError('Contract is near expiry or market orders are restricted')
    return s


def quote_spec(e, c, pair, q, now, entry=False):
    if not q or not -5 <= now-q.get('ts', 0) <= 10:
        raise ValueError('Fresh futures order book unavailable')
    s = fresh_spec(q.get('contract'), now, entry)
    if s.get('pair') != pair or q.get('product_id') != s['product_id']:
        raise ValueError('No spot-proxy substitute: futures book/contract identity mismatch')
    if not s.get('session_open'): raise ValueError('Futures session is closed; no paper fill')
    return s


def fee(contracts, size, price):
    return contracts * size * price * contract.TAKER_RATE + contracts * contract.CONTRACT_CHARGE


def tick(price, increment, up):
    return (math.ceil(price/increment-1e-9) if up else math.floor(price/increment+1e-9))*increment


def plan(e, c, pair, direction, q, evidence, now):
    # Contract book sizes have been converted to underlying units by fetch_quote.
    try: s = quote_spec(e, c, pair, q, now, True)
    except ValueError as exc: return None, str(exc)
    if direction not in ('LONG','SHORT'): return None, 'Confirmed futures direction unavailable'
    sign = e.direction_sign(direction)
    strategy = evidence.get('strategy', 'BREAKOUT')
    risk_rate = e.SCALP_RISK if strategy == 'SNIPER_SCALP' else e.RISK
    eq, cash = e.equity_value(c), float(e.get(c,'cash'))
    if eq <= 0: return None, 'Paper futures equity is exhausted; no new entries'
    rows = [dict(p) for p in c.execute('SELECT * FROM positions')]
    exposure = sum(p['qty']*p['mark'] for p in rows)
    exit_reserve = sum(fee(p['contracts'],p['contract_size'],p['mark']) for p in rows if p['model']==MODEL)
    size, minimum, step, rate = s['contract_size'],s['minimum_contracts'],s['contract_increment'],s['margin_rates'][direction.lower()]
    upper = min(s.get('max_contracts',5000), int(max(0., eq*2-exposure)/(size*q['price'])))
    best, first_reason = None, None
    for count in range(minimum, upper+1, step):
        qty=count*size
        try:
            entry=e.execution_price(q,'BUY' if sign==1 else 'SELL',qty=qty)
            impact=e.impact_rate(q,'SELL' if sign==1 else 'BUY',qty=qty)
        except ValueError as exc:
            first_reason=first_reason or str(exc);break
        if strategy=='SNIPER_SCALP':
            if not all(k in evidence for k in ('structural_stop','opposite_range_level','atr_5m')):
                return None,'Sniper futures structural evidence unavailable'
            stop=tick(evidence['structural_stop'],s['tick_size'],up=sign==-1)
            distance=sign*(entry-stop)/entry
            if not .001<=distance<=.008: return None,'Sniper: futures stop must be 0.1–0.8% from fill'
            move=min(sign*(evidence['opposite_range_level']-entry)*.95,evidence['atr_5m']*2)
            target=tick(entry+sign*move,s['tick_size'],up=sign==-1)
        else:
            stop=tick(entry*(1-sign*e.STOP),s['tick_size'],up=sign==-1)
            target=tick(entry*(1+sign*e.STOP*2),s['tick_size'],up=sign==-1)
        factor=(1-sign*impact)*(1-sign*e.SLIPPAGE)
        stop_fill,target_fill=stop*factor,target*factor
        entry_fee=fee(count,size,entry)
        loss=sign*(entry-stop_fill)*qty+entry_fee+fee(count,size,stop_fill)
        reward=sign*(target_fill-entry)*qty-entry_fee-fee(count,size,target_fill)
        margin=entry*qty*rate
        reserve=fee(count,size,max(entry,stop_fill))
        rr=reward/loss if loss>0 else 0
        reasons=[]
        if not sign*(entry-stop)>0 or not sign*(target-entry)>0 or loss<=0 or reward<=0: reasons.append('Futures protective levels have no positive net reward')
        if loss>eq*risk_rate+1e-9: reasons.append(f'Minimum whole-contract planned loss ${loss:.2f} exceeds ${eq*risk_rate:.2f} risk allowance')
        if margin+entry_fee+reserve>cash-exit_reserve+1e-9: reasons.append('Whole contract exceeds free margin plus both-fee reserve')
        if strategy=='SNIPER_SCALP' and rr<2: reasons.append(f'Sniper futures net reward/risk {rr:.2f}R is below 2R after costs')
        result={'product_id':s['product_id'],'strategy':strategy,'contracts':count,'contract_size':size,'qty':qty,'entry_estimate':entry,'entry_fee':entry_fee,'stop':stop,'target':target,'risk_amount':loss,'risk_rate':risk_rate,'risk_fraction':loss/(entry*qty),'reward_amount':reward,'net_rr':rr,'margin':margin,'margin_rate':rate,'leverage':1/rate,'projected_exit_book_impact_pct':impact*100,'fee_rate':contract.TAKER_RATE,'contract_charge_per_side':contract.CONTRACT_CHARGE,'fee_source':contract.FEE_SOURCE,'book_time':e.iso(q['ts']),'max_hold_seconds':e.SCALP_MAX_HOLD if strategy=='SNIPER_SCALP' else None}
        if reasons:
            first_reason=first_reason or '; '.join(reasons)
            if best is None: best=result
            break
        best=result;best['passes']=True
    if best and best.get('passes'): return best,None
    return best,first_reason or 'Minimum whole futures contract exceeds the 2x-equity gross exposure ceiling'


def open_position(e,c,pair,direction,q,evidence,now):
    reasons=e.risk_blocks(c,now)
    if c.execute('SELECT 1 FROM positions WHERE pair=?',(pair,)).fetchone():reasons.append('Position already open for this market')
    if reasons:return '; '.join(reasons)
    p,error=plan(e,c,pair,direction,q,evidence,now)
    if error:return error
    s=q['contract'];strategy=evidence.get('strategy','BREAKOUT');sign=e.direction_sign(direction)
    evidence={**evidence,'strategy':strategy,'plan':p,'product_id':s['product_id'],'planned_risk_amount':p['risk_amount'],'execution':{'model':MODEL,'book_time':e.iso(q['ts']),'fee_rate':contract.TAKER_RATE,'contract_charge_per_side':contract.CONTRACT_CHARGE,'fee_source':contract.FEE_SOURCE,'contracts':p['contracts'],'contract_size':p['contract_size'],'additional_stress_rate':e.SLIPPAGE}}
    payload=json.dumps(evidence,separators=(',',':'))
    e.put(c,'cash',float(e.get(c,'cash'))-p['margin']-p['entry_fee'])
    expiry=now+e.SCALP_MAX_HOLD if strategy=='SNIPER_SCALP' else None
    c.execute('INSERT INTO positions(pair,qty,entry,entry_fee,stop,peak,opened,direction,leverage,margin,target,initial_stop,funding,funding_at,evidence,mark,mark_ts,strategy,expires,risk_amount,model,product_id,contracts,contract_size,settlement_basis,vm_total,settlement_cycle,margin_rate) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(pair,p['qty'],p['entry_estimate'],p['entry_fee'],p['stop'],p['entry_estimate'],now,direction,p['leverage'],p['margin'],p['target'],p['stop'],0,now,payload,q['price'],q['ts'],strategy,expiry,p['risk_amount'],MODEL,p['product_id'],p['contracts'],p['contract_size'],p['entry_estimate'],0,settlement_cycle(now),p['margin_rate']))
    reason='sniper_sweep_retest' if strategy=='SNIPER_SCALP' else 'news_aware_breakout' if sign==1 else 'news_aware_breakdown'
    c.execute('INSERT INTO trades(ts,pair,side,qty,price,fee,reason,pnl,direction,action,leverage,margin,funding,evidence,strategy,risk_amount,model,product_id,contracts,contract_size) VALUES (?,?,?,?,?,?,?,NULL,?,?,?,?,0,?,?,?,?,?,?,?)',(now,pair,'BUY' if sign==1 else 'SELL',p['qty'],p['entry_estimate'],p['entry_fee'],reason,direction,'OPEN_'+direction,p['leverage'],p['margin'],payload,strategy,p['risk_amount'],MODEL,p['product_id'],p['contracts'],p['contract_size']))
    e.put(c,'entry_bar_'+pair+'_'+strategy,evidence['bar'])
    return None


def close_position(e,c,p,q,reason,now):
    s=quote_spec(e,c,p['pair'],q,now)
    if s['product_id']!=p['product_id']:raise ValueError('Exit must use the held futures contract')
    sign=e.direction_sign(p['direction']);side='SELL' if sign==1 else 'BUY'
    execution=e.execution_price(q,side,qty=p['qty'])
    exit_fee=fee(p['contracts'],p['contract_size'],execution)
    # Settled variation is already in cash; credit only the remaining change.
    credit=p['margin']+sign*(execution-p['settlement_basis'])*p['qty']-exit_fee
    pnl=sign*(execution-p['entry'])*p['qty']-p['entry_fee']-exit_fee
    ev=dict(p['evidence']) if isinstance(p['evidence'],dict) else json.loads(p['evidence']);ev['exit_execution']={'model':MODEL,'book_time':e.iso(q['ts']),'fee_rate':contract.TAKER_RATE,'contract_charge_per_side':contract.CONTRACT_CHARGE,'settled_variation':p['vm_total'],'fee_source':contract.FEE_SOURCE}
    e.put(c,'cash',float(e.get(c,'cash'))+credit)
    c.execute('DELETE FROM positions WHERE pair=?',(p['pair'],))
    c.execute('INSERT INTO trades(ts,pair,side,qty,price,fee,reason,pnl,direction,action,leverage,margin,funding,evidence,strategy,hold_seconds,risk_amount,r_multiple,model,product_id,contracts,contract_size) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(now,p['pair'],side,p['qty'],execution,exit_fee,reason,pnl,p['direction'],'CLOSE_'+p['direction'],p['leverage'],p['margin'],0,json.dumps(ev),p['strategy'],max(0,now-p['opened']),p['risk_amount'],pnl/p['risk_amount'] if p['risk_amount'] else None,MODEL,p['product_id'],p['contracts'],p['contract_size']))
    e.put(c,'exit_at_'+p['pair'],now);e.put(c,'exit_strategy_'+p['pair'],p['strategy'])


def settlement_cycle(now):
    local=datetime.fromtimestamp(now,ZoneInfo('America/New_York'))
    if local.hour<17:local-=timedelta(days=1)
    return local.date().isoformat()


def settle_position(e,c,p,now):
    cycle=settlement_cycle(now)
    if p['settlement_cycle']==cycle:return
    value=e.direction_sign(p['direction'])*(p['mark']-p['settlement_basis'])*p['qty']
    c.execute('INSERT INTO futures_settlements(position_opened,pair,product_id,cycle,ts,price,amount,kind) VALUES (?,?,?,?,?,?,?,?)',(p['opened'],p['pair'],p['product_id'],cycle,now,p['mark'],value,'PAPER_DAILY_VARIATION_ESTIMATE'))
    e.put(c,'cash',float(e.get(c,'cash'))+value)
    c.execute('UPDATE positions SET settlement_basis=?,vm_total=vm_total+?,settlement_cycle=? WHERE pair=?',(p['mark'],value,cycle,p['pair']))


def mark_positions(e,quotes,now):
    with e.db() as c:
        c.execute('BEGIN IMMEDIATE');e.roll_day(c,now)
        valid={}
        for row in c.execute('SELECT * FROM positions WHERE model=?',(MODEL,)).fetchall():
            p=dict(row);q=quotes.get(p['pair'])
            try:
                s=quote_spec(e,c,p['pair'],q,now)
                if s['product_id']!=p['product_id']:raise ValueError('Held futures contract mismatch')
            except ValueError:continue
            valid[p['pair']]=q
            rate=s['margin_rates'][p['direction'].lower()]
            margin=p['qty']*q['price']*rate
            e.put(c,'cash',float(e.get(c,'cash'))-(margin-p['margin']))
            c.execute('UPDATE positions SET mark=?,mark_ts=?,margin=?,margin_rate=?,leverage=? WHERE pair=?',(q['price'],q['ts'],margin,rate,1/rate,p['pair']))
        rows=[dict(p) for p in c.execute('SELECT * FROM positions WHERE model=?',(MODEL,))]
        required=sum(p['margin']+fee(p['contracts'],p['contract_size'],p['mark']) for p in rows)
        guard=e.equity_value(c)<=required
        for p in rows:
            q=valid.get(p['pair'])
            if not q:continue
            sign=e.direction_sign(p['direction']);ex=q['bid'] if sign==1 else q['ask'];s=q['contract']
            reason='margin_protection' if guard else p.get('exit_pending') or ('expiry_exit' if now>=s['expiry']-86400 else 'stop' if sign*(ex-p['stop'])<=0 else 'target' if sign*(ex-p['target'])>=0 else 'time_stop' if p['expires'] and now>=p['expires'] else None)
            if reason:
                try:close_position(e,c,p,q,reason,now)
                except ValueError as exc:
                    c.execute('UPDATE positions SET exit_pending=? WHERE pair=?',(reason,p['pair']))
                    e.log.warning('Futures exit waiting: %s: %s',p['pair'],exc)
                continue
            peak=max(p['peak'],ex) if sign==1 else min(p['peak'],ex)
            stop=p['stop']
            if p['strategy']=='SNIPER_SCALP':
                try:
                    fill=e.execution_price(q,'SELL' if sign==1 else 'BUY',qty=p['qty'])
                    net=sign*(fill-p['entry'])*p['qty']-p['entry_fee']-fee(p['contracts'],p['contract_size'],fill)
                    if net>=p['risk_amount']:
                        fixed=(p['entry_fee']+p['contracts']*contract.CONTRACT_CHARGE)/p['qty']
                        be=(p['entry']+sign*fixed)/(1-sign*contract.TAKER_RATE)
                        impact=e.impact_rate(q,'SELL' if sign==1 else 'BUY',qty=p['qty'])
                        be/=((1-sign*impact)*(1-sign*e.SLIPPAGE))
                        be=tick(be,s['tick_size'],up=sign==1)
                        stop=max(stop,be) if sign==1 else min(stop,be)
                except ValueError:pass
            elif sign*(peak/p['entry']-1)>=e.STOP:
                trail=tick(peak*(1-sign*.02),s['tick_size'],up=sign==1)
                stop=max(stop,trail) if sign==1 else min(stop,trail)
            c.execute('UPDATE positions SET stop=?,peak=? WHERE pair=?',(stop,peak,p['pair']))
            settle_position(e,c,p,now)
        e.roll_day(c,now)
        c.execute('INSERT OR REPLACE INTO equity VALUES (?,?,?)',(now,e.equity_value(c),float(e.get(c,'cash'))))
        if valid:e.put(c,'last_mark',e.iso(now))
