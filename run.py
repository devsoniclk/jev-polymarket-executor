"""
CLI entrypoint for Jev Polymarket Executor.
Commands: scan, decide, execute, monitor, positions, stats, redeem
"""
import os
import sys
import json
import logging
import argparse
import time
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from polymarket_client import PolymarketClient
from signal_aggregator import SignalAggregator
from jev_executor import JevExecutor
from order_manager import OrderManager
from risk_manager import RiskManager
from market_scanner import MarketScanner
from paper_trader import PaperTrader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("jev-polymarket")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)


def load_config() -> dict:
    """Load config from config.yaml or env."""
    import yaml
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
    config = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                config = yaml.safe_load(f) or {}
        except Exception:
            pass

    # Override from env
    from dotenv import load_dotenv
    dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(dotenv_path):
        load_dotenv(dotenv_path)

    config.setdefault("openrouter_api_key", os.getenv("OPENROUTER_API_KEY", ""))
    config.setdefault("polymarket_api_key", os.getenv("POLYMARKET_API_KEY", ""))
    config.setdefault("polymarket_api_secret", os.getenv("POLYMARKET_API_SECRET", ""))
    config.setdefault("polymarket_passphrase", os.getenv("POLYMARKET_PASSPHRASE", ""))
    config.setdefault("paper_mode", os.getenv("PAPER_MODE", "true").lower() == "true")
    config.setdefault("min_edge", float(os.getenv("MIN_EDGE", "0.08")))
    config.setdefault("max_per_trade", float(os.getenv("MAX_PER_TRADE", "10")))
    config.setdefault("max_exposure", float(os.getenv("MAX_EXPOSURE", "100")))
    config.setdefault("daily_loss_limit", float(os.getenv("DAILY_LOSS_LIMIT", "50")))

    return config


def make_components(config: dict):
    """Build all components from config."""
    client = PolymarketClient(
        api_key=config.get("polymarket_api_key", ""),
        api_secret=config.get("polymarket_api_secret", ""),
        passphrase=config.get("polymarket_passphrase", ""),
    )
    jev = JevExecutor(
        api_key=config.get("openrouter_api_key", ""),
        min_edge=config.get("min_edge", 0.08),
    )
    scanner = MarketScanner(
        client=client,
        min_volume=config.get("min_volume", 1000),
        min_liquidity=config.get("min_liquidity", 500),
    )
    order_mgr = OrderManager(
        client=client,
        paper_mode=config.get("paper_mode", True),
    )
    risk_mgr = RiskManager(
        max_per_trade=config.get("max_per_trade", 10.0),
        max_exposure=config.get("max_exposure", 100.0),
        daily_loss_limit=config.get("daily_loss_limit", 50.0),
        kelly_fraction=config.get("kelly_fraction", 0.25),
    )
    signal_agg = SignalAggregator()
    paper_trader = PaperTrader(initial_balance=config.get("paper_balance", 100.0))

    return client, jev, scanner, order_mgr, risk_mgr, signal_agg, paper_trader


# ── Commands ──────────────────────────────────────────────────────

def cmd_scan(args, config):
    """Scan for tradeable markets."""
    client, jev, scanner, _, _, _, _ = make_components(config)

    print("=" * 70)
    print("SCANNING POLYMARKET MARKETS")
    print("=" * 70)

    markets = scanner.scan_all()

    if not markets:
        print("\nNo tradeable markets found. Check API connectivity.")
        return

    print(f"\nFound {len(markets)} tradeable markets:\n")
    for i, m in enumerate(markets, 1):
        print(f"  {i}. {scanner.format_market(m)}")
        print()

    # Save to file
    scan_file = os.path.join(DATA_DIR, "last_scan.json")
    with open(scan_file, "w") as f:
        json.dump(markets, f, indent=2, default=str)
    print(f"Saved to {scan_file}")


def cmd_decide(args, config):
    """Run Jev decision engine on scanned markets."""
    client, jev, scanner, _, risk_mgr, signal_agg, _ = make_components(config)

    print("=" * 70)
    print("JEV DECISION ENGINE")
    print("=" * 70)

    # Load last scan or scan fresh
    scan_file = os.path.join(DATA_DIR, "last_scan.json")
    if os.path.exists(scan_file) and not args.fresh:
        with open(scan_file, "r") as f:
            markets = json.load(f)
        print(f"\nUsing {len(markets)} markets from last scan")
    else:
        markets = scanner.scan_all()
        print(f"\nFresh scan: {len(markets)} markets found")

    if not markets:
        print("No markets to evaluate.")
        return

    # Get composite signal
    composite = signal_agg.compute_composite()
    print(f"\nComposite signal: score={composite['score']}, "
          f"dir={composite['direction']}, conf={composite['confidence']}")

    # Check risk
    risk_check = risk_mgr.can_trade()
    print(f"Risk status: {'OK' if risk_check['allowed'] else risk_check['reason']}")

    decisions = []
    for i, market in enumerate(markets[:10], 1):  # limit to 10
        question = market.get("question", "?")
        print(f"\n--- Market {i}: {question[:60]}...")

        # Get real-time price if available
        tokens = market.get("tokens", [])
        real_time_price = None
        price_to_beat = None
        if tokens:
            for t in tokens:
                p = t.get("price")
                if p:
                    try:
                        real_time_price = float(p)
                        break
                    except (ValueError, TypeError):
                        pass

        result = jev.evaluate_market(
            market,
            price_to_beat=price_to_beat,
            real_time_price=real_time_price,
            signal_data=composite,
        )

        edge = result.get("edge", 0)
        action = result.get("action", "skip")
        fair_prob = result.get("fair_probability", 0.5)
        market_prob = result.get("market_implied_prob", 0.5)
        should_trade = result.get("should_trade", False)
        confidence = result.get("confidence", 0)

        print(f"  Fair prob: {fair_prob:.3f} | Market: {market_prob:.3f} | Edge: {edge:+.3f}")
        print(f"  Action: {action} | Confidence: {confidence:.2f} | Trade: {'YES' if should_trade else 'NO'}")

        # Size the trade if applicable
        if should_trade:
            sizing = risk_mgr.size_trade(fair_prob, market_prob, edge, confidence)
            print(f"  Size: ${sizing['size']:.2f} ({sizing['method']})")

        result["market_question"] = question
        decisions.append(result)

    # Save decisions
    decisions_file = os.path.join(DATA_DIR, "decisions.jsonl")
    for d in decisions:
        d["timestamp"] = datetime.utcnow().isoformat()
        with open(decisions_file, "a") as f:
            f.write(json.dumps(d, default=str) + "\n")

    tradeable = [d for d in decisions if d.get("should_trade")]
    print(f"\n{'='*70}")
    print(f"SUMMARY: {len(tradeable)}/{len(decisions)} markets with tradeable edge")
    print(f"{'='*70}")


def cmd_execute(args, config):
    """Execute trades based on decisions."""
    _, _, _, order_mgr, risk_mgr, _, paper_trader = make_components(config)

    print("=" * 70)
    print("EXECUTE TRADES")
    print("=" * 70)

    if order_mgr.is_killed():
        print("KILL SWITCH ACTIVE. Remove data/KILL to resume.")
        return

    # Load latest decisions
    decisions_file = os.path.join(DATA_DIR, "decisions.jsonl")
    if not os.path.exists(decisions_file):
        print("No decisions file. Run 'decide' first.")
        return

    # Read last batch of decisions
    decisions = []
    with open(decisions_file, "r") as f:
        for line in f:
            if line.strip():
                try:
                    decisions.append(json.loads(line.strip()))
                except json.JSONDecodeError:
                    continue

    tradeable = [d for d in decisions if d.get("should_trade")]
    if not tradeable:
        print("No tradeable decisions found.")
        return

    print(f"Executing {len(tradeable)} trades...")
    for d in tradeable:
        action = d.get("action", "skip")
        fair_prob = d.get("fair_probability", 0.5)
        market_prob = d.get("market_implied_prob", 0.5)
        edge = d.get("edge", 0)
        confidence = d.get("confidence", 0)
        question = d.get("market_question", "?")

        # Size
        sizing = risk_mgr.size_trade(fair_prob, market_prob, edge, confidence)
        size = sizing["size"]
        if size <= 0:
            print(f"  SKIP {question[:50]} (size=0)")
            continue

        # For paper mode
        if config.get("paper_mode", True):
            # Determine price
            if action == "buy-yes":
                price = market_prob
            elif action == "buy-no":
                price = 1.0 - market_prob
            else:
                continue

            shares = size / price if price > 0 else 0
            result = paper_trader.simulate_buy(
                token_id=d.get("state", {}).get("market_question", "unknown"),
                side=action,
                price=price,
                size=shares,
                market_question=question,
                fair_prob=fair_prob,
            )
            print(f"  PAPER {action} ${size:.2f} @ {price:.3f} = {shares:.1f} shares | {result.get('order_id', 'err')}")
        else:
            # Live trading via order manager
            print(f"  LIVE {action} ${size:.2f} (not implemented in paper mode)")

    print(f"\nExecution complete.")


def cmd_monitor(args, config):
    """Monitor positions and cancel stale orders."""
    _, _, _, order_mgr, _, _, _ = make_components(config)

    print("=" * 70)
    print("MONITORING")
    print("=" * 70)

    order_mgr.start_cancel_loop()
    print("Cancel loop started (17s interval, 18s max age)")
    print("Press Ctrl+C to stop")

    try:
        while True:
            positions = order_mgr.get_positions()
            exposure = order_mgr.get_exposure()
            daily_pnl = order_mgr.get_daily_pnl()
            print(f"\rPositions: {len(positions)} | Exposure: ${exposure:.2f} | "
                  f"Daily P&L: ${daily_pnl:.2f}", end="", flush=True)
            time.sleep(5)
    except KeyboardInterrupt:
        order_mgr.stop_cancel_loop()
        print("\nMonitoring stopped.")


def cmd_positions(args, config):
    """Show current positions."""
    _, _, _, order_mgr, _, _, paper_trader = make_components(config)

    print("=" * 70)
    print("POSITIONS")
    print("=" * 70)

    # Live positions
    live = order_mgr.get_positions()
    print(f"\nLive positions ({len(live)}):")
    if live:
        for key, pos in live.items():
            print(f"  {key}: size={pos.get('size', 0):.1f} "
                  f"avg=${pos.get('avg_price', 0):.3f} "
                  f"q={pos.get('market_question', '?')[:50]}")
    else:
        print("  (none)")

    # Paper positions
    paper = paper_trader.get_positions()
    print(f"\nPaper positions ({len(paper)}):")
    if paper:
        for key, pos in paper.items():
            print(f"  {key}: size={pos.get('size', 0):.1f} "
                  f"avg=${pos.get('avg_price', 0):.3f} "
                  f"q={pos.get('market_question', '?')[:50]}")
    else:
        print("  (none)")


def cmd_stats(args, config):
    """Show trading statistics."""
    _, _, _, order_mgr, risk_mgr, _, paper_trader = make_components(config)

    print("=" * 70)
    print("TRADING STATISTICS")
    print("=" * 70)

    stats = paper_trader.get_stats()
    print(f"\nPaper Trading:")
    print(f"  Balance: ${stats['balance']:.2f} (started ${stats['initial_balance']:.2f})")
    print(f"  Total P&L: ${stats['total_pnl']:.2f} ({stats['roi_pct']:.1f}% ROI)")
    print(f"  Trades: {stats['total_trades']} (W:{stats['wins']} L:{stats['losses']})")
    print(f"  Win Rate: {stats['win_rate']*100:.1f}%")
    print(f"  Brier Score: {stats['brier_score']:.4f}")
    print(f"  Open Positions: {stats['open_positions']}")

    risk = risk_mgr.get_status()
    print(f"\nRisk Status:")
    print(f"  Circuit Breaker: {'ACTIVE' if risk['circuit_breaker_active'] else 'OFF'} "
          f"({risk['consecutive_losses']} consecutive losses)")
    print(f"  Daily P&L: ${risk['daily_pnl']:.2f} / -${risk['daily_loss_limit']:.2f}")
    print(f"  Trades today: {risk['trade_count']}")
    print(f"  Max/trade: ${risk['max_per_trade']:.2f} | Max exposure: ${risk['max_exposure']:.2f}")


def cmd_redeem(args, config):
    """Redeem winning positions."""
    _, _, _, order_mgr, _, _, paper_trader = make_components(config)

    print("=" * 70)
    print("REDEEM WINNERS")
    print("=" * 70)

    if config.get("paper_mode", True):
        print("Paper mode: resolve positions manually")
        positions = paper_trader.get_positions()
        if not positions:
            print("No open paper positions.")
            return

        for key, pos in positions.items():
            side = pos.get("side", "?")
            q = pos.get("market_question", "?")
            print(f"\n  {key}: {q}")
            # In paper mode, we'd need to check resolution
            print(f"  (Use simulate_resolve for manual resolution)")
    else:
        redeemed = order_mgr.redeem_winners()
        if redeemed:
            for r in redeemed:
                print(f"  Redeemed: {r}")
        else:
            print("No winners to redeem.")


def main():
    parser = argparse.ArgumentParser(description="Jev Polymarket Executor")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("scan", help="Scan for tradeable markets")
    decide_p = sub.add_parser("decide", help="Run Jev decision engine")
    decide_p.add_argument("--fresh", action="store_true", help="Fresh scan")
    sub.add_parser("execute", help="Execute trades")
    sub.add_parser("monitor", help="Monitor positions")
    sub.add_parser("positions", help="Show positions")
    sub.add_parser("stats", help="Show statistics")
    sub.add_parser("redeem", help="Redeem winners")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    config = load_config()

    commands = {
        "scan": cmd_scan,
        "decide": cmd_decide,
        "execute": cmd_execute,
        "monitor": cmd_monitor,
        "positions": cmd_positions,
        "stats": cmd_stats,
        "redeem": cmd_redeem,
    }

    cmd_fn = commands.get(args.command)
    if cmd_fn:
        cmd_fn(args, config)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()