"""
Capital-Preserving Risk Manager
================================
Core principle: NEVER lose the initial capital. Trade only with profits after breakeven.

Protocol:
1. PHASE 1 (Seed): Trade tiny until cumulative profits >= initial capital
   - Max risk per trade: 2% of initial capital
   - Max daily loss: 5% of initial capital
   - Max concurrent positions: 2
   - Stop trading if down 10% from initial
   
2. PHASE 2 (Protected): After breakeven, risk only profits
   - Max risk per trade: 5% of current profits
   - Max daily loss: 20% of profits
   - Max concurrent positions: 3
   - Initial capital is LOCKED — never risk it again
   
3. PHASE 3 (Scaling): Compound profits with decreasing risk
   - Max risk per trade: 3% of total balance (but never touch initial capital)
   - Max daily loss: 10% of profits
   - Max concurrent positions: 5
   - Withdraw initial capital to separate wallet

4. CIRCUIT BREAKERS:
   - 3 consecutive losses → pause 1 hour
   - 5 consecutive losses → pause 4 hours
   - Down 15% from peak → stop trading for the day
   - Down 25% from peak → stop trading, require manual restart
   - Kill switch: touch data/KILL
"""

import json
import os
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, List


class CapitalPreservingRiskManager:
    """Risk manager that prioritizes capital preservation above all else."""

    def __init__(
        self,
        initial_capital: float = 10.0,
        config_file: str = "data/risk_state.json",
    ):
        self.initial_capital = initial_capital
        self.config_file = config_file
        self._load_state()

    def _load_state(self):
        """Load persistent risk state from disk."""
        if os.path.exists(self.config_file):
            with open(self.config_file) as f:
                state = json.load(f)
            self.current_balance = state.get("current_balance", self.initial_capital)
            self.peak_balance = state.get("peak_balance", self.current_balance)
            self.total_profit = state.get("total_profit", 0.0)
            self.total_trades = state.get("total_trades", 0)
            self.wins = state.get("wins", 0)
            self.losses = state.get("losses", 0)
            self.consecutive_losses = state.get("consecutive_losses", 0)
            self.daily_pnl = state.get("daily_pnl", 0.0)
            self.daily_trades = state.get("daily_trades", 0)
            self.last_reset_date = state.get("last_reset_date", "")
            self.open_positions = state.get("open_positions", [])
            self.trade_history = state.get("trade_history", [])
            self.paused_until = state.get("paused_until", 0)
            self.hard_stop = state.get("hard_stop", False)
            self.initial_capital_withdrawn = state.get("initial_capital_withdrawn", False)
        else:
            self.current_balance = self.initial_capital
            self.peak_balance = self.initial_capital
            self.total_profit = 0.0
            self.total_trades = 0
            self.wins = 0
            self.losses = 0
            self.consecutive_losses = 0
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self.last_reset_date = ""
            self.open_positions = []
            self.trade_history = []
            self.paused_until = 0
            self.hard_stop = False
            self.initial_capital_withdrawn = False

    def _save_state(self):
        """Persist risk state to disk."""
        os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
        state = {
            "current_balance": self.current_balance,
            "peak_balance": self.peak_balance,
            "total_profit": self.total_profit,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "consecutive_losses": self.consecutive_losses,
            "daily_pnl": self.daily_pnl,
            "daily_trades": self.daily_trades,
            "last_reset_date": self.last_reset_date,
            "open_positions": self.open_positions,
            "trade_history": self.trade_history[-100:],  # keep last100
            "paused_until": self.paused_until,
            "hard_stop": self.hard_stop,
            "initial_capital_withdrawn": self.initial_capital_withdrawn,
            "saved_at": datetime.now().isoformat(),
        }
        with open(self.config_file, "w") as f:
            json.dump(state, f, indent=2)

    # ──────────────────────────────────────────────
    # PHASE DETECTION
    # ──────────────────────────────────────────────

    def get_phase(self) -> str:
        """Determine current risk phase based on profit status."""
        if self.hard_stop:
            return "STOPPED"
        if self.total_profit <= 0:
            return "SEED"  # Haven't broken even yet
        if self.total_profit < self.initial_capital:
            return "PROTECTED"  # Profitable but haven't doubled
        return "SCALING"  # Doubled initial capital

    def get_profits_only(self) -> float:
        """Returns only the profit portion (never touches initial capital)."""
        return max(0, self.current_balance - self.initial_capital)

    def get_risk_capital(self) -> float:
        """Returns the capital available for risk (profits only after breakeven)."""
        phase = self.get_phase()
        if phase == "SEED":
            # In seed phase, risk a small portion of initial capital
            return self.initial_capital * 0.10  # max10% of initial at risk
        elif phase == "PROTECTED":
            # Only risk profits
            return self.get_profits_only()
        elif phase == "SCALING":
            # Risk profits with compounding
            return self.get_profits_only()
        return 0.0  # STOPPED

    # ──────────────────────────────────────────────
    # TRADE SIZE CALCULATION
    # ──────────────────────────────────────────────

    def calculate_position_size(self, edge: float, probability: float, price: float) -> float:
        """Calculate optimal position size using fractional Kelly with capital preservation."""
        # Daily reset check
        self._check_daily_reset()

        # Pre-checks
        if not self._can_trade():
            return 0.0

        phase = self.get_phase()
        risk_capital = self.get_risk_capital()

        if risk_capital <= 0:
            return 0.0

        # Phase-specific limits
        if phase == "SEED":
            max_per_trade = self.initial_capital * 0.02  # 2% of initial
            max_daily_loss = self.initial_capital * 0.05  # 5% of initial
            max_concurrent = 2
        elif phase == "PROTECTED":
            max_per_trade = risk_capital * 0.05  # 5% of profits
            max_daily_loss = risk_capital * 0.20  # 20% of profits
            max_concurrent = 3
        elif phase == "SCALING":
            max_per_trade = risk_capital * 0.03  # 3% of profits
            max_daily_loss = risk_capital * 0.10  # 10% of profits
            max_concurrent = 5
        else:
            return 0.0

        # Check daily loss limit
        if self.daily_pnl <= -max_daily_loss:
            return 0.0

        # Check concurrent positions
        if len(self.open_positions) >= max_concurrent:
            return 0.0

        # Kelly criterion (fractional)
        if edge <= 0 or probability <= 0 or probability >= 1:
            return 0.0

        # Kelly: f* = (bp - q) / b where b = (1-price)/price
        b = (1 - price) / price if price > 0 and price < 1 else 0
        if b <= 0:
            return 0.0

        full_kelly = (b * probability - (1 - probability)) / b
        fractional_kelly = max(0, full_kelly * 0.25)  # 25% Kelly

        # Calculate size
        kelly_size = risk_capital * fractional_kelly
        size = min(kelly_size, max_per_trade)

        # Minimum trade size (scaled to capital)
        min_trade = max(0.50, self.initial_capital * 0.01)  # $0.50 or1% of capital
        if size < min_trade:
            return 0.0

        # Ensure we don't exceed available balance
        size = min(size, self.current_balance * 0.5)  # never use more than50% of balance

        return round(size, 2)

    # ──────────────────────────────────────────────
    # TRADE APPROVAL
    # ──────────────────────────────────────────────

    def approve_trade(
        self,
        asset: str,
        side: str,
        size_usd: float,
        price: float,
        edge: float,
        probability: float,
        reason: str = "",
    ) -> Dict:
        """
        Approve or reject a trade. Returns approval dict.
        
        Returns:
            {
                "approved": bool,
                "size_usd": float,
                "reason": str,
                "phase": str,
                "risk_capital": float,
                "checks": dict of check_name -> passed
            }
        """
        checks = {}

        # Kill switch
        if os.path.exists("data/KILL"):
            return {"approved": False, "size_usd": 0, "reason": "KILL SWITCH ACTIVE", "checks": {"kill_switch": False}}

        # Hard stop
        if self.hard_stop:
            return {"approved": False, "size_usd": 0, "reason": "HARD STOP — manual restart required", "checks": {"hard_stop": False}}

        # Pause check
        if time.time() < self.paused_until:
            remaining = int(self.paused_until - time.time())
            return {"approved": False, "size_usd": 0, "reason": f"PAUSED — {remaining}s remaining", "checks": {"paused": False}}

        # Daily reset
        self._check_daily_reset()

        phase = self.get_phase()
        risk_capital = self.get_risk_capital()

        # Check1: Minimum edge
        min_edge = 0.05 if phase == "SEED" else 0.08
        checks["min_edge"] = edge >= min_edge

        # Check2: Positive probability
        checks["positive_probability"] = 0 < probability < 1

        # Check3: Valid price
        checks["valid_price"] = 0.01 <= price <= 0.99

        # Check4: Not too many consecutive losses
        max_consecutive = 3 if phase == "SEED" else 5
        checks["consecutive_losses_ok"] = self.consecutive_losses < max_consecutive

        # Check5: Daily loss limit
        if phase == "SEED":
            daily_limit = self.initial_capital * 0.05
        elif phase == "PROTECTED":
            daily_limit = risk_capital * 0.20
        else:
            daily_limit = risk_capital * 0.10
        checks["daily_loss_ok"] = self.daily_pnl > -daily_limit

        # Check6: Drawdown from peak
        drawdown = (self.peak_balance - self.current_balance) / self.peak_balance if self.peak_balance > 0 else 0
        max_drawdown = 0.15  #15% max drawdown
        checks["drawdown_ok"] = drawdown < max_drawdown

        # Check7: Has risk capital
        checks["has_risk_capital"] = risk_capital > 0

        # Check8: Balance check
        checks["balance_ok"] = self.current_balance > self.initial_capital * 0.5

        # Check9: Concurrent positions
        max_concurrent = {"SEED": 2, "PROTECTED": 3, "SCALING": 5}.get(phase, 0)
        checks["positions_ok"] = len(self.open_positions) < max_concurrent

        # All checks must pass
        all_pass = all(checks.values())

        # Calculate size
        if all_pass:
            size = self.calculate_position_size(edge, probability, price)
            if size <= 0:
                all_pass = False
                checks["size_positive"] = False
        else:
            size = 0

        # Build reason
        failed = [k for k, v in checks.items() if not v]
        if all_pass:
            reason_str = f"APPROVED [{phase}] — {reason}"
        else:
            reason_str = f"REJECTED [{phase}] — failed: {', '.join(failed)}"

        return {
            "approved": all_pass,
            "size_usd": size,
            "reason": reason_str,
            "phase": phase,
            "risk_capital": risk_capital,
            "checks": checks,
            "consecutive_losses": self.consecutive_losses,
            "daily_pnl": self.daily_pnl,
            "drawdown_pct": round(drawdown * 100, 2),
        }

    # ──────────────────────────────────────────────
    # TRADE RECORDING
    # ──────────────────────────────────────────────

    def record_open(
        self,
        market_id: str,
        asset: str,
        side: str,
        size_usd: float,
        entry_price: float,
        edge: float,
        reason: str = "",
    ):
        """Record an opened position."""
        position = {
            "market_id": market_id,
            "asset": asset,
            "side": side,
            "size_usd": size_usd,
            "entry_price": entry_price,
            "edge": edge,
            "reason": reason,
            "opened_at": time.time(),
            "opened_at_iso": datetime.now().isoformat(),
        }
        self.open_positions.append(position)
        self.current_balance -= size_usd
        self._save_state()

    def record_close(self, market_id: str, exit_price: float, resolution: str = "win") -> Dict:
        """Record a closed position and update all risk metrics."""
        # Find the position
        pos = None
        for p in self.open_positions:
            if p["market_id"] == market_id:
                pos = p
                break

        if not pos:
            return {"error": "Position not found"}

        # Calculate P&L
        entry = pos["entry_price"]
        size = pos["size_usd"]

        if resolution == "win":
            pnl = size * (1 / entry - 1)  # profit from buying at entry, winning at $1
            self.wins += 1
            self.consecutive_losses = 0
        elif resolution == "loss":
            pnl = -size  # lost entire position
            self.losses += 1
            self.consecutive_losses += 1
        else:
            pnl = 0

        # Update balance
        self.current_balance += size + pnl
        self.total_profit = self.current_balance - self.initial_capital
        self.peak_balance = max(self.peak_balance, self.current_balance)
        self.daily_pnl += pnl
        self.total_trades += 1
        self.daily_trades += 1

        # Record in history
        trade = {
            **pos,
            "exit_price": exit_price,
            "resolution": resolution,
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl / size * 100, 2) if size > 0 else 0,
            "balance_after": round(self.current_balance, 4),
            "closed_at": time.time(),
            "closed_at_iso": datetime.now().isoformat(),
        }
        self.trade_history.append(trade)

        # Remove from open positions
        self.open_positions = [p for p in self.open_positions if p["market_id"] != market_id]

        # Circuit breaker: pause after consecutive losses
        if self.consecutive_losses >= 3:
            self.paused_until = time.time() + 3600  # pause1 hour
        if self.consecutive_losses >= 5:
            self.paused_until = time.time() + 14400  # pause4 hours

        # Hard stop: down25% from peak
        drawdown = (self.peak_balance - self.current_balance) / self.peak_balance if self.peak_balance > 0 else 0
        if drawdown >= 0.25:
            self.hard_stop = True

        # Capital protection: if balance drops below initial, go back to SEED phase
        if self.current_balance < self.initial_capital:
            self.total_profit = self.current_balance - self.initial_capital

        self._save_state()

        return {
            "market_id": market_id,
            "resolution": resolution,
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl / size * 100, 2) if size > 0 else 0,
            "balance": round(self.current_balance, 4),
            "total_profit": round(self.total_profit, 4),
            "phase": self.get_phase(),
            "consecutive_losses": self.consecutive_losses,
            "paused_until": self.paused_until,
            "hard_stop": self.hard_stop,
        }

    # ──────────────────────────────────────────────
    # CAPITAL WITHDRAWAL
    # ──────────────────────────────────────────────

    def withdraw_initial_capital(self) -> Dict:
        """Withdraw initial capital after doubling. Locks it forever."""
        if self.current_balance < self.initial_capital * 2:
            return {
                "success": False,
                "reason": f"Need ${self.initial_capital * 2:.2f} to withdraw. Currently at ${self.current_balance:.2f}",
            }

        if self.initial_capital_withdrawn:
            return {"success": False, "reason": "Already withdrawn"}

        self.initial_capital_withdrawn = True
        self.current_balance -= self.initial_capital
        self._save_state()

        return {
            "success": True,
            "withdrawn": self.initial_capital,
            "remaining_balance": self.current_balance,
            "message": f"Withdrew ${self.initial_capital:.2f} initial capital. Now trading with ${self.current_balance:.2f} pure profit.",
        }

    # ──────────────────────────────────────────────
    # STATUS & REPORTING
    # ──────────────────────────────────────────────

    def get_status(self) -> Dict:
        """Get comprehensive risk status."""
        phase = self.get_phase()
        risk_capital = self.get_risk_capital()
        drawdown = (self.peak_balance - self.current_balance) / self.peak_balance if self.peak_balance > 0 else 0

        return {
            "phase": phase,
            "initial_capital": self.initial_capital,
            "current_balance": round(self.current_balance, 4),
            "peak_balance": round(self.peak_balance, 4),
            "total_profit": round(self.total_profit, 4),
            "profit_only": round(self.get_profits_only(), 4),
            "risk_capital": round(risk_capital, 4),
            "drawdown_pct": round(drawdown * 100, 2),
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": f"{self.wins/self.total_trades*100:.1f}%" if self.total_trades > 0 else "N/A",
            "consecutive_losses": self.consecutive_losses,
            "daily_pnl": round(self.daily_pnl, 4),
            "daily_trades": self.daily_trades,
            "open_positions": len(self.open_positions),
            "paused_until": self.paused_until,
            "hard_stop": self.hard_stop,
            "initial_capital_withdrawn": self.initial_capital_withdrawn,
        }

    def get_rules(self) -> str:
        """Get human-readable current risk rules."""
        phase = self.get_phase()
        status = self.get_status()

        if phase == "SEED":
            return f"""
PHASE: SEED (protecting initial ${self.initial_capital})
  Max per trade:    ${self.initial_capital * 0.02:.2f} (2% of initial)
  Max daily loss:   ${self.initial_capital * 0.05:.2f} (5% of initial)
  Max positions:    2
  Min edge:         5%
  Circuit breaker:  3 consecutive losses → pause1h
  Hard stop:        Down25% from peak → manual restart
  Goal:             Reach ${self.initial_capital * 2:.2f} to unlock PROTECTED phase
"""
        elif phase == "PROTECTED":
            return f"""
PHASE: PROTECTED (initial capital preserved)
  Risk capital:     ${status['risk_capital']:.2f} (profits only)
  Max per trade:    ${status['risk_capital'] * 0.05:.2f} (5% of profits)
  Max daily loss:   ${status['risk_capital'] * 0.20:.2f} (20% of profits)
  Max positions:    3
  Min edge:         8%
  Circuit breaker:  5 consecutive losses → pause4h
  Goal:             Reach ${self.initial_capital * 3:.2f} to withdraw initial capital
"""
        elif phase == "SCALING":
            return f"""
PHASE: SCALING (compounding pure profit)
  Risk capital:     ${status['risk_capital']:.2f} (profits only)
  Max per trade:    ${status['risk_capital'] * 0.03:.2f} (3% of profits)
  Max daily loss:   ${status['risk_capital'] * 0.10:.2f} (10% of profits)
  Max positions:    5
  Min edge:         8%
  Initial withdrawn: {status['initial_capital_withdrawn']}
"""
        return f"PHASE: STOPPED — hard stop triggered. Manual restart required."

    def _can_trade(self) -> bool:
        """Check if trading is allowed."""
        if self.hard_stop:
            return False
        if os.path.exists("data/KILL"):
            return False
        if time.time() < self.paused_until:
            return False
        return True

    def _check_daily_reset(self):
        """Reset daily counters at midnight UTC."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if self.last_reset_date != today:
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self.last_reset_date = today
            self._save_state()

    def force_restart(self):
        """Manual restart after hard stop."""
        self.hard_stop = False
        self.consecutive_losses = 0
        self.paused_until = 0
        self._save_state()

    def reset(self):
        """Full reset to initial state."""
        self.current_balance = self.initial_capital
        self.peak_balance = self.initial_capital
        self.total_profit = 0.0
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.consecutive_losses = 0
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.open_positions = []
        self.trade_history = []
        self.paused_until = 0
        self.hard_stop = False
        self.initial_capital_withdrawn = False
        self._save_state()