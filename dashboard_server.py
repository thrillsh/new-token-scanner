"""
Dashboard server for New Token Scanner -- serves dashboard.html and
data/scan_results.json exactly like `python -m http.server` did before,
but ALSO exposes a few tiny read/write API routes so the dashboard itself
can let you click "Track" on any card and watch simulated-hold progress
inline, instead of using the CLI's --paper-trade/--pick flags.

Still 100% read-only with respect to real money: every route here only
ever reads/writes data/paper_trades.json -- the SAME simulated-only file
the CLI's --paper-trade / --paper-update flags already use, so the
dashboard and the CLI share one consistent tracked list either way. No
wallet, no private key, no order execution anywhere in this file.

Run with:   python dashboard_server.py
Then open:  http://localhost:8000/

(Replaces `python -m http.server` for this project -- that command still
works for just viewing the static grid, but won't support the Track
button or the Tracked panel below, since those need somewhere to write.)
"""
import os
import threading
import time
from datetime import datetime, timezone
from flask import Flask, jsonify, request, send_from_directory

import new_token_scanner as nts

app = Flask(__name__, static_folder='.', static_url_path='')

# Auto-snapshot cadence: every 5 min for the first hour after entry (to
# actually test the "most tokens peak within 30 min" hypothesis with real
# resolution), then hourly after that so it doesn't hammer the API forever
# on positions you're holding longer. Manual "Refresh prices" in the
# dashboard still works any time on top of this -- this just fills in the
# gaps automatically so you're not relying on remembering to click.
FIRST_HOUR_INTERVAL_SEC = 5 * 60
AFTER_FIRST_HOUR_INTERVAL_SEC = 60 * 60
SCHEDULER_TICK_SEC = 30  # how often the background thread wakes up to check


def _snapshot_due(pos: dict, now: datetime) -> bool:
    last = pos["history"][-1]
    last_time = datetime.fromisoformat(last["time"])
    entry_time = datetime.fromisoformat(pos["entry_time"])
    age_since_entry = (now - entry_time).total_seconds()
    since_last_snapshot = (now - last_time).total_seconds()
    interval = FIRST_HOUR_INTERVAL_SEC if age_since_entry <= 3600 else AFTER_FIRST_HOUR_INTERVAL_SEC
    return since_last_snapshot >= interval


def run_price_updates() -> int:
    """Shared by the manual /api/paper_trades/update route AND the
    background auto-scheduler below, so both paths log snapshots in
    exactly the same format. Returns number of positions updated."""
    trades = nts.load_paper_trades()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    updated = 0
    for pos in trades["positions"]:
        if pos["status"] != "open":
            continue
        price = nts.fetch_current_price(pos["network"], pos["token_address"])
        if price is None:
            continue
        pct_change = ((price - pos["entry_price"]) / pos["entry_price"]) * 100 if pos["entry_price"] else 0.0
        pos["history"].append({"time": now, "price": price, "pct_change": round(pct_change, 2)})
        updated += 1
    if updated:
        nts.save_paper_trades(trades)
    return updated


def _auto_snapshot_loop():
    """Background daemon thread: wakes every SCHEDULER_TICK_SEC, checks
    which OPEN positions are due for a snapshot per the cadence above,
    and fetches only those (not a blanket update-everything each tick --
    keeps API usage proportional to how many positions are actually in
    their first hour). Still simulated-only: reads/writes the same
    data/paper_trades.json, never touches a wallet."""
    while True:
        try:
            trades = nts.load_paper_trades()
            now_dt = datetime.now(timezone.utc)
            due = [p for p in trades["positions"]
                   if p["status"] == "open" and _snapshot_due(p, now_dt)]
            if due:
                now = now_dt.isoformat()
                changed = False
                for pos in due:
                    price = nts.fetch_current_price(pos["network"], pos["token_address"])
                    if price is None:
                        continue
                    pct_change = ((price - pos["entry_price"]) / pos["entry_price"]) * 100 if pos["entry_price"] else 0.0
                    pos["history"].append({"time": now, "price": price, "pct_change": round(pct_change, 2)})
                    changed = True
                if changed:
                    nts.save_paper_trades(trades)
        except Exception as e:
            print(f"[auto-snapshot] skipped a cycle due to error: {e}")
        time.sleep(SCHEDULER_TICK_SEC)


@app.route('/')
def index():
    return send_from_directory('.', 'dashboard.html')


@app.route('/data/scan_results.json')
def scan_results():
    if not os.path.exists('data/scan_results.json'):
        return jsonify({"error": "no scan yet -- run new_token_scanner.py first"}), 404
    return send_from_directory('data', 'scan_results.json')


@app.route('/api/paper_trades', methods=['GET'])
def get_paper_trades():
    return jsonify(nts.load_paper_trades())


@app.route('/api/watch', methods=['POST'])
def watch():
    """Manually track WHATEVER candidate was clicked on the dashboard --
    unlike the CLI's --paper-trade (which auto-ranks and filters by
    honeypot status), this opens a simulated position on exactly the card
    you clicked, no eligibility filter applied. You already saw that
    card's honeypot status and red flags before clicking -- this doesn't
    re-check or override your own judgment call."""
    c = request.get_json(force=True) or {}
    token_address = c.get("token_address")
    if not token_address:
        return jsonify({"error": "missing token_address"}), 400

    trades = nts.load_paper_trades()
    already = {p["token_address"] for p in trades["positions"] if p["status"] == "open"}
    if token_address in already:
        return jsonify({"error": "already tracking this token"}), 409

    now = datetime.now(timezone.utc).isoformat()
    position = {
        "symbol": c.get("symbol"),
        "name": c.get("name"),
        "network": c.get("network"),
        "token_address": token_address,
        "entry_time": now,
        "entry_price": c.get("price_usd"),
        "entry_liquidity_usd": c.get("reserve_usd"),
        "entry_volume_24h_usd": c.get("volume_24h_usd"),
        "status": "open",
        "history": [{"time": now, "price": c.get("price_usd"), "pct_change": 0.0}],
    }
    trades["positions"].append(position)
    nts.save_paper_trades(trades)
    return jsonify(position)


@app.route('/api/paper_trades/<path:token_address>/close', methods=['POST'])
def close_trade(token_address):
    trades = nts.load_paper_trades()
    for p in trades["positions"]:
        if p["token_address"] == token_address and p["status"] == "open":
            p["status"] = "closed"
            p["closed_time"] = datetime.now(timezone.utc).isoformat()
            nts.save_paper_trades(trades)
            return jsonify(p)
    return jsonify({"error": "not found or already closed"}), 404


@app.route('/api/paper_trades/update', methods=['POST'])
def update_trades():
    """Manual, on-demand refresh for every open simulated position -- same
    logic as the CLI's --paper-update and the same one the background
    auto-scheduler uses, just triggered immediately by the dashboard's
    Refresh button instead of waiting for the next scheduled snapshot."""
    run_price_updates()
    return jsonify(nts.load_paper_trades())


if __name__ == '__main__':
    print("New Token Scanner dashboard: http://localhost:8000/")
    print("(Ctrl+C to stop -- this only serves local files and reads/writes")
    print(" data/paper_trades.json, a simulated-only log. Nothing here trades.)")
    print(f"Auto-snapshotting open positions every {FIRST_HOUR_INTERVAL_SEC//60}min "
          f"for their first hour, then every {AFTER_FIRST_HOUR_INTERVAL_SEC//60}min after.")
    scheduler_thread = threading.Thread(target=_auto_snapshot_loop, daemon=True)
    scheduler_thread.start()
    app.run(port=8000, debug=False)
