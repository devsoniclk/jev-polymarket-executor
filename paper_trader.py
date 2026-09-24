"""
Paper trader: simulate CLOB execution, track positions, P&L,
win rate, Brier score, JSONL logging.
"""
import json
import os
import time
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime
import math

log = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PAPER_TRADES = os.path.join(DATA_DIR, "paper_trades.jsonl")
PAPER_POSITIONS = os.path.join(DATA_DIR, "paper_positions.json")
PAPER_STATS = os.path.join(DATA_DIR, "paper_stats.json")


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


class PaperTrader:
    """Simulate CLOB execution with full tracking."""

    def __init__(self, initial_balance: float = 100.0):
        self.initial_balance = initial_balance
        self._load_state()

    def _load_state(self):
        state = _load_json(PAPER_STATS, {
            "balance": self.initial_balance,
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "brier_sum": 0.0,
            "brier_count": 0,
            "started_at": datetime.utcnow().isoformat(),
        })
        self.balance = state.get("balance", self.initial_balance)
        self.total_trades = state.get("total_trades", 0)
        self.wins = state.get("wins", 0)
        self.losses = state.get("losses", 0)
        self.total_pnl = state.get("total_pnl", 0.0)
        self.brier_sum = state.get("brier_sum", 0.0)
        self.brier_count = state.get("brier_count", 0)

    def _save_state(self):
        _save_json(PAPER_STATS, {
            "balance": self.balance,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "total_pnl": self.total_pnl,
            "brier_sum": self.brier_sum,
            "brier_count": self.brier_count,
            "started_at": _load_json(PAPER_STATS, {}).get("started_at",
                                                           datetime.utcnow().isoformat()),
        })

    # ── Simulated execution ───────────────────────────────────────
    def simulate_buy(self, token_id: str, side: str, price: float,
                     size: float, market_question: str = "",
                     fair_prob: float = 0.5) -> Dict:
        """
        Simulate a buy order (instant fill at given price).
        side: 'buy-yes' or 'buy-no'
        """
        cost = price * size
        if cost > self.balance:
            return {"error": f"Insufficient balance: ${self.balance:.2f} < ${cost:.2f}"}

        self.balance -= cost
        order_id = f"paper-{int(time.time() * 1000)}"

        # Record position
        positions = _load_json(PAPER_POSITIONS, {})
        key = f"{token_id}:{side}"
        positions[key] = {
            "token_id": token_id,
            "side": side,
            "size": size,
            "avg_price": price,
            "cost": cost,
            "market_question": market_question,
            "fair_prob": fair_prob,
            "opened_at": datetime.utcnow().isoformat(),
            "order_id": order_id,
        }
        _save_json(PAPER_POSITIONS, positions)

        # Log trade
        _append_jsonl(PAPER_TRADES, {
            "type": "open",
            "order_id": order_id,
            "token_id": token_id,
            "side": side,
            "price": price,
            "size": size,
            "cost": cost,
            "fair_prob": fair_prob,
            "balance_after": self.balance,
            "timestamp": datetime.utcnow().isoformat(),
        })

        log.info("Paper BUY %s @ $%.3f x %.1f (cost=$%.2f, balance=$%.2f)",
                 side, price, size, cost, self.balance)

        return {"order_id": order_id, "status": "filled", "cost": cost,
                "balance": self.balance}

    def simulate_resolve(self, token_id: str, side: str,
                         resolved_yes: bool) -> Dict:
        """
        Resolve a position.
        Returns P&L and updates stats.
        """
        positions = _load_json(PAPER_POSITIONS, {})
        key = f"{token_id}:{side}"
        if key not in positions:
            return {"error": "Position not found"}

        pos = positions.pop(key)
        size = pos["size"]
        cost = pos["cost"]
        fair_prob = pos.get("fair_prob", 0.5)

        # Determine if we won
        if side == "buy-yes":
            won = resolved_yes
        else:
            won = not resolved_yes

        if won:
            payout = size * 1.0  # $1 per share
            pnl = payout - cost
        else:
            payout = 0.0
            pnl = -cost

        self.balance += payout
        self.total_trades += 1
        self.total_pnl += pnl
        if won:
            self.wins += 1
        else:
            self.losses += 1

        # Brier score: (forecast - outcome)^2
        outcome = 1.0 if resolved_yes else 0.0
        if side == "buy-yes":
            forecast = fair_prob
        else:
            forecast = 1.0 - fair_prob
        brier = (forecast - outcome) ** 2
        self.brier_sum += brier
        self.brier_count += 1

        _save_json(PAPER_POSITIONS, positions)
        self._save_state()

        # Log resolution
        _append_jsonl(PAPER_TRADES, {
            "type": "resolve",
            "token_id": token_id,
            "side": side,
            "won": won,
            "pnl": pnl,
            "payout": payout,
            "cost": cost,
            "brier": brier,
            "balance_after": self.balance,
            "timestamp": datetime.utcnow().isoformat(),
        })

        log.info("Paper RESOLVE %s: %s (pnl=$%.2f, balance=$%.2f)",
                 side, "WIN" if won else "LOSS", pnl, self.balance)

        return {"won": won, "pnl": pnl, "payout": payout, "brier": brier,
                "balance": self.balance}

    # ── Stats ─────────────────────────────────────────────────────
    def get_positions(self) -> Dict:
        """Get open paper positions."""
        return _load_json(PAPER_POSITIONS, {})

    def get_stats(self) -> Dict[str, Any]:
        """Get paper trading statistics."""
        win_rate = self.wins / self.total_trades if self.total_trades > 0 else 0.0
        brier_score = self.brier_sum / self.brier_count if self.brier_count > 0 else 0.0
        roi = (self.balance - self.initial_balance) / self.initial_balance * 100

        return {
            "balance": round(self.balance, 2),
            "initial_balance": self.initial_balance,
            "total_pnl": round(self.total_pnl, 2),
            "roi_pct": round(roi, 2),
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(win_rate, 4),
            "brier_score": round(brier_score, 4),
            "open_positions": len(self.get_positions()),
        }

    def get_trade_history(self, limit: int = 50) -> List[Dict]:
        """Get recent paper trades."""
        if not os.path.exists(PAPER_TRADES):
            return []
        trades = []
        try:
            with open(PAPER_TRADES, "r") as f:
                for line in f:
                    if line.strip():
                        trades.append(json.loads(line.strip()))
        except Exception:
            pass
        return trades[-limit:]

    def reset(self):
        """Reset paper trading state."""
        self.balance = self.initial_balance
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        self.brier_sum = 0.0
        self.brier_count = 0
        self._save_state()
        _save_json(PAPER_POSITIONS, {})
        # Truncate trades log
        if os.path.exists(PAPER_TRADES):
            open(PAPER_TRADES, "w").close()
        log.info("Paper trader reset (balance=$%.2f)", self.balance)