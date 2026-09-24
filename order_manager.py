"""
Order lifecycle manager: place maker orders, track positions,
cancel stale orders, auto-redeem winners, kill switch.
"""
import json
import os
import time
import logging
import threading
from typing import Dict, List, Optional, Any
from datetime import datetime

log = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
POSITIONS_FILE = os.path.join(DATA_DIR, "positions.json")
ORDERS_FILE = os.path.join(DATA_DIR, "orders.json")
TRADES_LOG = os.path.join(DATA_DIR, "trades.jsonl")
KILL_FILE = os.path.join(DATA_DIR, "KILL")


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _load_json(path: str, default: Any = None) -> Any:
    if default is None:
        default = {}
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: str, data: Any):
    _ensure_data_dir()
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _append_jsonl(path: str, entry: Dict):
    _ensure_data_dir()
    with open(path, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


class OrderManager:
    """Order lifecycle management with maker-only orders."""

    def __init__(self, client, paper_mode: bool = False):
        self.client = client
        self.paper_mode = paper_mode
        self._lock = threading.Lock()
        self._running = False
        self._cancel_thread: Optional[threading.Thread] = None

    # ── Kill switch ───────────────────────────────────────────────
    def is_killed(self) -> bool:
        """Check if kill switch is active."""
        return os.path.exists(KILL_FILE)

    def activate_kill(self):
        """Activate kill switch - touch data/KILL."""
        _ensure_data_dir()
        with open(KILL_FILE, "w") as f:
            f.write(datetime.utcnow().isoformat())
        log.warning("KILL SWITCH ACTIVATED")

    def deactivate_kill(self):
        """Remove kill switch."""
        if os.path.exists(KILL_FILE):
            os.remove(KILL_FILE)
            log.info("Kill switch deactivated")

    # ── Position tracking ─────────────────────────────────────────
    def get_positions(self) -> Dict[str, Dict]:
        """Get all tracked positions."""
        return _load_json(POSITIONS_FILE, {})

    def update_position(self, token_id: str, side: str, size: float,
                        price: float, market_question: str = ""):
        """Update position after a fill."""
        with self._lock:
            positions = self.get_positions()
            key = f"{token_id}:{side}"
            if key in positions:
                pos = positions[key]
                old_size = pos.get("size", 0)
                old_avg = pos.get("avg_price", 0)
                new_size = old_size + size
                if new_size > 0:
                    pos["avg_price"] = (old_avg * old_size + price * size) / new_size
                pos["size"] = new_size
                pos["last_update"] = datetime.utcnow().isoformat()
            else:
                positions[key] = {
                    "token_id": token_id,
                    "side": side,
                    "size": size,
                    "avg_price": price,
                    "market_question": market_question,
                    "opened_at": datetime.utcnow().isoformat(),
                    "last_update": datetime.utcnow().isoformat(),
                }
            _save_json(POSITIONS_FILE, positions)

            # Log trade
            _append_jsonl(TRADES_LOG, {
                "type": "fill",
                "token_id": token_id,
                "side": side,
                "size": size,
                "price": price,
                "timestamp": datetime.utcnow().isoformat(),
            })

    def close_position(self, token_id: str, side: str, exit_price: float) -> Dict:
        """Close a position and record P&L."""
        with self._lock:
            positions = self.get_positions()
            key = f"{token_id}:{side}"
            if key not in positions:
                return {"error": "position not found"}

            pos = positions.pop(key)
            avg_price = pos.get("avg_price", 0)
            size = pos.get("size", 0)

            if side == "buy-yes":
                pnl = (exit_price - avg_price) * size
            else:
                pnl = (avg_price - exit_price) * size

            _save_json(POSITIONS_FILE, positions)
            _append_jsonl(TRADES_LOG, {
                "type": "close",
                "token_id": token_id,
                "side": side,
                "size": size,
                "avg_price": avg_price,
                "exit_price": exit_price,
                "pnl": pnl,
                "timestamp": datetime.utcnow().isoformat(),
            })

            return {"pnl": pnl, "size": size, "avg_price": avg_price}

    # ── Order placement ───────────────────────────────────────────
    def place_maker_order(self, token_id: str, side: str, price: float,
                          size: float, market_question: str = "") -> Dict:
        """
        Place a maker (post_only) order.
        """
        if self.is_killed():
            return {"error": "kill switch active"}

        if self.paper_mode:
            result = {
                "order_id": f"paper-{int(time.time()*1000)}",
                "status": "paper_fill",
                "token_id": token_id,
                "side": side,
                "price": price,
                "size": size,
            }
            self.update_position(token_id, side, size, price, market_question)
            return result

        result = self.client.place_order(
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            post_only=True,
        )

        if "error" not in result:
            order_id = result.get("order_id") or result.get("id", "")
            # Track order
            orders = _load_json(ORDERS_FILE, [])
            if not isinstance(orders, list):
                orders = []
            orders.append({
                "order_id": order_id,
                "token_id": token_id,
                "side": side,
                "price": price,
                "size": size,
                "placed_at": time.time(),
                "market_question": market_question,
            })
            _save_json(ORDERS_FILE, orders)

        return result

    # ── Stale order cancellation ──────────────────────────────────
    def cancel_stale_orders(self, max_age_seconds: float = 18.0):
        """Cancel orders older than max_age_seconds."""
        if self.paper_mode:
            return

        orders = _load_json(ORDERS_FILE, [])
        if not isinstance(orders, list):
            return

        now = time.time()
        remaining = []
        cancelled = 0

        for order in orders:
            age = now - order.get("placed_at", now)
            if age > max_age_seconds:
                result = self.client.cancel_order(order["order_id"])
                if "error" not in result:
                    cancelled += 1
                    log.info("Cancelled stale order %s (age %.0fs)", order["order_id"], age)
                else:
                    remaining.append(order)
            else:
                remaining.append(order)

        _save_json(ORDERS_FILE, remaining)
        if cancelled:
            log.info("Cancelled %d stale orders", cancelled)

    def _cancel_loop(self, interval: float = 17.0, max_age: float = 18.0):
        """Background loop to cancel stale orders every 15-20s."""
        while self._running:
            try:
                self.cancel_stale_orders(max_age)
            except Exception as e:
                log.warning("Cancel loop error: %s", e)
            time.sleep(interval)

    def start_cancel_loop(self, interval: float = 17.0, max_age: float = 18.0):
        """Start background stale order cancellation."""
        if self._running:
            return
        self._running = True
        self._cancel_thread = threading.Thread(
            target=self._cancel_loop, args=(interval, max_age),
            daemon=True, name="order-cancel-loop"
        )
        self._cancel_thread.start()
        log.info("Started cancel loop (interval=%.0fs, max_age=%.0fs)", interval, max_age)

    def stop_cancel_loop(self):
        """Stop background cancel loop."""
        self._running = False
        if self._cancel_thread:
            self._cancel_thread.join(timeout=5)

    # ── Auto-redeem winners ───────────────────────────────────────
    def redeem_winners(self) -> List[Dict]:
        """Check positions and redeem any winners."""
        if self.paper_mode:
            return []

        positions = self.get_positions()
        redeemed = []
        for key, pos in positions.items():
            # Check if market resolved
            # In paper mode this is simulated; live would check via API
            try:
                result = self.client.redeem_position(pos.get("condition_id", ""))
                if "error" not in result:
                    redeemed.append({"key": key, "result": result})
                    log.info("Redeemed position %s", key)
            except Exception as e:
                log.debug("Redeem check for %s: %s", key, e)

        return redeemed

    def get_open_orders(self) -> List[Dict]:
        """Get current open orders."""
        if self.paper_mode:
            return _load_json(ORDERS_FILE, [])
        return self.client.get_open_orders()

    def get_trade_history(self, limit: int = 50) -> List[Dict]:
        """Get recent trade history from JSONL log."""
        if not os.path.exists(TRADES_LOG):
            return []
        trades = []
        try:
            with open(TRADES_LOG, "r") as f:
                for line in f:
                    if line.strip():
                        trades.append(json.loads(line.strip()))
        except Exception:
            pass
        return trades[-limit:]

    def get_exposure(self) -> float:
        """Calculate total open exposure in dollars."""
        positions = self.get_positions()
        total = 0.0
        for pos in positions.values():
            total += abs(pos.get("size", 0) * pos.get("avg_price", 0))
        return total

    def get_daily_pnl(self) -> float:
        """Calculate today's P&L from trades log."""
        if not os.path.exists(TRADES_LOG):
            return 0.0
        today = datetime.utcnow().strftime("%Y-%m-%d")
        daily_pnl = 0.0
        try:
            with open(TRADES_LOG, "r") as f:
                for line in f:
                    if line.strip():
                        trade = json.loads(line.strip())
                        ts = trade.get("timestamp", "")
                        if ts.startswith(today) and trade.get("type") == "close":
                            daily_pnl += trade.get("pnl", 0)
        except Exception:
            pass
        return daily_pnl