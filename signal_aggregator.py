"""
Signal aggregator - reads decisions from jev-polymarket, jev-news-sentiment,
jev-crypto-decisions and computes a composite weighted signal.
"""
import json
import os
import logging
import glob
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)

SIGNAL_SOURCES = {
    "polymarket": os.path.join(PARENT_DIR, "jev-polymarket", "data", "decisions.jsonl"),
    "news_sentiment": os.path.join(PARENT_DIR, "jev-news-sentiment", "data"),
    "crypto_decisions": os.path.join(PARENT_DIR, "jev-crypto-decisions", "data"),
}

DEFAULT_WEIGHTS = {
    "polymarket": 0.45,
    "news_sentiment": 0.30,
    "crypto_decisions": 0.25,
}


def _read_jsonl(path: str, max_age_seconds: int = 3600) -> List[Dict]:
    """Read JSONL file, filtering to recent entries."""
    entries = []
    if not os.path.exists(path):
        return entries
    cutoff = datetime.utcnow() - timedelta(seconds=max_age_seconds)
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts = entry.get("timestamp") or entry.get("ts") or entry.get("time")
                    if ts:
                        try:
                            entry_time = datetime.fromisoformat(str(ts).replace("Z", "+00:00").replace("+00:00", ""))
                        except Exception:
                            entry_time = datetime.utcnow()
                        if entry_time < cutoff:
                            continue
                    entries.append(entry)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        log.warning("Failed to read %s: %s", path, e)
    return entries


def _read_jsonl_dir(directory: str, max_age_seconds: int = 3600) -> List[Dict]:
    """Read all .jsonl files in a directory."""
    entries = []
    if not os.path.isdir(directory):
        return entries
    for path in sorted(glob.glob(os.path.join(directory, "*.jsonl"))):
        entries.extend(_read_jsonl(path, max_age_seconds))
    return entries


def _extract_score(entry: Dict) -> Optional[float]:
    """Extract a 0-1 score from a decision entry."""
    for key in ("score", "confidence", "probability", "fair_prob", "signal"):
        val = entry.get(key)
        if val is not None:
            try:
                v = float(val)
                if v > 1.0:
                    v = v / 100.0
                return max(0.0, min(1.0, v))
            except (ValueError, TypeError):
                continue
    return None


def _extract_direction(entry: Dict) -> Optional[str]:
    """Extract directional signal: 'bullish', 'bearish', or None."""
    action = (entry.get("action") or entry.get("direction") or
              entry.get("choice") or entry.get("signal") or "").lower()
    if any(w in action for w in ("buy-yes", "bullish", "up", "long", "yes")):
        return "bullish"
    if any(w in action for w in ("buy-no", "bearish", "down", "short", "no")):
        return "bearish"
    return None


class SignalAggregator:
    """Composite signal aggregator across multiple Jev signal sources."""

    def __init__(self, weights: Optional[Dict[str, float]] = None,
                 max_age_seconds: int = 3600):
        self.weights = weights or DEFAULT_WEIGHTS
        self.max_age_seconds = max_age_seconds

    def read_polymarket_signals(self) -> List[Dict]:
        """Read signals from jev-polymarket/data/decisions.jsonl."""
        path = SIGNAL_SOURCES["polymarket"]
        entries = _read_jsonl(path, self.max_age_seconds)
        log.info("Read %d polymarket signals from %s", len(entries), path)
        return entries

    def read_news_signals(self) -> List[Dict]:
        """Read signals from jev-news-sentiment/data/."""
        path = SIGNAL_SOURCES["news_sentiment"]
        entries = _read_jsonl_dir(path, self.max_age_seconds)
        log.info("Read %d news sentiment signals", len(entries))
        return entries

    def read_crypto_signals(self) -> List[Dict]:
        """Read signals from jev-crypto-decisions/data/."""
        path = SIGNAL_SOURCES["crypto_decisions"]
        entries = _read_jsonl_dir(path, self.max_age_seconds)
        log.info("Read %d crypto decision signals", len(entries))
        return entries

    def read_all_signals(self) -> Dict[str, List[Dict]]:
        """Read all signal sources."""
        return {
            "polymarket": self.read_polymarket_signals(),
            "news_sentiment": self.read_news_signals(),
            "crypto_decisions": self.read_crypto_signals(),
        }

    def compute_composite(self, market_filter: Optional[str] = None) -> Dict[str, Any]:
        """
        Compute composite signal across all sources.
        Returns: {score: float 0-1, direction: str, confidence: float,
                  sources: {name: {score, count, direction}}, raw: {name: [entries]}}
        """
        all_signals = self.read_all_signals()
        source_results: Dict[str, Dict] = {}
        weighted_sum = 0.0
        weight_total = 0.0

        for source_name, entries in all_signals.items():
            if not entries:
                source_results[source_name] = {"score": None, "count": 0, "direction": None}
                continue

            # Filter by market if requested
            if market_filter:
                entries = [e for e in entries
                           if market_filter.lower() in json.dumps(e).lower()]
            if not entries:
                source_results[source_name] = {"score": None, "count": 0, "direction": None}
                continue

            scores = []
            directions = []
            for entry in entries:
                s = _extract_score(entry)
                d = _extract_direction(entry)
                if s is not None:
                    scores.append(s)
                if d:
                    directions.append(d)

            if scores:
                avg_score = sum(scores) / len(scores)
            else:
                avg_score = 0.5  # neutral

            # Majority direction
            if directions:
                bullish = sum(1 for d in directions if d == "bullish")
                bearish = sum(1 for d in directions if d == "bearish")
                direction = "bullish" if bullish > bearish else "bearish" if bearish > bullish else "neutral"
            else:
                direction = "neutral"

            w = self.weights.get(source_name, 0.0)
            source_results[source_name] = {
                "score": avg_score,
                "count": len(entries),
                "direction": direction,
            }
            weighted_sum += avg_score * w
            weight_total += w

        composite_score = weighted_sum / weight_total if weight_total > 0 else 0.5

        # Composite direction by weighted vote
        dir_scores = {"bullish": 0.0, "bearish": 0.0}
        for source_name, result in source_results.items():
            w = self.weights.get(source_name, 0.0)
            d = result.get("direction")
            if d in dir_scores:
                dir_scores[d] += w
        composite_direction = max(dir_scores, key=dir_scores.get)
        if dir_scores["bullish"] == dir_scores["bearish"]:
            composite_direction = "neutral"

        confidence = abs(composite_score - 0.5) * 2  # 0-1 scale

        return {
            "score": round(composite_score, 4),
            "direction": composite_direction,
            "confidence": round(confidence, 4),
            "sources": source_results,
            "raw": all_signals,
        }

    def get_latest_decisions(self, limit: int = 10) -> List[Dict]:
        """Get the most recent decision entries across all sources."""
        all_signals = self.read_all_signals()
        combined = []
        for source_name, entries in all_signals.items():
            for entry in entries:
                entry["_source"] = source_name
                combined.append(entry)

        # Sort by timestamp if available
        def _ts(e):
            for key in ("timestamp", "ts", "time"):
                if key in e:
                    return str(e[key])
            return ""
        combined.sort(key=_ts, reverse=True)
        return combined[:limit]