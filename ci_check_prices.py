"""
CI price checker -- meant to run under GitHub Actions on a schedule (see
.github/workflows/track-prices.yml). Basic/testing-stage version: one
check per run, no intra-run loop yet, no discovery scan here (that stays
in new_token_scanner.py on its own schedule).

What it does, every run:
  1. Load data/paper_trades.json
  2. Group OPEN positions by network (only tracked tokens -- never all
     scan candidates, see fetch_prices_batch)
  3. One batched API call per network (not per token)
  4. Append a price snapshot to each position's history
  5. Check each position against ALERT_THRESHOLDS; send an ntfy
     notification the FIRST time a threshold is crossed (de-duped via
     an "alerted_thresholds" list stored on the position itself, so the
     dedupe survives across runs/restarts -- state has to live in the
     committed file since GitHub Actions runners are stateless)
  6. Save data/paper_trades.json back out

The GitHub Actions workflow is responsible for committing the changed
file -- this script only writes locally, same as it would running on
your own machine.

Still simulated-only: reads/writes data/paper_trades.json, sends a
notification message. No wallet, no order execution, nothing here can
place a trade.
"""
import os
import requests
from datetime import datetime, timezone

import new_token_scanner as nts

# Positive = pump thresholds (profit-taking signal to go check manually).
# Negative = drop thresholds (early rug/dump warning).
ALERT_THRESHOLDS = [50, 100, 150, 300, 400, 1000, -30, -50, -80]

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}" if NTFY_TOPIC else None


def send_alert(title: str, message: str, priority: str = "default", tags: str = ""):
    """
    HTTP headers only support ASCII/latin-1 -- raw emoji unicode in the
    Title header (like the original rocket/warning symbols) fails to
    encode and silently breaks every send. ntfy's fix for this is the
    "Tags" header: comma-separated short codes (e.g. "rocket",
    "warning") that ntfy's own apps render as emoji client-side. Title
    stays plain ASCII text; tags carry the visual signal instead.
    See: https://docs.ntfy.sh/publish/#tags-emojis
    """
    if not NTFY_URL:
        print(f"[no NTFY_TOPIC set -- would have alerted] {title}: {message}")
        return
    try:
        ascii_title = title.encode("ascii", "ignore").decode().strip()
        if not ascii_title:
            # Symbol was entirely non-ASCII (e.g. 屎壳郎, 😎WOJA) and got
            # stripped to nothing -- fall back to a generic title rather
            # than send an empty/whitespace-only header, which some
            # HTTP clients (including requests) reject outright.
            ascii_title = "Tracked token alert"
        headers = {"Title": ascii_title, "Priority": priority}
        if tags:
            headers["Tags"] = tags
        requests.post(
            NTFY_URL,
            data=message.encode("utf-8"),
            headers=headers,
            timeout=10,
        )
    except Exception as e:
        print(f"  ntfy send failed (continuing anyway): {e}")


def check_thresholds(position: dict, pct_change: float) -> list:
    """Returns newly-crossed thresholds not already alerted for this
    position, and records them so they won't fire again."""
    already = set(position.setdefault("alerted_thresholds", []))
    newly_crossed = []
    for t in ALERT_THRESHOLDS:
        if t in already:
            continue
        crossed = (pct_change >= t) if t > 0 else (pct_change <= t)
        if crossed:
            newly_crossed.append(t)
            already.add(t)
    position["alerted_thresholds"] = sorted(already)
    return newly_crossed


def run_check():
    trades = nts.load_paper_trades()
    open_positions = [p for p in trades["positions"] if p["status"] == "open"]

    if not open_positions:
        print("No open positions to check.")
        return

    by_network = {}
    for pos in open_positions:
        by_network.setdefault(pos["network"], []).append(pos)

    now = datetime.now(timezone.utc).isoformat()
    changed = False

    for network, positions in by_network.items():
        addresses = [p["token_address"] for p in positions]
        prices = nts.fetch_prices_batch(network, addresses)  # 1 call per network

        for pos in positions:
            price = prices.get(pos["token_address"])
            if price is None:
                print(f"  [{network}] {pos['symbol']}: no price this check, skipped")
                continue

            pct_change = ((price - pos["entry_price"]) / pos["entry_price"]) * 100 if pos["entry_price"] else 0.0
            pos["history"].append({"time": now, "price": price, "pct_change": round(pct_change, 2)})
            changed = True

            crossed = check_thresholds(pos, pct_change)
            for t in crossed:
                direction = "pumped" if t > 0 else "dropped"
                tag = "rocket" if t > 0 else "warning"
                priority = "high" if abs(t) >= 100 else "default"
                send_alert(
                    title=f"{pos['symbol']} {direction} {t:+d}%",
                    message=(f"{pos['symbol']} ({network}) now at {pct_change:+.2f}% "
                             f"since entry (${pos['entry_price']:.8f} -> ${price:.8f}). "
                             f"Simulated tracking only -- nothing bought."),
                    priority=priority,
                    tags=tag,
                )
                print(f"  ALERT: {pos['symbol']} crossed {t:+d}% (now {pct_change:+.2f}%)")
                nts.log_event("threshold_alert", {
                    "symbol": pos["symbol"], "network": network, "token_address": pos["token_address"],
                    "threshold_pct": t, "pct_change_at_alert": round(pct_change, 2), "price": price,
                })

    if changed:
        nts.save_paper_trades(trades)
        print("Saved updated paper_trades.json")
    else:
        print("No price data received this run -- nothing to save")

    nts.log_event("price_check_run", {
        "positions_checked": len(open_positions),
        "networks": list(by_network.keys()),
        "positions_updated": sum(1 for p in open_positions if p["history"][-1]["time"] == now),
    })


if __name__ == '__main__':
    run_check()
