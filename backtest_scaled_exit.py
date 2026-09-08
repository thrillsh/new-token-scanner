"""
Backtest: scaled/tranche exit vs. hold-to-close, run against YOUR OWN
already-collected data/paper_trades.json.

This is pure analysis, offline, read-only. It does not touch a wallet,
does not execute anything, does not run going forward, and does not
modify paper_trades.json -- it only reads it and writes a report.

WHY THIS EXISTS
Real prices don't move in a straight line; a position that peaks +500%
can (and often does) also fall back or rug before you'd have a chance to
sell everything at that peak. Rather than debate exit rules in the
abstract, this replays each of your CLOSED positions' actual recorded
price history and asks: if a rule had sold fractions of the position at
certain profit thresholds along the way, would the blended result have
beaten just holding 100% to whatever the position eventually closed at?

RULES COMPARED (see outline -- these are a starting set to compare
against each other and the baseline, not a claim that any one is
"correct"):
  baseline           : hold 100% to close (this is your real dashboard number)
  conservative_scaled: 25% at +50%, 25% at +150%, 25% at +400%, rest to close
  patient_scaled     : 25% at +100%, 25% at +300%, 25% at +1000%, rest to close
  moonbag            : 50% at +100%, rest to close

A tranche that never reaches its threshold simply never sells early --
it's included in the "rest to close" portion instead, same as baseline
for that fraction.

Only CLOSED positions get the headline comparison, since their outcome
is real and final. OPEN positions (including any huge unrealized
outlier) are shown separately as a preview using their most recent
price, clearly labeled as unrealized/not final.

Usage:
    python backtest_scaled_exit.py
Reads:  data/paper_trades.json
Writes: data/backtest_report.json  (full per-position + aggregate detail)
Prints: a summary table to the terminal
"""
import json
import os

PAPER_TRADES_FILE = 'data/paper_trades.json'
REPORT_FILE = 'data/backtest_report.json'

RULES = {
    "baseline": [],  # no early tranches; 100% at close
    "conservative_scaled": [
        (0.25, 50.0), (0.25, 150.0), (0.25, 400.0),
    ],  # remaining 0.25 goes to close
    "patient_scaled": [
        (0.25, 100.0), (0.25, 300.0), (0.25, 1000.0),
    ],
    "moonbag": [
        (0.50, 100.0),
    ],
}


def first_crossing_price(history: list, entry_price: float, threshold_pct: float):
    """First snapshot where pct_change >= threshold_pct. Returns that
    snapshot's price, or None if the threshold was never reached."""
    for snap in history:
        if snap["pct_change"] >= threshold_pct:
            return snap["price"]
    return None


def simulate_rule(position: dict, tranches: list) -> dict:
    """
    Returns the blended exit multiple (final_value / entry_value) for
    one rule on one position, plus which tranches actually fired.
    """
    entry_price = position["entry_price"]
    history = position["history"]
    close_price = history[-1]["price"]  # last known price: real close if
                                          # closed, most recent snapshot if open

    remaining_fraction = 1.0
    weighted_value = 0.0
    fired = []

    for fraction, threshold in tranches:
        exit_price = first_crossing_price(history, entry_price, threshold)
        if exit_price is not None and entry_price:
            weighted_value += fraction * (exit_price / entry_price)
            remaining_fraction -= fraction
            fired.append({"threshold_pct": threshold, "fraction": fraction, "exit_price": exit_price})
        # if never reached, that fraction just stays in remaining_fraction
        # and exits at close instead (handled below)

    if entry_price:
        weighted_value += remaining_fraction * (close_price / entry_price)

    return {
        "multiple": weighted_value,
        "pct_return": (weighted_value - 1.0) * 100,
        "tranches_fired": fired,
        "remaining_fraction_at_close": remaining_fraction,
    }


def run_backtest():
    if not os.path.exists(PAPER_TRADES_FILE):
        print(f"No {PAPER_TRADES_FILE} found -- nothing to backtest yet.")
        return

    with open(PAPER_TRADES_FILE) as f:
        data = json.load(f)

    positions = data.get("positions", [])
    closed = [p for p in positions if p["status"] == "closed" and p.get("history")]
    open_ = [p for p in positions if p["status"] == "open" and p.get("history")]

    if not closed:
        print("No closed positions with history yet -- backtest needs at least a few to be meaningful.")
        return

    per_position_results = []
    rule_aggregates = {name: {"total_multiple": 0.0, "wins": 0, "n": 0} for name in RULES}

    for pos in closed:
        row = {
            "symbol": pos["symbol"], "network": pos["network"],
            "baseline_pct": (pos["history"][-1]["price"] / pos["entry_price"] - 1) * 100 if pos["entry_price"] else 0.0,
            "rules": {},
        }
        for rule_name, tranches in RULES.items():
            result = simulate_rule(pos, tranches)
            row["rules"][rule_name] = result
            agg = rule_aggregates[rule_name]
            agg["total_multiple"] += result["multiple"]
            agg["n"] += 1
            if result["pct_return"] > 0:
                agg["wins"] += 1
        per_position_results.append(row)

    # Disaster cases: baseline ended at -90% or worse (rugs / near-total loss)
    disasters = [r for r in per_position_results if r["baseline_pct"] <= -90.0]
    # Big winners: baseline ended above +100%
    big_winners = [r for r in per_position_results if r["baseline_pct"] >= 100.0]

    print(f"\n{'='*72}\nSCALED EXIT BACKTEST -- {len(closed)} closed positions\n{'='*72}")
    print(f"{'Rule':<22} {'Avg return':>12} {'Win rate':>10} {'n':>5}")
    print("-" * 72)
    for name, agg in rule_aggregates.items():
        avg_multiple = agg["total_multiple"] / agg["n"]
        avg_pct = (avg_multiple - 1) * 100
        win_rate = agg["wins"] / agg["n"] * 100
        print(f"{name:<22} {avg_pct:>11.2f}% {win_rate:>9.1f}% {agg['n']:>5}")

    if disasters:
        print(f"\n--- {len(disasters)} disaster cases (baseline <= -90%) ---")
        print(f"{'Rule':<22} {'Avg return on these':>20}")
        for name in RULES:
            avg = sum(r["rules"][name]["pct_return"] for r in disasters) / len(disasters)
            print(f"{name:<22} {avg:>19.2f}%")

    if big_winners:
        print(f"\n--- {len(big_winners)} big winners (baseline >= +100%) ---")
        print(f"{'Rule':<22} {'Avg return on these':>20}")
        for name in RULES:
            avg = sum(r["rules"][name]["pct_return"] for r in big_winners) / len(big_winners)
            print(f"{name:<22} {avg:>19.2f}%")

    if open_:
        print(f"\n--- {len(open_)} OPEN positions (unrealized, most recent price, NOT final) ---")
        for pos in open_:
            baseline_pct = (pos["history"][-1]["price"] / pos["entry_price"] - 1) * 100 if pos["entry_price"] else 0.0
            print(f"  {pos['symbol']:<18} baseline (unrealized): {baseline_pct:>12.2f}%")

    report = {
        "closed_count": len(closed),
        "open_count": len(open_),
        "rule_aggregates": {
            name: {
                "avg_pct_return": (agg["total_multiple"] / agg["n"] - 1) * 100,
                "win_rate_pct": agg["wins"] / agg["n"] * 100,
                "n": agg["n"],
            } for name, agg in rule_aggregates.items()
        },
        "disaster_case_count": len(disasters),
        "big_winner_count": len(big_winners),
        "per_position": per_position_results,
    }
    os.makedirs('data', exist_ok=True)
    with open(REPORT_FILE, 'w') as f:
        json.dump(report, f, indent=2)
    print(f"\nFull detail written to {REPORT_FILE}")


if __name__ == '__main__':
    run_backtest()
