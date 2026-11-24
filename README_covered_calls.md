# Covered calls via the IBKR Client Portal Web API (REST)

`covered_calls.py` sells calls against the uncovered 100-share lots in every IB account. It talks to
IBKR's REST API (Client Portal Web API). You don't need TWS or the IB Gateway app, but IBKR does require
its small **Client Portal Gateway** running locally, plus a browser login (2FA) roughly once a day.
IBKR only offers login-free OAuth to organizational accounts.

## One-time setup

1. **Java**, which the gateway needs:
   `brew install openjdk@21`, then follow brew's hint to link it, or add
   `export PATH="/opt/homebrew/opt/openjdk@21/bin:$PATH"` to `~/.zshrc`.
2. **Latest gateway.** The copy in `../clientportal` is from 2022 and should be replaced:
   download https://download2.interactivebrokers.com/portal/clientportal.gw.zip and unzip it over `../clientportal`.
   Then set `listenPort: 5055` in `root/conf.yaml`. **macOS AirPlay Receiver owns port 5000.**
3. Python deps: `python3 -m venv .venv && .venv/bin/pip install requests`

## Each time you run it

```bash
cd ../clientportal && bin/run.sh root/conf.yaml      # leave running
# browser: https://localhost:5055  → accept the self-signed cert → log in → approve 2FA
cd ../IB
.venv/bin/python covered_calls.py                    # DRY RUN: prints + saves plans/covered_calls_*.csv
.venv/bin/python covered_calls.py --live             # places the orders after you type YES
```

## Rules (defaults; all are flags)

| | default | flag |
|---|---|---|
| expiry | 21–49 days out, closest to 35 | `--dte-min/--dte-max/--dte-target` |
| strike | call delta closest to 0.20, never above 0.30, at least 3% OTM | `--delta --max-delta --min-otm` |
| liquidity | bid at least $0.10 | `--min-bid` |
| size | every uncovered 100-share lot | `--coverage 0.5` |
| price | DAY limit at the mid | |
| cost basis | off; `--above-cost` never picks a strike below your average cost | |
| scope | all accounts / symbols | `--accounts --symbols --exclude` |

Symbols that already have short calls count as covered. A symbol with a working SELL call order is skipped.
Live greeks and quotes need the OPRA options market-data subscription, and the market should be open.

## Merrill Edge

Merrill has no trading API for individual accounts. Export positions from Merrill (Positions → Download CSV), then:

```bash
.venv/bin/python covered_calls.py --merrill-csv ~/Downloads/Positions.csv
```

This prints the same plan, priced with IB market data, for you to enter in Merrill by hand.
