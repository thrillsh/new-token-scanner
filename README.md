# New Token Scanner — Project Overview & Update Log

A read-only research and paper-trading toolkit for tracking new token launches.
Every execution step is manual — nothing in this project holds a private key
or signs a transaction. Signals are detected automatically; trades are
confirmed by a human, every time.

## Architecture

```
┌─────────────────────┐      commits JSON       ┌──────────────────────┐
│  GitHub Actions      │ ───────────────────────▶│  Repo (data/*.json)  │
│  (cron, every 5 min) │                          │  source of truth     │
│                      │                          └───────────┬──────────┘
│  - new_token_scanner │                                      │ raw fetch
│  - ci_check_prices   │                                      │ (polling)
│  - threshold alerts  │                                      ▼
└─────────────────────┘                          ┌──────────────────────┐
         ▲                                        │  Dashboard (static)  │
         │ repository_dispatch                    │  GitHub Pages        │
         └────────────────────────────────────────│  - shows positions   │
              (Track/Close button clicks)          │  - shows signals     │
                                                    │  - Buy/Sell links →  │
                                                    │    opens Jupiter/    │
                                                    │    Uniswap for YOU   │
                                                    │    to confirm        │
                                                    └──────────────────────┘
```

**Detector (GitHub Actions):** scans for new tokens, tracks paper positions,
checks prices on a 5-minute cron, and writes results back to the repo.

**Source of truth (repo JSON files):** `data/scan_results.json`,
`data/paper_trades.json` — committed by Actions, read by everything else.

**Cockpit (Dashboard):** static site polling the repo's raw files, rendering
current positions and any signal that crossed a threshold. Track/Close
actions fire a `repository_dispatch` event back to Actions. Buy/Sell actions
generate a pre-filled swap link (Jupiter for Solana, Uniswap/1inch for EVM)
that opens in your own wallet for manual review and confirmation.

No component in this pipeline is authorized to execute a trade on its own.

## Components

| File | Role |
|---|---|
| `new_token_scanner.py` | Discovers new liquidity pool launches; honeypot/eligibility checks |
| `dashboard.html` | Static dashboard UI — positions, signals, swap links |
| `dashboard_server.py` | *(legacy, local-only)* Flask server for local dev; not used in hosted deploy |
| `ci_check_prices.py` | Runs in Actions; checks open positions, appends price history, flags threshold crossings |
| `track-prices.yml` | GitHub Actions workflow — 5-min cron, runs the price checker, commits results |
| `backtest_scaled_exit.py` | Offline analysis — compares exit strategies against closed paper positions |

## Recent Updates

- **Hosting decision:** dashboard moves to GitHub Pages (static hosting,
  no cold starts, no backend to maintain).
- **Data flow simplified:** dashboard reads `data/*.json` directly from
  `raw.githubusercontent.com` on a client-side poll (~30–60s), rather than
  through a server API. Chosen over the GitHub API specifically to avoid
  the 60 req/hr unauthenticated rate limit and to avoid putting an
  auth token in client-side code just to read public data.
- **Write path:** Track/Close actions now trigger a `repository_dispatch`
  event (via a narrowly-scoped GitHub token) instead of a direct server
  write, so GitHub Actions remains the only thing that commits changes.
- **Safety check:** both the dashboard's local watch route and the real
  GitHub Actions `on-watch-token.yml` workflow now reject incomplete
  payloads before writing a paper-trade entry, preventing malformed
  `data/paper_trades.json` records that could break later price checks.
- **New: swap link builder.** Replaces the earlier private-key-based
  `DexTrader` buy/sell skeleton. Given a token address, network, and
  amount, it builds a pre-filled swap URL (Jupiter / Uniswap) — no key
  material, no signing, no execution. The user clicks, reviews the quote
  in their own wallet, and confirms manually.
- **Alerting unchanged:** `ci_check_prices.py` keeps sending ntfy alerts
  on threshold crossings; the dashboard's signal panel is a visual
  companion to those, not a replacement.

## Explicitly out of scope

This project does not and will not include code that holds a private key,
signs a transaction, or executes a buy/sell autonomously. Any future
"automation" work should assume the human clicks the final confirm button
in their own wallet.
