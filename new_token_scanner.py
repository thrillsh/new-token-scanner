"""
New Token Scanner -- READ-ONLY discovery tool. Surfaces recently-launched
Solana tokens passing basic liquidity/age/safety filters, WITH ready-made
links for you to do your own manual check on X and the project's own site
(whitepaper/docs) before deciding anything.

THIS SCRIPT NEVER BUYS ANYTHING. No wallet, no private key, no order
execution anywhere in this file. It answers one question: "what's worth
me spending 2 minutes checking on X and the project site right now" --
same read-only, human-in-the-loop spirit as market_scanner.py in the
separate trading-bot-project, but for a completely different, much
higher-risk asset class (brand-new tokens, not established liquid ones) --
which is exactly why this stays a SEPARATE tool with SEPARATE, more
cautious defaults, not folded into that project.

Why the filters exist (each one addresses a specific known risk):
- MIN_LIQUIDITY_USD: very low liquidity means a small trade can be the
  entire market, and it's trivial for a dev to pull it all instantly
  (a "rug pull").
- MIN_VOLUME_USD: liquidity alone can be faked/seeded by the deployer;
  real trading volume happening on top of it is a weak but real
  independent signal that other people are actually transacting.
- MAX_POOL_AGE_HOURS: the whole point is catching things EARLY, but also
  narrow enough that you're not drowning in noise from pools created
  days ago that already fully played out.
- honeypot/safety check: see the loud warning in check_basic_safety() --
  this script does NOT reliably detect honeypots itself. Treat its
  liquidity/volume filtering as a coarse noise-reducer, not a safety
  guarantee.

Uses CoinGecko's free Demo API tier (same COINGECKO_API_KEY env var
pattern as the other project) -- the base new_pools endpoint works on
Demo; only the more advanced server-side Megafilter needs a paid plan,
which this script deliberately avoids depending on.
"""

import os
import json
import argparse
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

COINGECKO_API_KEY = os.environ.get('COINGECKO_API_KEY', '')
BASE_URL = "https://api.coingecko.com/api/v3"
HEADERS = {"x-cg-demo-api-key": COINGECKO_API_KEY}

# ─── FILTER THRESHOLDS ─── tune these, but understand each one first (see
# module docstring above for why each exists before loosening any of them).
MIN_LIQUIDITY_USD = 5_000
MIN_VOLUME_USD = 1_000
MAX_POOL_AGE_HOURS = 6

# CoinGecko's onchain network ids -- NOT necessarily the chain's common
# name. Verify against https://api.coingecko.com/api/v3/onchain/networks
# (or CoinGecko's docs) if a network here ever comes back empty -- these
# ids occasionally differ from what you'd guess (e.g. Ethereum is 'eth',
# not 'ethereum'). Solana was verified working against the real tutorial
# this was based on; the others are CoinGecko's documented ids as of this
# writing but weren't individually re-verified live here.
NETWORKS = ["solana", "eth", "bsc", "base"]

# Per-chain block explorer, for the "check the contract yourself" link.
# Falls back to just the DexScreener chart link if a network isn't listed
# here, rather than guessing at a URL pattern that might be wrong.
EXPLORER_URL_TEMPLATES = {
    "solana": "https://solscan.io/token/{address}",
    "eth": "https://etherscan.io/token/{address}",
    "bsc": "https://bscscan.com/token/{address}",
    "base": "https://basescan.org/token/{address}",
}


def fetch_new_pools(network, pages=2):
    """Pulls recently created pools for one network. Returns raw pool
    records with sideloaded token/dex info resolved inline."""
    results = []
    for page in range(1, pages + 1):
        resp = requests.get(
            f"{BASE_URL}/onchain/networks/{network}/new_pools",
            headers=HEADERS,
            params={"include": "base_token,dex", "page": page},
            timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json()
        included = {item["id"]: item for item in payload.get("included", [])}

        for pool in payload.get("data", []):
            attrs = pool["attributes"]
            base_id = pool["relationships"]["base_token"]["data"]["id"]
            base = included.get(base_id, {}).get("attributes", {})

            created_raw = attrs.get("pool_created_at")
            created_at = None
            age_hours = None
            if created_raw:
                created_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
                age_hours = (datetime.now(timezone.utc) - created_at).total_seconds() / 3600

            results.append({
                "symbol": base.get("symbol") or "?",
                "name": base.get("name") or "?",
                "token_address": base.get("address"),
                "pool_address": attrs.get("address"),
                "reserve_usd": float(attrs.get("reserve_in_usd") or 0),
                "volume_24h_usd": float((attrs.get("volume_usd") or {}).get("h24") or 0),
                "price_usd": float(attrs.get("base_token_price_usd") or 0),
                "created_at": created_raw,
                "age_hours": round(age_hours, 2) if age_hours is not None else None,
                "network": network,
            })
    return results


def check_basic_safety(pool: dict) -> list:
    """
    Returns a list of WARNING strings, never a "safe to buy" clearance.

    IMPORTANT, read this before trusting this function for anything:
    this is a coarse liquidity/volume/age filter, NOT honeypot detection,
    NOT contract auditing, NOT insider/bundler-wallet detection. A token
    can pass every check here and still be a honeypot (buyable, not
    sellable) or a rug (liquidity pulled the moment after you buy). This
    function exists to cut obvious noise, not to certify safety. For
    anything closer to real risk scoring, check the token manually on a
    tool that actually does contract-level analysis (e.g. Solana
    Tracker's free risk score) before buying -- this script doesn't
    replace that step.
    """
    warnings = []
    if pool["reserve_usd"] < MIN_LIQUIDITY_USD:
        warnings.append(f"Low liquidity (${pool['reserve_usd']:,.0f} < ${MIN_LIQUIDITY_USD:,.0f} floor)")
    if pool["volume_24h_usd"] < MIN_VOLUME_USD:
        warnings.append(f"Low volume (${pool['volume_24h_usd']:,.0f} < ${MIN_VOLUME_USD:,.0f} floor)")
    if pool["age_hours"] is not None and pool["age_hours"] > MAX_POOL_AGE_HOURS:
        warnings.append(f"Older than scan window ({pool['age_hours']:.1f}h > {MAX_POOL_AGE_HOURS}h)")
    return warnings


HONEYPOT_API_KEY = os.environ.get('HONEYPOT_API_KEY', '')

# Per honeypot.is's own current docs (checked directly, not assumed):
# "API Keys are currently not required - please leave the API key out of
# your requests in order [to succeed]. This will change in the near
# future." So: HONEYPOT_API_KEY is OPTIONAL right now -- the header is
# only attached if one is actually set, never sent empty/blank, since the
# docs are explicit that including it wrong breaks the request today.
# When honeypot.is does require a key later, setting HONEYPOT_API_KEY in
# .env will already work without any code change here.

# honeypot.is / EVM chain IDs -- Solana isn't supported here at all (this
# API works by simulating an EVM buy/sell transaction, which doesn't map
# onto how Solana token risk actually works -- freeze/mint authority, LP
# lock status, etc. are a different check entirely, not built here yet).
HONEYPOT_CHAIN_IDS = {"eth": 1, "bsc": 56, "base": 8453}


def check_honeypot(network: str, token_address: str) -> dict:
    """
    Real honeypot detection via honeypot.is -- actually simulates a
    buy/sell to see if selling gets blocked, unlike the coarse liquidity/
    volume/age filter earlier in this file, which is just noise reduction.

    No API key needed as of this writing (see note above) -- this runs
    for free with zero signup. If honeypot.is starts requiring one later
    and requests start failing, that's the first thing to check.

    IMPORTANT HONESTY NOTE: response parsing was written from partial/
    fragmentary documentation, not a fully confirmed current schema -- it
    defensively checks a few plausible field paths and falls back to
    status='unknown' rather than guessing, but if honeypot.is changes
    their response shape, this could start reporting 'unknown' for
    everything rather than silently misreporting safe/unsafe. If that
    happens, check https://docs.honeypot.is directly and update the
    parsing below -- don't assume 'unknown' always means the check ran
    correctly.
    """
    if network not in HONEYPOT_CHAIN_IDS:
        return {"status": "not_supported", "detail": f"{network} not supported by this honeypot check (EVM-only)"}

    headers = {"X-API-KEY": HONEYPOT_API_KEY} if HONEYPOT_API_KEY else {}

    try:
        resp = requests.get(
            "https://api.honeypot.is/v2/IsHoneypot",
            headers=headers,
            params={"address": token_address, "chainID": HONEYPOT_CHAIN_IDS[network]},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        # Defensive parsing across a couple of plausible shapes rather than
        # trusting one exact path -- see honesty note above.
        is_honeypot = None
        for path in (
            lambda d: d.get("honeypotResult", {}).get("isHoneypot"),
            lambda d: d.get("isHoneypot"),
            lambda d: d.get("summary", {}).get("isHoneypot"),
        ):
            try:
                val = path(data)
                if isinstance(val, bool):
                    is_honeypot = val
                    break
            except Exception:
                continue

        if is_honeypot is None:
            return {"status": "unknown", "detail": "Response received but didn't match expected format -- verify manually"}
        if is_honeypot:
            return {"status": "flagged", "detail": "honeypot.is flagged this as a honeypot (sell likely blocked)"}
        return {"status": "checked_ok", "detail": "honeypot.is found no sell restriction -- NOT a full safety guarantee"}

    except Exception as e:
        return {"status": "unknown", "detail": f"Check failed: {e}"}


def fetch_token_info(network: str, token_address: str) -> dict:
    """
    Fetches a token's website/social links, if the deployer bothered to
    fill them in (many rug/scam tokens don't, which is itself a signal --
    absence of a website isn't proof of anything, but PRESENCE of a real
    one is at least something to actually go read).

    Only ever called for tokens that already passed the coarse liquidity/
    volume/age filter -- a handful per scan, not all ~150+ raw pools --
    to stay comfortably within the free Demo tier's rate limit (30
    calls/minute). Never call this in a tight loop over the full raw
    pool list.
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/onchain/networks/{network}/tokens/{token_address}/info",
            headers=HEADERS, timeout=15,
        )
        resp.raise_for_status()
        attrs = resp.json().get("data", {}).get("attributes", {})
        websites = attrs.get("websites") or []
        return {
            "website": websites[0] if websites else None,
            "twitter_handle": attrs.get("twitter_handle"),
            "description": attrs.get("description"),
        }
    except Exception:
        # Missing info is common and not itself suspicious (plenty of
        # legitimate-but-brand-new tokens haven't filled this in yet) --
        # fail quietly, the dashboard/output just shows what it doesn't have.
        return {"website": None, "twitter_handle": None, "description": None}


def build_review_links(pool: dict) -> dict:
    """Ready-made links for the manual review step -- X, website/whitepaper,
    chart, and on-chain explorer. Nothing here is fetched or automated
    beyond what fetch_token_info() already pulled; these are just URLs
    for YOU to open and check by hand.

    IMPORTANT: twitter_handle and website come from the token's own
    on-chain metadata, which is SELF-REPORTED by whoever deployed the
    token -- nobody verifies it. A scammer can put anything there: a
    stolen legitimate account's handle, an unrelated tweet, garbage.
    Concretely observed in the wild: a token whose "website" field was
    literally someone else's unrelated tweet URL, and whose "X account"
    pointed to a random uninvolved person's post. This is NOT the same
    as a platform-verified account -- treat it as "what the deployer
    claims," exactly as trustworthy as the token's name (i.e. not
    trustworthy on its own at all).
    """
    symbol = pool["symbol"]
    token_addr = pool["token_address"] or ""
    explorer = None
    template = EXPLORER_URL_TEMPLATES.get(pool["network"])
    if template and token_addr:
        explorer = template.format(address=token_addr)

    twitter_handle = pool.get("twitter_handle")
    x_link = f"https://x.com/{twitter_handle}" if twitter_handle else \
        f"https://x.com/search?q=%24{quote(symbol)}%20OR%20{quote(token_addr)}&src=typed_query&f=live"

    # Red flag, not a nice-to-have: if the self-reported "website" is
    # itself just a link to a social media post (x.com/twitter.com/...),
    # that's suspicious on its own -- a real project's website field
    # should be an actual website. Surfaced explicitly rather than
    # displayed as a normal link.
    website = pool.get("website")
    website_is_social_post = bool(website) and any(
        d in website for d in ("x.com/", "twitter.com/")
    ) and "/status/" in (website or "")

    return {
        "x_link": x_link,
        "x_link_is_self_reported": twitter_handle is not None,  # NOT "verified" -- just present in metadata
        "website": website,
        "website_is_social_post": website_is_social_post,
        "dexscreener": f"https://dexscreener.com/{pool['network']}/{pool['pool_address']}" if pool["pool_address"] else None,
        "explorer": explorer,
    }


PAPER_TRADES_FILE = 'data/paper_trades.json'


def load_paper_trades() -> dict:
    if os.path.exists(PAPER_TRADES_FILE):
        with open(PAPER_TRADES_FILE) as f:
            return json.load(f)
    return {"positions": []}


def save_paper_trades(data: dict):
    os.makedirs('data', exist_ok=True)
    with open(PAPER_TRADES_FILE, 'w') as f:
        json.dump(data, f, indent=2)


ACTIVITY_LOG_FILE = 'data/activity_log.jsonl'


def log_event(event_type: str, details: dict):
    """
    Permanent, append-only audit trail of every discrete event in this
    project -- a position opened, closed, or an alert threshold crossed
    -- kept SEPARATE from paper_trades.json's live position state.

    Why a separate file: paper_trades.json represents current state (an
    open position's history array can be long, and a closed position
    could in principle be pruned or reset later for housekeeping). This
    log is never rewritten or trimmed by anything in this project -- it
    only ever gets appended to -- so "what actually happened, when" is
    preserved for analysis (e.g. by backtest_scaled_exit.py or manual
    review) even if the live state file is ever cleaned up.

    Format: JSON Lines (one JSON object per line) so it can grow
    indefinitely without needing to load/rewrite the whole file each
    time, and is trivial to read with pandas/jq/a simple line loop later.
    """
    os.makedirs('data', exist_ok=True)
    entry = {"time": datetime.now(timezone.utc).isoformat(), "event": event_type, **details}
    with open(ACTIVITY_LOG_FILE, 'a') as f:
        f.write(json.dumps(entry) + "\n")


def rank_eligible_candidates(candidates: list, already_tracked: set) -> list:
    """
    Returns ALL eligible candidates (honeypot 'checked_ok', no fake-website
    red flag, not already tracked), sorted best-first by a normalized
    liquidity+volume score (each divided by the max in the eligible set,
    then summed, so one metric being huge doesn't drown out the other).

    This is a RANKING for you to choose from, not an auto-pick and not a
    safety endorsement -- see check_basic_safety() and check_honeypot()
    docstrings, both still fully apply.
    """
    eligible = [
        c for c in candidates
        if c.get("honeypot", {}).get("status") == "checked_ok"
        and c["token_address"] not in already_tracked
        # A passed honeypot check doesn't cancel out other red flags
        # already surfaced elsewhere -- e.g. a fake "website" that's
        # actually just a social media post link (see build_review_links).
        and not c.get("links", {}).get("website_is_social_post")
    ]
    if not eligible:
        return []

    max_liq = max(c["reserve_usd"] for c in eligible) or 1
    max_vol = max(c["volume_24h_usd"] for c in eligible) or 1
    for c in eligible:
        c["_score"] = (c["reserve_usd"] / max_liq) + (c["volume_24h_usd"] / max_vol)

    return sorted(eligible, key=lambda c: c["_score"], reverse=True)


def print_ranked_candidates(ranked: list, top_n=5):
    if not ranked:
        print("No eligible candidates right now (needs honeypot status "
              "'checked_ok', a real website -- not a social post link -- "
              "and not already being tracked).")
        return
    print(f"\nTop {min(top_n, len(ranked))} eligible candidates, ranked by liquidity+volume:")
    print("=" * 78)
    for i, c in enumerate(ranked[:top_n], 1):
        print(f"{i}. [{c['network'].upper()}] {c['symbol']} ({c['name']})  -- score {c['_score']:.2f}")
        print(f"   Liquidity: ${c['reserve_usd']:,.0f}   24h Volume: ${c['volume_24h_usd']:,.0f}   "
              f"Age: {c['age_hours']}h   Price: ${c['price_usd']:.8f}")
        print(f"   Token addr: {c['token_address']}")
    print()


def fetch_current_price(network: str, token_address: str) -> float | None:
    """Current price for one token, for updating an existing paper
    position -- separate from the new_pools scan, so updating doesn't
    require a full rescan."""
    try:
        resp = requests.get(
            f"{BASE_URL}/onchain/simple/networks/{network}/token_price/{token_address}",
            headers=HEADERS, timeout=15,
        )
        resp.raise_for_status()
        prices = resp.json().get("data", {}).get("attributes", {}).get("token_prices", {})
        val = prices.get(token_address)
        return float(val) if val is not None else None
    except Exception as e:
        print(f"  Could not fetch current price for {token_address[:12]}...: {e}")
        return None


def fetch_prices_batch(network: str, token_addresses: list) -> dict:
    """
    Current prices for MULTIPLE tokens on the SAME network in one API
    call -- the endpoint accepts comma-separated addresses (up to ~30).
    Used by the CI price-check script so checking N tracked tokens on one
    chain costs 1 call instead of N, keeping usage well inside the free
    tier's 30-calls/minute limit regardless of how many positions are open.

    Returns {token_address: price_or_None}. A missing/failed address maps
    to None rather than being silently dropped, so callers can tell
    "no price data" apart from "wasn't asked about".
    """
    if not token_addresses:
        return {}
    try:
        joined = ",".join(token_addresses)
        resp = requests.get(
            f"{BASE_URL}/onchain/simple/networks/{network}/token_price/{joined}",
            headers=HEADERS, timeout=15,
        )
        resp.raise_for_status()
        prices = resp.json().get("data", {}).get("attributes", {}).get("token_prices", {})
        return {addr: (float(prices[addr]) if prices.get(addr) is not None else None)
                for addr in token_addresses}
    except Exception as e:
        print(f"  Could not fetch batch prices for {network} ({len(token_addresses)} tokens): {e}")
        return {addr: None for addr in token_addresses}


def open_paper_trade(candidates: list, pick_rank: int = None):
    """Simulated-only: logs a hypothetical entry at the current scan
    price. If pick_rank is given (1-indexed, from the printed ranked
    list), opens that specific candidate; otherwise defaults to #1.
    No wallet, no real funds, nothing bought -- this only ever writes to
    data/paper_trades.json so you can watch how it WOULD have gone."""
    trades = load_paper_trades()
    already_tracked = {p["token_address"] for p in trades["positions"] if p["status"] == "open"}

    ranked = rank_eligible_candidates(candidates, already_tracked)
    print_ranked_candidates(ranked)

    if not ranked:
        return

    idx = (pick_rank - 1) if pick_rank else 0
    if idx < 0 or idx >= len(ranked):
        print(f"--pick {pick_rank} is out of range (only {len(ranked)} eligible candidates). Nothing opened.")
        return
    pick = ranked[idx]

    now = datetime.now(timezone.utc).isoformat()
    position = {
        "symbol": pick["symbol"],
        "name": pick["name"],
        "network": pick["network"],
        "token_address": pick["token_address"],
        "entry_time": now,
        "entry_price": pick["price_usd"],
        "entry_liquidity_usd": pick["reserve_usd"],
        "entry_volume_24h_usd": pick["volume_24h_usd"],
        "status": "open",
        "history": [{"time": now, "price": pick["price_usd"], "pct_change": 0.0}],
    }
    trades["positions"].append(position)
    save_paper_trades(trades)
    log_event("track_opened", {
        "symbol": pick["symbol"], "network": pick["network"], "token_address": pick["token_address"],
        "entry_price": pick["price_usd"], "entry_liquidity_usd": pick["reserve_usd"],
        "entry_volume_24h_usd": pick["volume_24h_usd"], "source": "cli",
    })

    print(f"PAPER TRADE OPENED (simulated only -- nothing bought):")
    print(f"  [{pick['network'].upper()}] {pick['symbol']} ({pick['name']})")
    print(f"  Entry price:  ${pick['price_usd']:.8f}")
    print(f"  Liquidity:    ${pick['reserve_usd']:,.0f}   24h Volume: ${pick['volume_24h_usd']:,.0f}")
    print(f"  Token addr:   {pick['token_address']}")


def update_paper_trades():
    """Re-checks current price for every OPEN simulated position and
    appends a snapshot -- run this manually whenever you want a progress
    check. Doesn't touch the scanner's new_pools scan at all."""
    trades = load_paper_trades()
    open_positions = [p for p in trades["positions"] if p["status"] == "open"]

    if not open_positions:
        print("No open paper trades yet. Run with --paper-trade after a scan to open one.")
        return

    print(f"Updating {len(open_positions)} open paper trade(s)...\n")
    now = datetime.now(timezone.utc).isoformat()

    for pos in open_positions:
        price = fetch_current_price(pos["network"], pos["token_address"])
        if price is None:
            print(f"[{pos['network'].upper()}] {pos['symbol']}: price unavailable this update, skipped.")
            continue

        pct_change = ((price - pos["entry_price"]) / pos["entry_price"]) * 100 if pos["entry_price"] else 0.0
        pos["history"].append({"time": now, "price": price, "pct_change": round(pct_change, 2)})

        entry_dt = datetime.fromisoformat(pos["entry_time"])
        hours_held = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600

        arrow = "\u25b2" if pct_change >= 0 else "\u25bc"
        print(f"[{pos['network'].upper()}] {pos['symbol']} ({pos['name']}) -- held {hours_held:.1f}h")
        print(f"  Entry: ${pos['entry_price']:.8f}  ->  Now: ${price:.8f}   {arrow} {pct_change:+.2f}%")
        print(f"  (Simulated only -- this was never actually bought.)\n")

    save_paper_trades(trades)


def run_scan(pages=2):
    print(f"Scanning {len(NETWORKS)} chain(s) for new pools (last ~{MAX_POOL_AGE_HOURS}h, "
          f"min liquidity ${MIN_LIQUIDITY_USD:,.0f}, min volume ${MIN_VOLUME_USD:,.0f})...")
    print()

    all_pools = []
    for network in NETWORKS:
        print(f"  {network}...", end=" ", flush=True)
        try:
            pools = fetch_new_pools(network, pages=pages)
            all_pools.extend(pools)
            print(f"OK ({len(pools)} pools)")
        except Exception as e:
            # One chain failing (bad network id, rate limit, transient
            # error) shouldn't stop the others from being scanned.
            print(f"FAILED ({e}) -- skipping this chain, continuing with the rest.")
    print()

    candidates = []
    for pool in all_pools:
        warnings = check_basic_safety(pool)
        if warnings:
            continue  # doesn't clear even the coarse noise filter -- skip
        candidates.append(pool)

    candidates.sort(key=lambda p: p["reserve_usd"], reverse=True)

    # Enrich ONLY the shortlist (not all ~150+ raw pools) with website/X
    # handle info -- one extra API call per candidate, paced to stay well
    # within the free Demo tier's 30-calls/minute limit.
    if candidates:
        print(f"Fetching website/social info for {len(candidates)} shortlisted candidates...")
        for c in candidates:
            info = fetch_token_info(c["network"], c["token_address"])
            c.update(info)
            time.sleep(2.1)  # ~28 calls/min, comfortably under the 30/min Demo cap
        print()

        print(f"Running honeypot checks (EVM chains only)...")
        for c in candidates:
            c["honeypot"] = check_honeypot(c["network"], c["token_address"])
            if c["network"] in HONEYPOT_CHAIN_IDS:
                time.sleep(0.5)  # honeypot.is rate limit is more generous, light pacing only
        print()

    for c in candidates:
        c["links"] = build_review_links(c)

    print(f"Scanned {len(all_pools)} pools across {len(NETWORKS)} chain(s), {len(candidates)} passed basic filters.")
    print("=" * 78)
    if not candidates:
        print("Nothing passed the filters this run -- that's a normal, quiet outcome,")
        print("not an error. Try again later or loosen thresholds if this is persistent.")
    for c in candidates:
        hp = c.get("honeypot", {})
        hp_line = {
            "flagged": "  \u26a0\ufe0f  HONEYPOT FLAGGED -- " + hp.get("detail", ""),
            "checked_ok": "  Honeypot check: no sell restriction found (not a full guarantee)",
            "not_supported": "  Honeypot check: not supported for this chain (Solana)",
            "unknown": "  Honeypot check: unknown/unavailable -- " + hp.get("detail", ""),
        }.get(hp.get("status"), "")

        print(f"\n[{c['network'].upper()}] {c['symbol']} ({c['name']})")
        if hp.get("status") == "flagged":
            print(f"  {'!' * 60}")
            print(hp_line)
            print(f"  {'!' * 60}")
        elif hp_line:
            print(hp_line)
        print(f"  Pool age:   {c['age_hours']}h")
        print(f"  Liquidity:  ${c['reserve_usd']:,.0f}")
        print(f"  24h Volume: ${c['volume_24h_usd']:,.0f}")
        print(f"  Price:      ${c['price_usd']:.8f}")
        print(f"  Token addr: {c['token_address']}")
        x_label = "X (self-reported handle -- NOT verified)" if c['links']['x_link_is_self_reported'] else "X (keyword search, no handle found)"
        print(f"  -> {x_label}: {c['links']['x_link']}")
        if c['links'].get('website_is_social_post'):
            print(f"  \u26a0\ufe0f  'Website' field is actually just a social media post, not a real website -- suspicious on its own:")
            print(f"      {c['links']['website']}")
        if c['links']['website'] and not c['links'].get('website_is_social_post'):
            print(f"  -> Website:         {c['links']['website']}")
        if c['links']['dexscreener']:
            print(f"  -> Chart:           {c['links']['dexscreener']}")
        if c['links']['explorer']:
            print(f"  -> Explorer:        {c['links']['explorer']}")

    print()
    print("=" * 78)
    print("REMINDER: nothing here has been bought. This is a discovery shortlist")
    print("only -- passing these coarse filters is NOT a safety guarantee (see")
    print("check_basic_safety()'s docstring). Do your own X + project-site check")
    print("on each of these before deciding anything, same as always.")

    os.makedirs('data', exist_ok=True)
    with open('data/scan_results.json', 'w') as f:
        json.dump({"generated_at": datetime.now().isoformat(), "candidates": candidates}, f, indent=2)

    return candidates


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Read-only new-token discovery scanner. Never buys anything.")
    parser.add_argument('--pages', type=int, default=2, help="Number of result pages to fetch (20 pools/page)")
    parser.add_argument('--rank', action='store_true',
                         help="After scanning, print the top eligible candidates ranked by "
                              "liquidity+volume (no trade opened, just the ranked list)")
    parser.add_argument('--paper-trade', action='store_true',
                         help="After scanning, print the ranked list and open a SIMULATED "
                              "(no real funds) position -- logged to data/paper_trades.json. "
                              "Defaults to #1 unless --pick is given.")
    parser.add_argument('--pick', type=int, default=None,
                         help="Use with --paper-trade: rank number (1, 2, 3...) of the "
                              "candidate to open, from the printed ranked list")
    parser.add_argument('--paper-update', action='store_true',
                         help="Skip the scan entirely; just refresh current price on any open "
                              "simulated paper trades and print progress")
    args = parser.parse_args()

    if args.paper_update:
        update_paper_trades()
    else:
        results = run_scan(pages=args.pages)
        if args.paper_trade:
            open_paper_trade(results, pick_rank=args.pick)
        elif args.rank:
            trades = load_paper_trades()
            already_tracked = {p["token_address"] for p in trades["positions"] if p["status"] == "open"}
            print_ranked_candidates(rank_eligible_candidates(results, already_tracked))
