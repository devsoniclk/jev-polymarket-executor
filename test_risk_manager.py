#!/usr/bin/env python3
"""Test the capital-preserving risk manager with $10 initial capital."""
import os, sys, json, time
sys.path.insert(0, '.')
from capital_risk_manager import CapitalPreservingRiskManager

# Clean slate
if os.path.exists("data/risk_state.json"):
    os.remove("data/risk_state.json")

rm = CapitalPreservingRiskManager(initial_capital=10.0, config_file="data/risk_state.json")

print("="*65)
print("  CAPITAL-PRESERVING RISK MANAGER TEST")
print("  Initial Capital: $10.00")
print("="*65)

# ── PHASE1: SEED ──
print(f"\n{'─'*65}")
print("  PHASE 1: SEED (before breakeven)")
print(f"{'─'*65}")
print(rm.get_rules())

# Test1: Small trade in seed phase
print("  Test1: Small trade (edge=0.10, prob=0.70, price=0.60)")
approval = rm.approve_trade("btc-up", "yes", 1.0, 0.60, 0.10, 0.70, "test trade")
print(f"    Approved: {approval['approved']}")
print(f"    Size: ${approval['size_usd']:.2f}")
print(f"    Phase: {approval['phase']}")
print(f"    Risk Capital: ${approval['risk_capital']:.2f}")

if approval["approved"]:
    rm.record_open("test-1", "BTC", "yes", approval["size_usd"], 0.60, 0.10, "test")
    print(f"    Balance after open: ${rm.current_balance:.2f}")

    # Simulate a win
    result = rm.record_close("test-1", 1.0, "win")
    print(f"    Win! P&L: ${result['pnl']:.2f}")
    print(f"    Balance: ${result['balance']:.2f}")
    print(f"    Total Profit: ${result['total_profit']:.2f}")

# Test2: Edge too small
print(f"\n  Test2: Low edge (edge=0.03)")
approval = rm.approve_trade("test", "yes", 1.0, 0.60, 0.03, 0.70, "low edge")
print(f"    Approved: {approval['approved']} — {approval['reason']}")

# Test3: Consecutive losses → circuit breaker
print(f"\n  Test3:3 consecutive losses → circuit breaker")
for i in range(3):
    approval = rm.approve_trade(f"loss-{i}", "yes", 0.5, 0.60, 0.10, 0.70, f"loss test {i}")
    if approval["approved"]:
        rm.record_open(f"loss-{i}", "BTC", "yes", approval["size_usd"], 0.60, 0.10)
        result = rm.record_close(f"loss-{i}", 0.0, "loss")
        print(f"    Loss{i+1}: consecutive={result['consecutive_losses']}, paused={result['paused_until'] > time.time()}")

# Reset pause for testing
rm.paused_until = 0
rm.consecutive_losses = 0
rm._save_state()

# ── Simulate winning streak to reach PROTECTED phase ──
print(f"\n{'─'*65}")
print("  Simulating wins to reach PROTECTED phase...")
print(f"{'─'*65}")

for i in range(15):
    approval = rm.approve_trade(f"win-{i}", "yes", 0.5, 0.60, 0.12, 0.75, "winning streak")
    if approval["approved"]:
        rm.record_open(f"win-{i}", "BTC", "yes", approval["size_usd"], 0.60, 0.12)
        rm.record_close(f"win-{i}", 1.0, "win")
    elif rm.get_phase() == "SEED" and rm.current_balance < rm.initial_capital * 1.5:
        # Simulate smaller wins for seed phase
        rm.current_balance += 0.30
        rm.total_profit = rm.current_balance - rm.initial_capital
        rm.wins += 1
        rm.total_trades += 1
        rm._save_state()

print(f"  Balance: ${rm.current_balance:.2f}")
print(f"  Total Profit: ${rm.total_profit:.2f}")
print(f"  Phase: {rm.get_phase()}")
print(f"  Win Rate: {rm.wins}/{rm.total_trades}")

# ── PHASE2: PROTECTED ──
if rm.get_phase() in ("PROTECTED", "SCALING"):
    print(f"\n{'─'*65}")
    print("  PHASE2: PROTECTED (only risking profits)")
    print(f"{'─'*65}")
    print(rm.get_rules())

    approval = rm.approve_trade("protected-test", "yes", 1.0, 0.60, 0.10, 0.70, "protected phase")
    print(f"  Test: Approved={approval['approved']}, Size=${approval['size_usd']:.2f}")
    print(f"  Risk Capital (profits only): ${approval['risk_capital']:.2f}")

# ── CAPITAL WITHDRAWAL TEST ──
print(f"\n{'─'*65}")
print("  CAPITAL WITHDRAWAL TEST")
print(f"{'─'*65}")

# Simulate reaching2x
rm.current_balance = rm.initial_capital * 2.5
rm.total_profit = rm.current_balance - rm.initial_capital
rm._save_state()

withdrawal = rm.withdraw_initial_capital()
print(f"  Success: {withdrawal['success']}")
if withdrawal['success']:
    print(f"  Withdrawn: ${withdrawal['withdrawn']:.2f}")
    print(f"  Remaining: ${withdrawal['remaining_balance']:.2f}")
    print(f"  Message: {withdrawal['message']}")

# ── HARD STOP TEST ──
print(f"\n{'─'*65}")
print("  HARD STOP TEST (25% drawdown)")
print(f"{'─'*65}")

rm2 = CapitalPreservingRiskManager(initial_capital=10.0, config_file="data/risk_test2.json")
rm2.current_balance = 7.50  #25% drawdown
rm2.peak_balance = 10.00
rm2._save_state()

approval = rm2.approve_trade("hard-stop-test", "yes", 1.0, 0.60, 0.10, 0.70)
print(f"  Approved: {approval['approved']}")
print(f"  Reason: {approval['reason']}")

rm2.force_restart()
approval = rm2.approve_trade("restart-test", "yes", 1.0, 0.60, 0.10, 0.70)
print(f"  After restart: Approved={approval['approved']}")

# Cleanup
os.remove("data/risk_test2.json") if os.path.exists("data/risk_test2.json") else None

# ── FINAL STATUS ──
print(f"\n{'='*65}")
print("  FINAL STATUS")
print(f"{'='*65}")
status = rm.get_status()
for k, v in status.items():
    print(f"  {k}: {v}")

print(f"\n{'='*65}")
print("  TRADE HISTORY")
print(f"{'='*65}")
for t in rm.trade_history[-5:]:
    emoji = "+" if t["pnl"] > 0 else ""
    print(f"  {t['asset']} {t['side']} ${t['size_usd']:.2f} → {t['resolution']} P&L={emoji}${t['pnl']:.2f} balance=${t['balance_after']:.2f}")