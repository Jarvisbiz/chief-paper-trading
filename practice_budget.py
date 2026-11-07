"""Verified specifications and affordability floors for the paper futures adapter."""
import math
from datetime import datetime

ROOTS = {'BTC-USD': 'BIT', 'ETH-USD': 'ET', 'SOL-USD': 'SOL'}
TAKER_RATE = .001
CONTRACT_CHARGE = .15
FEE_SOURCE = 'Coinbase FUTURE Intro account snapshot + published $0.15/contract, checked 2026-11-07 UTC'


def contract_snapshot(product, now, held=False):
    pid = product['product_id']
    pair = next((pair for pair, root in ROOTS.items() if pid.startswith(root + '-')), None)
    if not pair or product.get('product_type') != 'FUTURE': raise ValueError('Unsupported futures product')
    details = product['future_product_details']
    expiry = datetime.fromisoformat(details['contract_expiry'].replace('Z', '+00:00')).timestamp()
    if not held and expiry <= now + 86400: raise ValueError('Contract near expiry; no inferred rollover')
    size, price, minimum, increment = map(float, (details['contract_size'], product['price'], product['base_min_size'], product['base_increment']))
    intraday = details['intraday_margin_rate']; overnight = details['overnight_margin_rate']
    rates = {direction: max(float(intraday[direction + '_margin_rate']), float(overnight[direction + '_margin_rate'])) for direction in ('long', 'short')}
    if any(not math.isfinite(x) or x <= 0 for x in (size, price, minimum, increment, *rates.values())) or max(rates.values()) > 1:
        raise ValueError('Invalid contract specs')
    if minimum != int(minimum) or increment != int(increment): raise ValueError('Unexpected fractional contract size')
    if product.get('trading_disabled') or product.get('is_disabled'): raise ValueError('Contract trading disabled')
    tick_size=float(product.get('price_increment') or product.get('quote_increment') or 0)
    if not math.isfinite(tick_size) or tick_size <= 0: raise ValueError('Verified contract tick size unavailable')
    return {'tick_size':tick_size, 'max_contracts':int(float(product.get('base_max_size') or 5000)), 'market_orders_allowed':not any(product.get(k) for k in ('cancel_only','limit_only','post_only','trading_disabled','is_disabled','view_only')), 'pair': pair, 'product_id': pid, 'contract_size': size, 'price': price, 'minimum_contracts': int(minimum), 'contract_increment': int(increment), 'margin_rates': rates, 'observed_at': int(now), 'expiry': int(expiry), 'session_open': product.get('fcm_trading_session_details', {}).get('is_session_open') is True}


def assess(snapshot, balance, direction='LONG', stop_fraction=.003, risk_rate=.0025, now=None):
    if not snapshot or now is None or not -5 <= now - snapshot.get('observed_at', 0) <= 600:
        return {'available': False, 'reason': 'Fresh verified U.S. futures contract specifications unavailable'}
    if direction not in ('LONG', 'SHORT') or any(not math.isfinite(x) or x <= 0 for x in (balance, stop_fraction, risk_rate)):
        raise ValueError('Invalid budget scenario')
    count = snapshot['minimum_contracts']
    notional = snapshot['contract_size'] * snapshot['price'] * count
    stop_ratio = 1 - stop_fraction if direction == 'LONG' else 1 + stop_fraction
    entry_fee = notional * TAKER_RATE + count * CONTRACT_CHARGE
    exit_fee = notional * stop_ratio * TAKER_RATE + count * CONTRACT_CHARGE
    margin = notional * snapshot['margin_rates'][direction.lower()]
    # Reserve both trading fees; spread, market movement and liquidation charges
    # remain excluded, so this is a floor rather than a sufficient live balance.
    cost_floor = entry_fee + exit_fee
    loss_floor = notional * stop_fraction + cost_floor
    allowed = balance * risk_rate
    reasons = []
    if not snapshot['session_open']: reasons.append('Contract session not verified open')
    if margin + cost_floor > balance: reasons.append('Minimum whole contract exceeds margin plus fee reserve')
    if loss_floor > allowed: reasons.append('Minimum whole contract exceeds the planned risk budget')
    return {'available': True, 'balance': balance, 'product_id': snapshot['product_id'], 'minimum_contracts': count, 'contract_size': snapshot['contract_size'], 'notional': notional, 'margin': margin, 'round_trip_fee_floor': cost_floor, 'planned_loss_floor': loss_floor, 'allowed_loss': allowed, 'stop_fraction': stop_fraction, 'risk_rate': risk_rate, 'minimum_balance_floor': max(margin + cost_floor, loss_floor / risk_rate), 'fits': not reasons, 'reasons': reasons, 'observed_at': snapshot['observed_at']}


def report(snapshots, balance, now):
    rows = []
    for pair in ROOTS:
        snapshot = snapshots.get(pair)
        directions = {direction: assess(snapshot, balance, direction, now=now) for direction in ('LONG', 'SHORT')}
        comparisons = {str(int(b)): assess(snapshot, b, now=now) for b in (80., 100., 150.)}
        rows.append({'pair': pair, 'directions': directions, 'comparisons': comparisons})
    return {'venue': 'COINBASE_US_FUTURES', 'execution_ready': True, 'fee_source': FEE_SOURCE, 'taker_rate': TAKER_RATE, 'contract_charge_per_side': CONTRACT_CHARGE, 'margin_policy': 'Higher of published intraday/overnight rates; no assumed intraday opt-in or offsets', 'example_stop_pct': .3, 'example_risk_pct': .25, 'rows': rows, 'disclosure': 'Affordability floor, not a trade signal or performance forecast. Uses dated nano contracts and latest quoted product price. This table excludes spread/depth; actual paper orders separately walk contract books and enforce risk. Liquidation surcharges remain unmodeled. Daily paper variation transfers are estimates, not official clearing. Actual account order preview controls; no spot-proxy fills.'}
