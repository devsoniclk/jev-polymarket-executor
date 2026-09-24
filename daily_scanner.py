#!/usr/bin/env python3
"""
Daily crypto market scanner for Polymarket Above X markets.
Scans active BTC/ETH Above X markets, computes edge based on distance from threshold.
"""

import json
import re
import ssl
import sys
import math
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError

# ---- CONFIG ----
GAMMA_API = "https://gamma-api.polymarket.com/markets"
COINGECKO_API = "https://api.coingecko.com/api/v3/simple/price"

COIN_MAP = {
    "bitcoin": {"cg_id": "bitcoin", "symbol": "BTC"},
    "btc":     {"cg_id": "bitcoin", "symbol": "BTC"},
    "ethereum": {"cg_id": "ethereum", "symbol": "ETH"},
    "eth":     {"cg_id": "ethereum", "symbol": "ETH"},
}

# ---- HELPERS ----
def make_ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

def fetch_json(url, headers=None, timeout=15):
    ctx = make_ssl_context()
    req = Request(url)
    req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read().decode())
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        print(f"  [WARN] Fetch failed: {e}", file=sys.stderr)
        return None

def safe_float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default

# ---- CORE LOGIC ----
def get_active_above_markets():
    """Fetch active Above X crypto markets from Gamma API."""
    url = f"{GAMMA_API}?active=true&limit=100&order=volume24hr&ascending=false"
    print(f"  Fetching: {url}")
    data = fetch_json(url)
    if not data:
        print("  [WARN] Gamma API returned no data")
        return []

    results = []
    for m in data:
        q = (m.get("question") or "").strip()
        q_lower = q.lower()

        if "above" not in q_lower:
            continue

        coin_found = None
        for keyword, info in COIN_MAP.items():
            if keyword in q_lower:
                coin_found = info
                break
        if not coin_found:
            continue

        # Extract threshold price
        price_match = re.search(r'\$?\s*([\d,]+(?:\.\d+)?)', q)
        if not price_match:
            continue
        threshold = float(price_match.group(1).replace(",", ""))
        if threshold < 100:  # Skip non-price matches
            continue

        end_date = m.get("endDate") or m.get("end_date_iso") or ""

        outcome_prices = m.get("outcomePrices")
        yes_price = None
        if outcome_prices:
            try:
                if isinstance(outcome_prices, str):
                    outcome_prices = json.loads(outcome_prices)
                yes_price = float(outcome_prices[0])
            except (IndexError, ValueError, TypeError):
                pass

        volume = safe_float(m.get("volume"))
        volume_24h = safe_float(m.get("volume24hr"))

        results.append({
            "question": q,
            "coin": coin_found["symbol"],
            "cg_id": coin_found["cg_id"],
            "threshold": threshold,
            "end_date": end_date,
            "yes_price": yes_price,
            "volume": volume,
            "volume_24h": volume_24h,
            "condition_id": m.get("conditionId", ""),
            "market_slug": m.get("slug", ""),
        })

    return results

def get_current_prices(coin_ids):
    """Get current USD prices from CoinGecko."""
    ids = ",".join(set(coin_ids))
    url = f"{COINGECKO_API}?ids={ids}&vs_currencies=usd"
    data = fetch_json(url)
    if not data:
        return {}
    return {cid: info.get("usd", 0) for cid, info in data.items()}

def compute_edge(market, current_price):
    """Compute distance and edge for an Above X market."""
    threshold = market["threshold"]
    if threshold <= 0:
        return None

    distance_pct = ((current_price - threshold) / threshold) * 100

    # Estimate true probability using sigmoid
    # Key insight: when distance >2% above threshold at 11:00 ET, P(above) >99%
    k = 2.5
    true_prob = 1.0 / (1.0 + math.exp(-k * distance_pct / 2.0))
    true_prob = max(0.01, min(0.99, true_prob))

    yes_price = market["yes_price"]
    edge = None
    if yes_price is not None and yes_price > 0:
        edge = true_prob - yes_price

    return {
        "distance_pct": round(distance_pct, 2),
        "true_prob_estimate": round(true_prob, 4),
        "market_yes_price": yes_price,
        "edge": round(edge, 4) if edge is not None else None,
    }

def format_table(opportunities, current_btc):
    """Format results as a readable table."""
    lines = []
    lines.append("=" * 80)
    lines.append(f"  BTC PRICE: ${current_btc:,.2f}")
    lines.append("=" * 80)
    lines.append(f"{'#':<3} {'Market':<50} {'Thresh':>10} {'Dist%':>8} {'TrueP':>8} {'Mkt':>6} {'Edge':>8}")
    lines.append("-" * 80)
    for i, opp in enumerate(opportunities[:15], 1):
        q = opp["question"][:48]
        thresh = f"${opp['threshold']:,.0f}"
        dist = f"{opp['distance_pct']:+.1f}%"
        true_p = f"{opp['true_prob_estimate']:.0%}"
        mkt = f"{opp['yes_price']:.0%}" if opp['yes_price'] is not None else "N/A"
        edge = f"{opp['edge']*100:+.1f}%" if opp['edge'] is not None else "N/A"
        lines.append(f"{i:<3} {q:<50} {thresh:>10} {dist:>8} {true_p:>8} {mkt:>6} {edge:>8}")
    return "\n".join(lines)

# ---- MOCK DATA (for testing when API is unreachable) ----
MOCK_MARKETS = [
    {
        "question": "Bitcoin above $83,000 on September 24?",
        "coin": "BTC", "cg_id": "bitcoin", "threshold": 83000,
        "end_date": "2026-09-24T16:00:00Z", "yes_price": 0.92,
        "volume": 561200, "volume_24h": 185000,
        "condition_id": "", "market_slug": "bitcoin-above-83k-sep24",
    },
    {
        "question": "Bitcoin above $84,000 on September 24?",
        "coin": "BTC", "cg_id": "bitcoin", "threshold": 84000,
        "end_date": "2026-09-24T16:00:00Z", "yes_price": 0.55,
        "volume": 420000, "volume_24h": 145000,
        "condition_id": "", "market_slug": "bitcoin-above-84k-sep24",
    },
    {
        "question": "Bitcoin above $85,000 on September 24?",
        "coin": "BTC", "cg_id": "bitcoin", "threshold": 85000,
        "end_date": "2026-09-24T16:00:00Z", "yes_price": 0.18,
        "volume": 310000, "volume_24h": 98000,
        "condition_id": "", "market_slug": "bitcoin-above-85k-sep24",
    },
    {
        "question": "Bitcoin above $82,000 on September 25?",
        "coin": "BTC", "cg_id": "bitcoin", "threshold": 82000,
        "end_date": "2026-09-25T16:00:00Z", "yes_price": 0.88,
        "volume": 280000, "volume_24h": 92000,
        "condition_id": "", "market_slug": "bitcoin-above-82k-sep25",
    },
    {
        "question": "Bitcoin above $86,000 on September 25?",
        "coin": "BTC", "cg_id": "bitcoin", "threshold": 86000,
        "end_date": "2026-09-25T16:00:00Z", "yes_price": 0.12,
        "volume": 195000, "volume_24h": 65000,
        "condition_id": "", "market_slug": "bitcoin-above-86k-sep25",
    },
    {
        "question": "Ethereum above $2,600 on September 24?",
        "coin": "ETH", "cg_id": "ethereum", "threshold": 2600,
        "end_date": "2026-09-24T16:00:00Z", "yes_price": 0.75,
        "volume": 150000, "volume_24h": 48000,
        "condition_id": "", "market_slug": "ethereum-above-2600-sep24",
    },
    {
        "question": "Ethereum above $2,700 on September 24?",
        "coin": "ETH", "cg_id": "ethereum", "threshold": 2700,
        "end_date": "2026-09-24T16:00:00Z", "yes_price": 0.30,
        "volume": 120000, "volume_24h": 38000,
        "condition_id": "", "market_slug": "ethereum-above-2700-sep24",
    },
]

# ---- MAIN ----
def main():
    print("=" * 80)
    print("  POLYMARKET DAILY CRYPTO SCANNER")
    print(f"  Scan time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 80)

    # Step 1: Fetch markets
    print("\n[1] Fetching active Above X markets from Gamma API...")
    markets = get_active_above_markets()
    use_mock = False

    if not markets:
        print("  [INFO] No live data from API. Using mock markets for demonstration.")
        markets = MOCK_MARKETS
        use_mock = True

    print(f"  Found {len(markets)} Above X crypto markets {'(mock)' if use_mock else ''}")

    # Step 2: Get current prices
    print("\n[2] Fetching current prices from CoinGecko...")
    coin_ids = [m["cg_id"] for m in markets]
    prices = get_current_prices(coin_ids)

    if not prices:
        print("  [INFO] CoinGecko unavailable. Using estimated prices.")
        # Use latest known prices from web search (Sep 24, 2026)
        prices = {"bitcoin": 84092, "ethereum": 2672}

    print(f"  Prices: BTC=${prices.get('bitcoin', 0):,.2f}  ETH=${prices.get('ethereum', 0):,.2f}")

    # Step 3: Analyze each market
    print("\n[3] Computing distance and edge for each market...")
    opportunities = []
    for m in markets:
        price = prices.get(m["cg_id"], 0)
        if price <= 0:
            continue

        analysis = compute_edge(m, price)
        if analysis is None:
            continue

        m["current_price"] = price
        m.update(analysis)
        opportunities.append(m)

    # Step 4: Sort by absolute edge (biggest mispricing first)
    opportunities.sort(key=lambda x: abs(x.get("edge") or 0), reverse=True)

    # Step 5: Display results
    btc_price = prices.get("bitcoin", 0)
    print("\n" + format_table(opportunities, btc_price))

    # Step 6: Highlight top opportunities
    print("\n" + "=" * 80)
    print("  TOP OPPORTUNITIES (sorted by edge)")
    print("=" * 80)
    for i, opp in enumerate(opportunities[:5], 1):
        edge_pct = f"{opp['edge']*100:+.1f}%" if opp.get('edge') is not None else "N/A"
        print(f"\n  {i}. {opp['question']}")
        print(f"     {opp['coin']} @ ${opp['current_price']:,.0f} vs threshold ${opp['threshold']:,.0f}")
        print(f"     Distance: {opp['distance_pct']:+.2f}% | True P: {opp['true_prob_estimate']:.1%} | "
              f"Market: {opp['yes_price']:.1%} | Edge: {edge_pct}")
        print(f"     Volume 24h: ${opp['volume_24h']:,.0f}")

    # Summary result
    result = {
        "markets_found": len(opportunities),
        "test_result": (
            f"Scanned {len(markets)} markets ({'mock' if use_mock else 'live'}), "
            f"found {len(opportunities)} with edge. "
            f"BTC: ${btc_price:,.0f}. "
            f"Top edge: {opportunities[0]['edge']*100:+.1f}% on "
            f"{opportunities[0]['question'][:50]}..."
            if opportunities else
            f"Scanned {len(markets)} markets, no opportunities found."
        ),
    }

    print(f"\n[RESULT] {json.dumps(result)}")
    return result


if __name__ == "__main__":
    result = main()