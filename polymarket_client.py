"""
Polymarket CLOB client - market discovery, orderbook, orders, WebSocket feeds.
"""
import json
import time
import logging
import threading
from typing import Dict, List, Optional, Any, Callable
import requests
import urllib3

# Suppress SSL warnings for environments with older LibreSSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)

# Use verify=False on macOS with LibreSSL < 1.1.1
import ssl
_SSL_OK = hasattr(ssl, "OPENSSL_VERSION") and "LibreSSL" not in ssl.OPENSSL_VERSION
_VERIFY = _SSL_OK

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
WS_CLOB = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WS_RTDS = "wss://ws-live-data.polymarket.com"


def btc_5m_slug() -> str:
    """Generate market slug for BTC 5-minute up/down markets."""
    return "btc-updown-5m-" + str(int(time.time()) // 300 * 300)


def btc_15m_slug() -> str:
    return "btc-updown-15m-" + str(int(time.time()) // 900 * 900)


def eth_5m_slug() -> str:
    return "eth-updown-5m-" + str(int(time.time()) // 300 * 300)


class PolymarketClient:
    """Full CLOB client for Polymarket."""

    def __init__(self, api_key: str = "", api_secret: str = "", passphrase: str = ""):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/json"
        self.session.verify = _VERIFY
        self._ws_threads: List[threading.Thread] = []

    # ── Gamma API (market discovery) ──────────────────────────────
    def search_markets(self, query: str = "", limit: int = 50,
                       active: bool = True, closed: bool = False) -> List[Dict]:
        """Search markets via Gamma API."""
        params: Dict[str, Any] = {"limit": limit, "active": str(active).lower()}
        if query:
            params["query"] = query
        if closed:
            params["closed"] = "true"
        try:
            r = self.session.get(f"{GAMMA_API}/markets", params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("Gamma search failed: %s", e)
            return []

    def get_market(self, condition_id: str) -> Optional[Dict]:
        """Get single market by condition_id."""
        try:
            r = self.session.get(f"{GAMMA_API}/markets/{condition_id}", timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("Gamma get_market failed: %s", e)
            return None

    def get_markets_by_slug(self, slug: str) -> List[Dict]:
        """Search markets by slug-like text."""
        return self.search_markets(query=slug)

    # ── CLOB API (orderbook, orders) ──────────────────────────────
    def get_orderbook(self, token_id: str) -> Dict:
        """Fetch orderbook for a token."""
        try:
            r = self.session.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("CLOB orderbook failed: %s", e)
            return {"bids": [], "asks": []}

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get midpoint price for a token."""
        try:
            r = self.session.get(f"{CLOB_API}/midpoint", params={"token_id": token_id}, timeout=10)
            r.raise_for_status()
            data = r.json()
            mid = data.get("mid") or data.get("midpoint")
            return float(mid) if mid else None
        except Exception as e:
            log.warning("CLOB midpoint failed: %s", e)
            return None

    def get_price(self, token_id: str, side: str = "buy") -> Optional[float]:
        """Get best price for buy/sell side."""
        try:
            r = self.session.get(f"{CLOB_API}/price",
                                 params={"token_id": token_id, "side": side}, timeout=10)
            r.raise_for_status()
            data = r.json()
            return float(data.get("price", 0))
        except Exception as e:
            log.warning("CLOB price failed: %s", e)
            return None

    def place_order(self, token_id: str, side: str, price: float, size: float,
                    post_only: bool = True) -> Dict:
        """
        Place an order on CLOB.
        POST https://clob.polymarket.com/order
        """
        order = {
            "tokenID": token_id,
            "price": str(price),
            "size": str(size),
            "side": side,  # "BUY" or "SELL"
            "type": "GTC",
            "feeRateBps": "0",
            "nonce": str(int(time.time() * 1000)),
        }
        if post_only:
            order["postOnly"] = True

        headers = {}
        if self.api_key:
            headers["POLY-API-KEY"] = self.api_key
            headers["POLY-PASSPHRASE"] = self.passphrase
            # Signature would go here with api_secret

        try:
            r = self.session.post(f"{CLOB_API}/order", json=order,
                                  headers=headers, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("CLOB place_order failed: %s", e)
            return {"error": str(e)}

    def cancel_order(self, order_id: str) -> Dict:
        """Cancel an open order."""
        headers = {}
        if self.api_key:
            headers["POLY-API-KEY"] = self.api_key
            headers["POLY-PASSPHRASE"] = self.passphrase
        try:
            r = self.session.delete(f"{CLOB_API}/order/{order_id}",
                                    headers=headers, timeout=10)
            r.raise_for_status()
            return {"status": "cancelled", "order_id": order_id}
        except Exception as e:
            log.warning("Cancel order failed: %s", e)
            return {"error": str(e)}

    def get_open_orders(self) -> List[Dict]:
        """List open orders."""
        headers = {}
        if self.api_key:
            headers["POLY-API-KEY"] = self.api_key
            headers["POLY-PASSPHRASE"] = self.passphrase
        try:
            r = self.session.get(f"{CLOB_API}/orders", headers=headers, timeout=10)
            r.raise_for_status()
            return r.json() if isinstance(r.json(), list) else []
        except Exception as e:
            log.warning("Get open orders failed: %s", e)
            return []

    def redeem_position(self, condition_id: str) -> Dict:
        """Redeem winning position."""
        headers = {}
        if self.api_key:
            headers["POLY-API-KEY"] = self.api_key
            headers["POLY-PASSPHRASE"] = self.passphrase
        try:
            r = self.session.post(f"{CLOB_API}/redeem", json={"conditionId": condition_id},
                                  headers=headers, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("Redeem failed: %s", e)
            return {"error": str(e)}

    # ── WebSocket helpers ─────────────────────────────────────────
    def start_book_ws(self, token_ids: List[str],
                      on_update: Callable[[Dict], None]) -> threading.Thread:
        """
        Start WebSocket for real-time orderbook updates.
        Requires `websocket-client` package.
        """
        def _run():
            try:
                import websocket
                url = WS_CLOB
                def on_message(ws, message):
                    try:
                        data = json.loads(message)
                        on_update(data)
                    except Exception:
                        pass

                def on_open(ws):
                    for tid in token_ids:
                        ws.send(json.dumps({
                            "type": "subscribe",
                            "channel": "book",
                            "token_id": tid
                        }))

                ws = websocket.WebSocketApp(url, on_message=on_message, on_open=on_open)
                ws.run_forever()
            except ImportError:
                log.warning("websocket-client not installed; WS disabled")
            except Exception as e:
                log.warning("WS error: %s", e)

        t = threading.Thread(target=_run, daemon=True, name="polymarket-book-ws")
        t.start()
        self._ws_threads.append(t)
        return t

    def start_rtds_ws(self, symbols: List[str],
                      on_price: Callable[[Dict], None]) -> threading.Thread:
        """
        Start WebSocket for Chainlink real-time price feed from RTDS.
        wss://ws-live-data.polymarket.com
        """
        def _run():
            try:
                import websocket
                def on_message(ws, message):
                    try:
                        data = json.loads(message)
                        on_price(data)
                    except Exception:
                        pass

                def on_open(ws):
                    for sym in symbols:
                        ws.send(json.dumps({
                            "type": "subscribe",
                            "symbol": sym
                        }))

                ws = websocket.WebSocketApp(WS_RTDS, on_message=on_message, on_open=on_open)
                ws.run_forever()
            except ImportError:
                log.warning("websocket-client not installed; RTDS WS disabled")
            except Exception as e:
                log.warning("RTDS WS error: %s", e)

        t = threading.Thread(target=_run, daemon=True, name="polymarket-rtds-ws")
        t.start()
        self._ws_threads.append(t)
        return t

    # ── Helpers ───────────────────────────────────────────────────
    def get_event_outcomes(self, event_slug: str) -> List[Dict]:
        """Get outcomes for an event by slug."""
        markets = self.search_markets(query=event_slug, limit=10)
        outcomes = []
        for m in markets:
            tokens = m.get("tokens", [])
            for t in tokens:
                outcomes.append({
                    "token_id": t.get("token_id"),
                    "outcome": t.get("outcome"),
                    "price": t.get("price"),
                    "market_question": m.get("question"),
                })
        return outcomes

    def get_market_implied_prob(self, token_id: str) -> Optional[float]:
        """Get market-implied probability (midpoint price)."""
        return self.get_midpoint(token_id)