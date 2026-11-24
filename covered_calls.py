"""
Covered-call writer for Interactive Brokers via the Client Portal Web API (REST) — no TWS / IB Gateway app.
Also a planning mode for Merrill Edge holdings (Merrill has no retail trading API).

How it runs: IBKR's small Client Portal Gateway (Java, ../clientportal) proxies REST calls to IBKR. Start it
(it listens on 5055: macOS AirPlay Receiver owns port 5000),
open https://localhost:5055 in a browser and log in (2FA) — the session lasts ~a day. Then run this script.
IBKR only offers login-free OAuth to organizational accounts, so individual accounts need that browser login.

For every stock position, sells calls against the full 100-share lots that are not already covered:
  1. Positions per account. Short calls already open on the same symbol count as covered; symbols with a
     working SELL call order are skipped (no double-writes).
  2. Expiry: DTE_MIN..DTE_MAX days out, the one closest to DTE_TARGET.
  3. Strike: OTM calls at least MIN_OTM_PCT above spot, delta closest to TARGET_DELTA (<= MAX_DELTA),
     bid >= MIN_BID; optionally never below your average cost (--above-cost).
  4. DAY limit order at the mid, rounded to the option tick.

DRY RUN BY DEFAULT: prints the plan and saves it to plans/. --live places the orders after you type YES.

  python3 covered_calls.py                          # plan for every IB account
  python3 covered_calls.py --symbols AAPL,MSFT --coverage 0.5
  python3 covered_calls.py --live                   # place them (asks for YES)
  python3 covered_calls.py --merrill-csv ~/Downloads/Positions.csv   # plan Merrill holdings to enter by hand
"""
import argparse
import csv
import math
import os
import sys
import time
import uuid
from datetime import date, datetime

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)   # the local gateway uses a self-signed cert

# ── defaults (all overridable on the command line) ──────────────────────────────
DTE_MIN, DTE_MAX, DTE_TARGET = 21, 49, 35
TARGET_DELTA, MAX_DELTA = 0.20, 0.30
MIN_OTM_PCT = 0.03          # strike at least 3% above spot
MIN_BID = 0.10              # skip calls bidding less than this
COVERAGE = 1.0              # fraction of uncovered 100-share lots to write against
MAX_STRIKES_TO_QUOTE = 12   # candidate strikes quoted per symbol
RISK_FREE = 0.045
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
# snapshot field ids: 31 last, 84 bid, 86 ask, 7308 delta, 7633 implied vol (%)
F_LAST, F_BID, F_ASK, F_DELTA, F_IV = "31", "84", "86", "7308", "7633"


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── pure helpers (unit-testable, no IB needed) ──────────────────────────────────
def pick_expiry(expirations, today, dte_min=DTE_MIN, dte_max=DTE_MAX, dte_target=DTE_TARGET):
    """expirations: iterable of 'YYYYMMDD'. -> (expiry, dte) closest to target inside [min, max], or None."""
    best = None
    for e in set(expirations):
        dte = (datetime.strptime(e, "%Y%m%d").date() - today).days
        if dte_min <= dte <= dte_max and (best is None or abs(dte - dte_target) < abs(best[1] - dte_target)):
            best = (e, dte)
    return best


def months_in_window(today, dte_min=DTE_MIN, dte_max=DTE_MAX):
    """Option-month codes ('DEC26') touched by the DTE window."""
    out, d = [], date.fromordinal(today.toordinal() + dte_min)
    end = date.fromordinal(today.toordinal() + dte_max)
    while (d.year, d.month) <= (end.year, end.month):
        out.append(f"{MONTHS[d.month - 1]}{d.year % 100:02d}")
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return out


def candidate_strikes(strikes, spot, min_otm_pct=MIN_OTM_PCT, floor=None, n=MAX_STRIKES_TO_QUOTE):
    """Lowest n listed strikes at/above max(spot*(1+min_otm), floor)."""
    lo = spot * (1 + min_otm_pct)
    if floor:
        lo = max(lo, floor)
    return sorted(k for k in set(strikes) if k >= lo - 1e-9)[:n]


def bs_call_delta(S, K, T, sigma, r=RISK_FREE):
    if T <= 0 or sigma <= 0 or S <= 0:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return 0.5 * (1 + math.erf(d1 / math.sqrt(2)))


def pick_strike(quotes, target=TARGET_DELTA, max_delta=MAX_DELTA, min_bid=MIN_BID):
    """quotes: [dict(strike, delta, bid, ask)] -> delta closest to target (<= max_delta, bid >= min_bid) or None."""
    ok = [q for q in quotes if q["delta"] is not None and q["delta"] <= max_delta
          and q["bid"] is not None and q["bid"] >= min_bid and q["ask"] and q["ask"] >= q["bid"]]
    return min(ok, key=lambda q: abs(q["delta"] - target)) if ok else None


def limit_price(bid, ask):
    """Mid, rounded to the option tick (0.01 under $3, 0.05 at/above), never below the bid."""
    mid = (bid + ask) / 2
    tick = 0.01 if mid < 3 else 0.05
    return max(round(round(mid / tick) * tick, 2), bid)


def lots_to_write(shares, short_calls, coverage=COVERAGE):
    """Uncovered full 100-share lots, times coverage (rounded down)."""
    free = int(shares // 100) - int(short_calls)
    return max(0, int(math.floor(free * coverage)))


def num(v):
    """Snapshot values come back as strings, sometimes prefixed ('C1.23' = closing, 'H' = halted)."""
    if v is None:
        return None
    try:
        return float(str(v).lstrip("CH").replace(",", "").replace("%", ""))
    except ValueError:
        return None


def read_merrill_csv(path):
    """Merrill Edge 'Positions' export -> {symbol: shares}. Finds the header row with Symbol and Quantity."""
    rows = list(csv.reader(open(os.path.expanduser(path), newline="", encoding="utf-8-sig")))
    for i, r in enumerate(rows):
        low = [c.strip().lower() for c in r]
        if any(c.startswith("symbol") for c in low) and any(c.startswith("quantity") for c in low):
            si = next(j for j, c in enumerate(low) if c.startswith("symbol"))
            qi = next(j for j, c in enumerate(low) if c.startswith("quantity"))
            out = {}
            for r2 in rows[i + 1:]:
                if len(r2) <= max(si, qi):
                    continue
                sym = r2[si].strip().upper()
                try:
                    qty = float(r2[qi].replace(",", ""))
                except ValueError:
                    continue
                if sym and sym.isalpha() and len(sym) <= 5 and qty >= 100:   # stocks/ETFs only, round lots
                    out[sym] = out.get(sym, 0) + qty
            return out
    sys.exit(f"{path}: no header row with Symbol and Quantity columns")


# ── Client Portal Web API ───────────────────────────────────────────────────────
class CP:
    def __init__(self, base):
        self.base = base.rstrip("/") + "/v1/api"
        self.s = requests.Session()
        self.s.verify = False
        self.s.headers["User-Agent"] = "covered-calls/1.0"
        self._last = 0.0

    def _call(self, method, path, **kw):
        wait = 0.12 - (time.time() - self._last)        # stay well under the gateway's ~10 req/s pacing
        if wait > 0:
            time.sleep(wait)
        for attempt in range(4):
            self._last = time.time()
            r = self.s.request(method, self.base + path, timeout=(5, 30), **kw)
            if r.status_code == 429:
                time.sleep(1 + attempt); continue
            if r.status_code == 401:
                sys.exit("Not logged in: open https://localhost:5055 in a browser and log in, then re-run.")
            r.raise_for_status()
            return r.json() if r.content else None
        r.raise_for_status()

    def get(self, path, **params):
        return self._call("GET", path, params=params or None)

    def post(self, path, body=None):
        return self._call("POST", path, json=body or {})

    # session
    def ensure_session(self):
        try:
            st = self.post("/iserver/auth/status")
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            sys.exit("Client Portal Gateway isn't running on this machine. Start it (see README) and log in at "
                     "https://localhost:5055.")
        if not (st or {}).get("authenticated"):
            self.post("/iserver/reauthenticate")
            time.sleep(3)
            st = self.post("/iserver/auth/status")
        if not (st or {}).get("authenticated"):
            sys.exit("Gateway is up but not logged in: open https://localhost:5055, log in (2FA), then re-run.")
        self.post("/tickle")
        self.get("/portfolio/accounts")     # required before portfolio endpoints
        return self.get("/iserver/accounts")  # required before order endpoints

    # data
    def positions(self, acct):
        out, page = [], 0
        while True:
            rows = self.get(f"/portfolio/{acct}/positions/{page}") or []
            out += rows
            if len(rows) < 100:
                return out
            page += 1

    def open_orders(self):
        self.get("/iserver/account/orders", force="true")            # first call primes the cache
        time.sleep(1)
        return (self.get("/iserver/account/orders") or {}).get("orders", [])

    def stock_conid(self, symbol):
        res = self.get("/iserver/secdef/search", symbol=symbol, secType="STK") or []
        for r in res:
            if (r.get("symbol") or "").upper() == symbol and any(s.get("secType") == "OPT" for s in r.get("sections", [])):
                return int(r["conid"])
        return int(res[0]["conid"]) if res else None

    def snapshot(self, conids, fields, tries=6):
        """Market-data snapshot; the first request only subscribes, so poll until fields arrive."""
        ids = ",".join(str(c) for c in conids)
        got = {}
        for _ in range(tries):
            for row in self.get("/iserver/marketdata/snapshot", conids=ids, fields=",".join(fields)) or []:
                if any(f in row for f in fields):
                    got[int(row["conid"])] = row
            if len(got) == len(conids) and all(F_BID in r or F_LAST in r for r in got.values()):
                break
            time.sleep(1.0)
        return got

    def strikes(self, conid, month):
        return (self.get("/iserver/secdef/strikes", conid=conid, sectype="OPT", month=month) or {}).get("call", [])

    def option_contracts(self, conid, month, strike):
        return self.get("/iserver/secdef/info", conid=conid, sectype="OPT", month=month,
                        strike=strike, right="C") or []

    def place(self, acct, order):
        """Submit one order and answer the gateway's confirmation prompts (logged). -> final response."""
        resp = self.post(f"/iserver/account/{acct}/orders", {"orders": [order]})
        for _ in range(5):
            if isinstance(resp, list) and resp and "id" in resp[0] and "order_id" not in resp[0]:
                log(f"  gateway asks: {' | '.join(resp[0].get('message', []))[:200]} -> confirming")
                resp = self.post(f"/iserver/reply/{resp[0]['id']}", {"confirmed": True})
                continue
            return resp
        return resp


# ── planning ────────────────────────────────────────────────────────────────────
def holdings(cp, accounts):
    """-> {(account, symbol): dict(shares, avg_cost, short_calls)} for stock positions."""
    hold, shorts = {}, {}
    for acct in accounts:
        for p in cp.positions(acct):
            ac, pos = p.get("assetClass"), float(p.get("position") or 0)
            if ac == "STK" and pos > 0:
                sym = (p.get("ticker") or p.get("contractDesc") or "").split()[0].upper()
                hold[(acct, sym)] = dict(shares=pos, avg_cost=float(p.get("avgCost") or 0) or None)
            elif ac == "OPT" and pos < 0 and (p.get("putOrCall") == "C" or " C " in f" {p.get('contractDesc','')} "):
                und = (p.get("undSym") or p.get("ticker") or p.get("contractDesc") or "").split()[0].upper()
                mult = float(p.get("multiplier") or 100)
                shorts[(acct, und)] = shorts.get((acct, und), 0) + (-pos) * mult / 100
    for k, h in hold.items():
        h["short_calls"] = shorts.get(k, 0)
    return hold


def working_call_sells(orders):
    live = {"Submitted", "PreSubmitted", "PendingSubmit", "Inactive"}
    return {(o.get("acct"), (o.get("ticker") or "").upper()) for o in orders
            if o.get("secType") == "OPT" and o.get("side") == "SELL" and o.get("status") in live
            and ("C" == o.get("putOrCall") or " C " in f" {o.get('description1','')} {o.get('description2','')} ")}


def plan_symbol(cp, symbol, avg_cost, a):
    conid = cp.stock_conid(symbol)
    if not conid:
        return dict(skip="symbol not found")
    snap = cp.snapshot([conid], [F_LAST, F_BID, F_ASK]).get(conid, {})
    spot = num(snap.get(F_LAST)) or ((num(snap.get(F_BID)) or 0) + (num(snap.get(F_ASK)) or 0)) / 2
    if not spot:
        return dict(skip="no stock price (market data subscription?)")
    today = date.today()
    months = months_in_window(today, a.dte_min, a.dte_max)
    # listed expiries in the window: ask secdef/info for one near-the-money strike per month
    exps = {}
    for m in months:
        ks = cp.strikes(conid, m)
        if not ks:
            continue
        k_atm = min(ks, key=lambda k: abs(k - spot * (1 + a.min_otm)))
        for c in cp.option_contracts(conid, m, k_atm):
            if c.get("maturityDate"):
                exps[c["maturityDate"]] = (m, ks)
    pick = pick_expiry(exps, today, a.dte_min, a.dte_max, a.dte_target)
    if not pick:
        return dict(skip=f"no expiry {a.dte_min}-{a.dte_max}d out")
    expiry, dte = pick
    month, all_strikes = exps[expiry]
    floor = avg_cost if a.above_cost else None
    ks = candidate_strikes(all_strikes, spot, a.min_otm, floor)
    if not ks:
        return dict(skip="no OTM strikes")
    # conid of the call at each candidate strike for THIS expiry (secdef/info returns every expiry in the month)
    opts = {}
    for k in ks:
        for c in cp.option_contracts(conid, month, k):
            if c.get("maturityDate") == expiry and c.get("right") == "C":
                opts[int(c["conid"])] = dict(strike=float(c.get("strike", k)), desc=c.get("desc2") or c.get("desc1"))
    if not opts:
        return dict(skip="strikes not listed for that expiry")
    snaps = cp.snapshot(list(opts), [F_BID, F_ASK, F_DELTA, F_IV, F_LAST])
    quotes, T = [], dte / 365
    for cid, info in opts.items():
        s = snaps.get(cid, {})
        delta, iv = num(s.get(F_DELTA)), num(s.get(F_IV))
        if delta is None and iv:                                      # no greek → BS from the quoted IV
            delta = bs_call_delta(spot, info["strike"], T, iv / 100 if iv > 3 else iv)
        quotes.append(dict(strike=info["strike"], delta=delta, bid=num(s.get(F_BID)), ask=num(s.get(F_ASK)),
                           conid=cid))
    q = pick_strike(quotes, a.delta, a.max_delta, a.min_bid)
    if not q:
        return dict(skip=f"no strike with delta<={a.max_delta} and bid>={a.min_bid} "
                         f"(quoted {len(quotes)}; outside market hours or no OPRA data?)")
    px = limit_price(q["bid"], q["ask"])
    return dict(symbol=symbol, spot=round(spot, 2), expiry=expiry, dte=dte, strike=q["strike"],
                delta=round(q["delta"], 3), bid=q["bid"], ask=q["ask"], limit=px, conid=q["conid"],
                otm_pct=round(100 * (q["strike"] / spot - 1), 1), yield_ann_pct=round(100 * px / spot * 365 / dte, 1))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gateway", default="https://localhost:5055", help="Client Portal Gateway URL")
    ap.add_argument("--accounts", default="", help="comma list of IB account ids (default: all)")
    ap.add_argument("--symbols", default="", help="only these symbols")
    ap.add_argument("--exclude", default="", help="never write calls on these")
    ap.add_argument("--coverage", type=float, default=COVERAGE, help="fraction of uncovered lots to write (0-1)")
    ap.add_argument("--delta", type=float, default=TARGET_DELTA)
    ap.add_argument("--max-delta", type=float, default=MAX_DELTA)
    ap.add_argument("--min-otm", type=float, default=MIN_OTM_PCT, help="0.03 = strike >= 3%% above spot")
    ap.add_argument("--min-bid", type=float, default=MIN_BID)
    ap.add_argument("--dte-min", type=int, default=DTE_MIN)
    ap.add_argument("--dte-max", type=int, default=DTE_MAX)
    ap.add_argument("--dte-target", type=int, default=DTE_TARGET)
    ap.add_argument("--above-cost", action="store_true", help="never pick a strike below your average cost")
    ap.add_argument("--merrill-csv", help="plan-only for Merrill holdings from its Positions CSV export")
    ap.add_argument("--live", action="store_true", help="place the orders (asks you to type YES)")
    a = ap.parse_args()
    only = {s.strip().upper() for s in a.symbols.split(",") if s.strip()}
    skip = {s.strip().upper() for s in a.exclude.split(",") if s.strip()}

    cp = CP(a.gateway)
    acc = cp.ensure_session()
    all_accts = (acc or {}).get("accounts", [])
    accts = [x for x in all_accts if not a.accounts or x in a.accounts.split(",")]
    log(f"logged in; accounts: {all_accts}")

    if a.merrill_csv:
        hold = {("MERRILL", s): dict(shares=q, avg_cost=None, short_calls=0)
                for s, q in read_merrill_csv(a.merrill_csv).items()}
        busy = set()
        if a.live:
            log("--live ignored for Merrill: no trading API, this is a plan to enter by hand")
            a.live = False
    else:
        hold = holdings(cp, accts)
        busy = working_call_sells(cp.open_orders())

    plans, skipped = [], []
    for (acct, sym), h in sorted(hold.items()):
        if (only and sym not in only) or sym in skip:
            continue
        n = lots_to_write(h["shares"], h["short_calls"], a.coverage)
        if n <= 0:
            skipped.append((acct, sym, f"{h['shares']:g} sh, {h['short_calls']:g} calls already written"))
            continue
        if (acct, sym) in busy:
            skipped.append((acct, sym, "a SELL call order is already working"))
            continue
        try:
            p = plan_symbol(cp, sym, h.get("avg_cost"), a)
        except Exception as e:
            p = dict(skip=f"error: {e!r}"[:160])
        if "skip" in p:
            skipped.append((acct, sym, p["skip"]))
            continue
        p.update(account=acct, contracts=n, shares=h["shares"], premium=round(n * 100 * p["limit"], 2))
        plans.append(p)
        log(f"planned {acct} {sym}")

    print()
    hdr = f"{'account':<11}{'symbol':<7}{'shares':>8}{'qty':>5}  {'expiry':<9}{'dte':>4}{'strike':>9}{'otm%':>6}" \
          f"{'delta':>7}{'bid':>7}{'ask':>7}{'limit':>7}{'premium$':>10}{'ann%':>6}"
    print(hdr); print("-" * len(hdr))
    for p in plans:
        print(f"{p['account']:<11}{p['symbol']:<7}{p['shares']:>8g}{p['contracts']:>5}  {p['expiry']:<9}{p['dte']:>4}"
              f"{p['strike']:>9g}{p['otm_pct']:>6}{p['delta']:>7}{p['bid']:>7}{p['ask']:>7}{p['limit']:>7}"
              f"{p['premium']:>10,.2f}{p['yield_ann_pct']:>6}")
    print(f"\n{len(plans)} orders, total premium ≈ ${sum(p['premium'] for p in plans):,.2f}")
    for acct, sym, why in skipped:
        print(f"  skipped {acct} {sym}: {why}")

    os.makedirs("plans", exist_ok=True)
    out = f"plans/covered_calls_{datetime.now():%Y%m%d_%H%M}.csv"
    with open(out, "w", newline="") as f:
        cols = ["account", "symbol", "shares", "contracts", "expiry", "dte", "strike", "otm_pct", "delta",
                "bid", "ask", "limit", "premium", "yield_ann_pct", "spot", "conid"]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore"); w.writeheader(); w.writerows(plans)
    log(f"plan saved to {out}")

    if not a.live or not plans:
        if not a.merrill_csv:
            print("\nDRY RUN — nothing sent. Re-run with --live to place these orders.")
        return
    if input("\nPlace these SELL-to-open call orders now? Type YES: ").strip() != "YES":
        sys.exit("aborted")
    for p in plans:
        order = dict(conid=p["conid"], orderType="LMT", price=p["limit"], side="SELL", quantity=p["contracts"],
                     tif="DAY", cOID=f"cc-{p['symbol']}-{uuid.uuid4().hex[:8]}")
        try:
            r = cp.place(p["account"], order)
            log(f"{p['account']} SELL {p['contracts']} {p['symbol']} {p['expiry']} {p['strike']:g}C @ {p['limit']}: {r}")
        except Exception as e:
            log(f"{p['account']} {p['symbol']} order FAILED: {e!r}")


if __name__ == "__main__":
    main()
