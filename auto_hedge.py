import asyncio
import math
import random
from datetime import datetime, timezone, date
import json
import os

from ib_insync import *

####################################
# CONFIG
####################################
IB_HOST = '127.0.0.1'
IB_PORT = 4001          # paper: 7497, live: 7496
CLIENT_ID = random.randint(50, 200)

BASE_THRESHOLD = 0.10     # hedge threshold per 1 unit notional
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

####################################
# IB PRICE HELPERS (MODEL-BASED)
####################################

async def get_snapshot_price(ib, contract):
    """
    Unified snapshot:
    - Options: use modelGreeks.optPrice
    - Stocks: use marketPrice / close
    """
    ticker = ib.reqMktData(contract, '', snapshot=False)
    start = datetime.now()

    while (datetime.now() - start).total_seconds() < 10:
        await asyncio.sleep(0.5)

        print(ticker)
        # OPTION: model price
        if contract.secType == 'OPT':
            mg = ticker.modelGreeks
            if mg and mg.optPrice and mg.optPrice > 0:
                return mg.optPrice

        # STOCK / ETF
        price = ticker.marketPrice()
        if price and price > 0:
            return price

        if ticker.close and ticker.close > 0:
            return ticker.close

    raise RuntimeError(f"No usable price for {contract.symbol}")

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

        # Request option market data ONCE
        ticker = ib.reqMktData(opt, '', snapshot=False)
        await asyncio.sleep(0.5)

        print(ticker)
        mg = ticker.modelGreeks
        if not mg or mg.delta is None:
            log(f"Skipping {opt.symbol} {opt.localSymbol}: no modelGreeks")
            continue

        delta = mg.delta
        opt_price = mg.optPrice
        S = mg.undPrice
        iv = mg.impliedVol

        multiplier = int(opt.multiplier or 100)
        total_delta = delta * p.position * multiplier

        deltas[opt.symbol] = deltas.get(opt.symbol, 0.0) + total_delta
        notionals[opt.symbol] = notionals.get(opt.symbol, 0) + abs(
            p.position * multiplier
        )

        log(
            f"{opt.symbol} {opt.right}{opt.strike} "
            f"IV={iv*100:.2f}% "
            f"delta={total_delta:.1f} "
            f"pos={p.position} "
            f"optPrice={opt_price:.2f} "
            f"S={S:.2f}"
        )

    return deltas, notionals

def get_stock_position_per_underlying(ib):
    stock_pos = {}
    for p in ib.positions():
        if p.contract.secType == 'STK':
            stock_pos[p.contract.symbol] = \
                stock_pos.get(p.contract.symbol, 0) + p.position
    return stock_pos

####################################
# HEDGING
####################################

async def hedge(ib, option_delta, option_notional, stock_pos):
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
            f"{symbol}: delta={delta:.1f} "
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
        log("Already hedged today. Exiting.")
        return

    ib = IB()
    await ib.connectAsync(
        IB_HOST,
        IB_PORT,
        clientId=CLIENT_ID,
        timeout=5
    )

    # IMPORTANT: delayed data
    ib.reqMarketDataType(3)

    deltas, notionals = await get_option_delta_per_underlying(ib)
    stock_pos = get_stock_position_per_underlying(ib)

    await hedge(ib, deltas, notionals, stock_pos)

    mark_hedged_today()
    ib.disconnect()

####################################
# RUN
####################################

if __name__ == "__main__":
    asyncio.run(main())
