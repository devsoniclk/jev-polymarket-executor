"""
Risk manager: position limits, Kelly sizing, circuit breaker, expiry guard.
"""
import os
import json
import time
import logging
from typing import Dict, Optional, Any
from datetime import datetime

log = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
RISK_STATE_FILE = os.path.join(DATA_DIR, "risk_state.json")


def _load_risk_state() -> Dict:
    if not os.path.exists(RISK_STATE_FILE):
        return {"consecutive_losses": 0, "daily_pnl": 0.0, "trade_count": 0,
                "last_reset": datetime.utcnow().strftime("%Y-%m-%d")}
    try:
        with open(RISK_STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"consecutive_losses": 0, "daily_pnl": 0.0, "trade_count": 0,
                "last_reset": datetime.utcnow().strftime("%Y-%m-%d")}


def _save_risk_state(state: Dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(RISK_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


class RiskManager:
    """Risk management with hard limits and Kelly sizing."""

    def __init__(self, max_per_trade: float = 10.0,
                 max_exposure: float = 100.0,
                 daily_loss_limit: float = 50.0,
                 kelly_fraction: float = 0.25,
                 circuit_breaker_losses: int = 3,
                 expiry_buffer_seconds: float = 75.0):
        self.max_per_trade = max_per_trade
        self.max_exposure = max_exposure
        self.daily_loss_limit = daily_loss_limit
        self.kelly_fraction = kelly_fraction
        self.circuit_breaker_losses = circuit_breaker_losses
        self.expiry_buffer_seconds = expiry_buffer_seconds

    def _reset_daily_if_needed(self):
        """Reset daily counters if it's a new day."""
        state = _load_risk_state()
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if state.get("last_reset") != today:
            state["daily_pnl"] = 0.0
            state["trade_count"] = 0
            state["last_reset"] = today
            _save_risk_state(state)
        return state

    def can_trade(self, current_exposure: float = 0.0) -> Dict[str, Any]:
        """
        Check if trading is allowed.
        Returns {allowed: bool, reason: str}
        """
        state = self._reset_daily_if_needed()

        # Check circuit breaker
        if state.get("consecutive_losses", 0) >= self.circuit_breaker_losses:
            return {"allowed": False,
                    "reason": f"Circuit breaker: {state['consecutive_losses']} consecutive losses"}

        # Check daily loss limit
        if state.get("daily_pnl", 0) <= -self.daily_loss_limit:
            return {"allowed": False,
                    "reason": f"Daily loss limit reached: ${state['daily_pnl']:.2f}"}

        # Check total exposure
        if current_exposure >= self.max_exposure:
            return {"allowed": False,
                    "reason": f"Max exposure reached: ${current_exposure:.2f} >= ${self.max_exposure}"}

        return {"allowed": True, "reason": "OK"}

    def size_trade(self, fair_prob: float, market_prob: float,
                   edge: float, confidence: float,
                   current_exposure: float = 0.0) -> Dict[str, Any]:
        """
        Calculate position size using fractional Kelly criterion.
        Returns {size: float, method: str, details: str}
        """
        # Can we trade at all?
        check = self.can_trade(current_exposure)
        if not check["allowed"]:
            return {"size": 0.0, "method": "blocked", "details": check["reason"]}

        # Kelly criterion: f* = (bp - q) / b
        # where b = odds, p = fair_prob, q = 1-p
        if edge <= 0 or fair_prob <= 0 or fair_prob >= 1:
            return {"size": 0.0, "method": "none", "details": "No edge or invalid prob"}

        # For binary market: if we buy YES at market_prob, payout is 1/market_prob
        # b = (1 / market_prob) - 1  (net odds)
        if market_prob <= 0 or market_prob >= 1:
            return {"size": 0.0, "method": "none", "details": "Invalid market prob"}

        b = (1.0 / market_prob) - 1.0
        p = fair_prob
        q = 1.0 - p
        kelly_full = (b * p - q) / b

        if kelly_full <= 0:
            return {"size": 0.0, "method": "kelly", "details": f"Negative Kelly: {kelly_full:.4f}"}

        # Apply fractional Kelly
        kelly_adj = kelly_full * self.kelly_fraction

        # Scale by confidence
        kelly_adj *= confidence

        # Convert to dollar size (fraction of bankroll)
        # Here we use max_exposure as bankroll proxy
        bankroll = self.max_exposure - current_exposure
        dollar_size = kelly_adj * bankroll

        # Cap at max per trade
        dollar_size = min(dollar_size, self.max_per_trade)
        dollar_size = min(dollar_size, self.max_exposure - current_exposure)
        dollar_size = max(0.0, round(dollar_size, 2))

        return {
            "size": dollar_size,
            "method": "kelly",
            "details": (f"Kelly full={kelly_full:.4f}, adj={kelly_adj:.4f}, "
                        f"b={b:.2f}, p={p:.3f}, bankroll=${bankroll:.2f}"),
            "kelly_full": round(kelly_full, 4),
            "kelly_adj": round(kelly_adj, 4),
        }

    def check_expiry(self, end_date_str: str) -> Dict[str, Any]:
        """
        Check if market is too close to expiry.
        Returns {safe: bool, seconds_remaining: float}
        """
        if not end_date_str:
            return {"safe": True, "seconds_remaining": float("inf")}

        try:
            # Parse ISO format
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00").replace("+00:00", ""))
            now = datetime.utcnow()
            remaining = (end_dt - now).total_seconds()
            safe = remaining > self.expiry_buffer_seconds
            return {"safe": safe, "seconds_remaining": remaining}
        except Exception:
            return {"safe": True, "seconds_remaining": float("inf")}

    def record_trade_result(self, pnl: float):
        """Record a trade result for circuit breaker tracking."""
        state = self._reset_daily_if_needed()
        state["daily_pnl"] = state.get("daily_pnl", 0.0) + pnl
        state["trade_count"] = state.get("trade_count", 0) + 1

        if pnl < 0:
            state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1
        else:
            state["consecutive_losses"] = 0  # reset on win

        _save_risk_state(state)

        # Log if circuit breaker triggered
        if state["consecutive_losses"] >= self.circuit_breaker_losses:
            log.warning("CIRCUIT BREAKER: %d consecutive losses!", state["consecutive_losses"])

    def reset_circuit_breaker(self):
        """Manually reset circuit breaker."""
        state = _load_risk_state()
        state["consecutive_losses"] = 0
        _save_risk_state(state)
        log.info("Circuit breaker reset")

    def get_status(self) -> Dict[str, Any]:
        """Get current risk status."""
        state = self._reset_daily_if_needed()
        return {
            "consecutive_losses": state.get("consecutive_losses", 0),
            "daily_pnl": state.get("daily_pnl", 0.0),
            "trade_count": state.get("trade_count", 0),
            "circuit_breaker_active": state.get("consecutive_losses", 0) >= self.circuit_breaker_losses,
            "daily_loss_remaining": self.daily_loss_limit + state.get("daily_pnl", 0.0),
            "max_per_trade": self.max_per_trade,
            "max_exposure": self.max_exposure,
            "daily_loss_limit": self.daily_loss_limit,
            "kelly_fraction": self.kelly_fraction,
            "expiry_buffer_seconds": self.expiry_buffer_seconds,
        }