import asyncio
import math
import random
from datetime import datetime, timezone, date
import json
import os

import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq

from ib_insync import *


####################################
# CONFIG
####################################
IB_HOST = '127.0.0.1'
IB_PORT = 4001          # paper: 7497, live: 7496
CLIENT_ID = random.randint(50, 200)

RISK_FREE_RATE = 0.05
DEFAULT_IV = 0.30

BASE_THRESHOLD = 0.10     # threshold per 1 unit notional (10%)
HEDGE_ONLY_ONCE_PER_DAY = True
STATE_FILE = 'last_hedge.json'

####################################
# UTILS
####################################

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def today_str():
    return date.today().isoformat()


def already_hedged_today():
    if not os.path.exists(STATE_FILE):
        return False
    with open(STATE_FILE, 'r') as f:
        data = json.load(f)
    return data.get('date') == today_str()


def mark_hedged_today():
    with open(STATE_FILE, 'w') as f:
        json.dump({'date': today_str()}, f)


def time_to_expiry(expiry_yyyymmdd):
    expiry = datetime.strptime(expiry_yyyymmdd, "%Y%m%d").replace(
        hour=16, tzinfo=timezone.utc
    )
    now = datetime.now(timezone.utc)
    return max((expiry - now).total_seconds() / (365 * 24 * 3600), 0.0)


####################################
# BLACK SCHOLES
####################################

def bs_price(S, K, T, r, sigma, right):
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if right == 'C' else max(0.0, K - S)

    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    if right == 'C':
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def bs_delta(S, K, T, r, sigma, right):
    if T <= 0 or sigma <= 0:
        return 0.0

    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))

    if right == 'C':
        return norm.cdf(d1)
    else:
        return norm.cdf(d1) - 1


####################################
# IMPLIED VOL
####################################

def implied_vol(option_price, S, K, T, r, right):
    if option_price <= 0 or T <= 0:
        return DEFAULT_IV

    try:
        return brentq(
            lambda vol: bs_price(S, K, T, r, vol, right) - option_price,
            1e-4,
            5.0
        )
    except Exception:
        return DEFAULT_IV


####################################
# IB HELPERS
####################################

async def get_snapshot_price(ib, contract):
    ticker = ib.reqMktData(contract, '', snapshot=False)
    
    timeout = 30
    start_time = datetime.now()
    
    while (datetime.now() - start_time).total_seconds() < timeout:
        await asyncio.sleep(0.2)
        
        # 1. Try Live Midpoint first
        mid = ticker.midpoint()
        
        # 2. If Live Mid is NaN, try Delayed Midpoint
        if math.isnan(mid):
            # Use getattr to avoid AttributeError if the field hasn't populated yet
            d_bid = getattr(ticker, 'delayedBid', float('nan'))
            d_ask = getattr(ticker, 'delayedAsk', float('nan'))
            
            if not math.isnan(d_bid) and not math.isnan(d_ask) and d_bid > 0 and d_ask > 0:
                mid = (d_bid + d_ask) / 2
        
        if mid and not math.isnan(mid) and mid > 0:
            return mid
        
        # 3. Fallback sequence
        fallback = (
            ticker.last or 
            getattr(ticker, 'delayedLast', float('nan')) or 
            ticker.close or 
            ticker.marketPrice()
        )
        
        if fallback and not math.isnan(fallback) and fallback > 0:
            return fallback

    # Final attempt: If still NaN, try to pull the last known close
    if not math.isnan(ticker.close) and ticker.close > 0:
        return ticker.close
        
    raise RuntimeError(f"No price for {contract.symbol} within {timeout}s.")


async def get_underlying_price(ib, symbol):
    stk = Stock(symbol, 'SMART', 'USD')
    await ib.qualifyContractsAsync(stk)
    return await get_snapshot_price(ib, stk)


####################################
# DELTA PER UNDERLYING
####################################

async def get_option_delta_per_underlying(ib):
    positions = ib.positions()
    deltas = {}
    notionals = {}

    for p in positions:
        if p.contract.secType != 'OPT':
            continue

        opt = p.contract
        await ib.qualifyContractsAsync(opt)

        # Request market data for this option
        ticker = ib.reqMktData(opt, '', snapshot=False)
        timeout = 10
        start = datetime.now()

        # Wait briefly until lastGreeks.undPrice is available
        S = None
        while (datetime.now() - start).total_seconds() < timeout:
            await asyncio.sleep(0.2)
            lg = ticker.lastGreeks
            if lg and lg.undPrice and lg.undPrice > 0:
                S = lg.undPrice
                break

        if S is None:
            # fallback: use stock price if lastGreeks not available
            S = await get_underlying_price(ib, opt.symbol)

        # Option price from snapshot (delayed/live mid fallback)
        opt_price = await get_snapshot_price(ib, opt)

        T = time_to_expiry(opt.lastTradeDateOrContractMonth)
        sigma = implied_vol(
            opt_price,
            S,
            opt.strike,
            T,
            RISK_FREE_RATE,
            opt.right
        )

        delta = bs_delta(
            S,
            opt.strike,
            T,
            RISK_FREE_RATE,
            sigma,
            opt.right
        )

        multiplier = int(opt.multiplier or 100)
        total_delta = delta * p.position * multiplier

        deltas[opt.symbol] = deltas.get(opt.symbol, 0.0) + total_delta
        notionals[opt.symbol] = abs(p.position * multiplier)

        print(
            f"{opt.symbol} {opt.right}{opt.strike} "
            f"IV={sigma*100:.2f}% delta={total_delta:.1f} "
            f"position={p.position} price={opt_price:.2f} S={S:.2f}"
        )

    return deltas, notionals


def get_stock_position_per_underlying(ib):
    """
    Returns:
    {
        'SPY': -40,
        'QQQ': 100
    }
    """
    positions = ib.positions()
    stock_pos = {}

    for p in positions:
        if p.contract.secType == 'STK':
            stock_pos[p.contract.symbol] = \
                stock_pos.get(p.contract.symbol, 0) + p.position

    return stock_pos


####################################
# HEDGING
####################################

async def hedge(
    ib,
    option_delta,
    option_notional,
    stock_pos
):
    symbols = set(option_delta) | set(option_notional)

    for symbol in symbols:
        delta = option_delta.get(symbol, 0.0)
        notional = option_notional.get(symbol, 0)
        current = stock_pos.get(symbol, 0)

        if notional == 0:
            continue

        target = int(round(-delta))
        diff = target - current

        threshold = BASE_THRESHOLD * notional

        log(
            f"{symbol}: delta={delta} "
            f"target={target} current={current} "
            f"diff={diff} threshold={threshold}"
        )

        if abs(diff) < threshold:
            continue

        qty = abs(diff)
        action = 'BUY' if diff > 0 else 'SELL'

        contract = Stock(symbol, 'SMART', 'USD')
        await ib.qualifyContractsAsync(contract)

        order = MarketOrder(action, qty)
        trade = ib.placeOrder(contract, order)
        await trade.filledEvent

        log(f"{symbol}: HEDGE EXECUTED → {action} {qty}")


####################################
# MAIN
####################################

async def main():
    if HEDGE_ONLY_ONCE_PER_DAY and already_hedged_today():
        print("Already hedged today. Exiting.")
        return

    ib = IB()
    await ib.connectAsync(IB_HOST, IB_PORT, clientId=CLIENT_ID, timeout=5)

    # ✅ IMPORTANT LINE
    ib.reqMarketDataType(3)  # delayed data

    deltas, notionals = await get_option_delta_per_underlying(ib)
    stock_pos = get_stock_position_per_underlying(ib)


    await hedge(ib, deltas, notionals, stock_pos)

    mark_hedged_today()
    ib.disconnect()

async def run_at_noon():
    while True:
        now = datetime.now()
        # Define the target: today at 12:00:00
        target = now.replace(hour=12, minute=0, second=0, microsecond=0)

        # If it's already past 12:00 today, set target to 12:00 tomorrow
        if now >= target:
            from datetime import timedelta
            target += timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        
        log(f"Next run scheduled for: {target.strftime('%Y-%m-%d %H:%M:%S')}")
        log(f"Waiting {wait_seconds / 3600:.2f} hours...")
        
        # Sleep until the target time
        await asyncio.sleep(wait_seconds)
        
        # Execute your main logic
        log("Target time reached. Starting hedge script...")
        try:
            await main()
        except Exception as e:
            log(f"An error occurred during execution: {e}")


if __name__ == "__main__":
    #asyncio.run(run_at_noon())
    asyncio.run(main())
