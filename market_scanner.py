"""
Market scanner: find tradeable Polymarket markets.
BTC/ETH Above X, 5m/15m Up/Down, political events.
Filters: volume > $1K, liquidity > $500, not sports.
"""
import time
import logging
from typing import Dict, List, Optional

from polymarket_client import PolymarketClient, btc_5m_slug, btc_15m_slug, eth_5m_slug

log = logging.getLogger(__name__)

SPORTS_KEYWORDS = [
    "nba", "nfl", "mlb", "nhl", "soccer", "football", "basketball",
    "baseball", "hockey", "tennis", "golf", "boxing", "ufc", "mma",
    "cricket", "rugby", "formula 1", "f1", "racing", "esports",
    "championship", "playoffs", "super bowl", "world cup", "grand slam",
]


class MarketScanner:
    """Scan and filter Polymarket markets for trading opportunities."""

    def __init__(self, client: Optional[PolymarketClient] = None,
                 min_volume: float = 1000.0,
                 min_liquidity: float = 500.0):
        self.client = client or PolymarketClient()
        self.min_volume = min_volume
        self.min_liquidity = min_liquidity

    def _is_sports(self, question: str) -> bool:
        """Check if market is sports-related."""
        q_lower = question.lower()
        return any(kw in q_lower for kw in SPORTS_KEYWORDS)

    def _passes_filters(self, market: Dict) -> bool:
        """Check if market passes volume/liquidity/sports filters."""
        question = market.get("question", "")
        if self._is_sports(question):
            return False

        volume = float(market.get("volume", 0) or 0)
        if volume < self.min_volume:
            return False

        liquidity = float(market.get("liquidity", 0) or 0)
        if liquidity < self.min_liquidity:
            return False

        # Must be active
        if market.get("closed", False):
            return False

        return True

    def scan_btc_markets(self) -> List[Dict]:
        """Scan for BTC Above/Below X daily markets."""
        markets = []
        queries = ["Bitcoin above", "BTC above", "Bitcoin price", "BTC price"]
        for q in queries:
            results = self.client.search_markets(query=q, limit=20)
            for m in results:
                if self._passes_filters(m):
                    m["_type"] = "btc-daily"
                    markets.append(m)
        log.info("Found %d BTC daily markets", len(markets))
        return markets

    def scan_eth_markets(self) -> List[Dict]:
        """Scan for ETH Above/Below X daily markets."""
        markets = []
        queries = ["Ethereum above", "ETH above", "Ethereum price", "ETH price"]
        for q in queries:
            results = self.client.search_markets(query=q, limit=20)
            for m in results:
                if self._passes_filters(m):
                    m["_type"] = "eth-daily"
                    markets.append(m)
        log.info("Found %d ETH daily markets", len(markets))
        return markets

    def scan_5m_markets(self) -> List[Dict]:
        """Scan for 5-minute up/down markets."""
        markets = []
        slugs = [btc_5m_slug(), eth_5m_slug()]
        for slug in slugs:
            # Try exact slug and broader search
            results = self.client.get_markets_by_slug(slug)
            if not results:
                results = self.client.search_markets(query="5 minute", limit=10)
            for m in results:
                if self._passes_filters(m):
                    m["_type"] = "5m-updown"
                    markets.append(m)
        log.info("Found %d 5-minute markets", len(markets))
        return markets

    def scan_15m_markets(self) -> List[Dict]:
        """Scan for 15-minute up/down markets."""
        markets = []
        slug = btc_15m_slug()
        results = self.client.get_markets_by_slug(slug)
        if not results:
            results = self.client.search_markets(query="15 minute", limit=10)
        for m in results:
            if self._passes_filters(m):
                m["_type"] = "15m-updown"
                markets.append(m)
        log.info("Found %d 15-minute markets", len(markets))
        return markets

    def scan_political_markets(self) -> List[Dict]:
        """Scan for political event markets."""
        markets = []
        queries = ["president", "election", "congress", "senate", "governor",
                    "political", "trump", "biden", "republican", "democrat"]
        seen_ids = set()
        for q in queries:
            results = self.client.search_markets(query=q, limit=15)
            for m in results:
                cid = m.get("condition_id") or m.get("id", "")
                if cid in seen_ids:
                    continue
                seen_ids.add(cid)
                if self._passes_filters(m):
                    m["_type"] = "political"
                    markets.append(m)
        log.info("Found %d political markets", len(markets))
        return markets

    def scan_crypto_markets(self) -> List[Dict]:
        """Scan all crypto-related markets."""
        markets = []
        queries = ["crypto", "bitcoin", "ethereum", "solana", "XRP",
                    "crypto market cap", "bitcoin dominance"]
        seen_ids = set()
        for q in queries:
            results = self.client.search_markets(query=q, limit=15)
            for m in results:
                cid = m.get("condition_id") or m.get("id", "")
                if cid in seen_ids:
                    continue
                seen_ids.add(cid)
                if self._passes_filters(m):
                    m.setdefault("_type", "crypto")
                    markets.append(m)
        log.info("Found %d crypto markets", len(markets))
        return markets

    def scan_all(self) -> List[Dict]:
        """Scan all market categories."""
        all_markets = []
        all_markets.extend(self.scan_btc_markets())
        all_markets.extend(self.scan_eth_markets())
        all_markets.extend(self.scan_5m_markets())
        all_markets.extend(self.scan_15m_markets())
        all_markets.extend(self.scan_political_markets())
        all_markets.extend(self.scan_crypto_markets())

        # Dedupe by condition_id
        seen = set()
        unique = []
        for m in all_markets:
            cid = m.get("condition_id") or m.get("id", "")
            if cid and cid not in seen:
                seen.add(cid)
                unique.append(m)

        log.info("Total unique tradeable markets: %d", len(unique))
        return unique

    def format_market(self, market: Dict) -> str:
        """Format a market for display."""
        q = market.get("question", "?")
        vol = float(market.get("volume", 0) or 0)
        liq = float(market.get("liquidity", 0) or 0)
        mtype = market.get("_type", "?")

        tokens = market.get("tokens", [])
        prices = []
        for t in tokens:
            outcome = t.get("outcome", "?")
            price = t.get("price", "?")
            prices.append(f"{outcome}={price}")

        price_str = ", ".join(prices) if prices else "no prices"

        return f"[{mtype}] {q}\n  vol=${vol:,.0f} liq=${liq:,.0f} | {price_str}"