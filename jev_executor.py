"""
Jev decision engine for Polymarket.
Builds state from market data, asks Jev via OpenRouter, computes edge.
"""
import json
import logging
import time
from typing import Dict, List, Optional, Any

import requests

log = logging.getLogger(__name__)

JEV_API_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "typesafe/jev-1.13"


class JevExecutor:
    """Jev-powered decision engine for Polymarket trading."""

    def __init__(self, api_key: str = "", min_edge: float = 0.08):
        self.api_key = api_key
        self.min_edge = min_edge

    def build_state(self, market: Dict, price_to_beat: Optional[float] = None,
                    real_time_price: Optional[float] = None,
                    news_summary: str = "", signal_data: Optional[Dict] = None) -> Dict:
        """
        Build Jev state from market data.
        """
        question = market.get("question", "")
        description = market.get("description", "")
        outcomes = market.get("tokens", [])
        
        # Get market prices (implied probabilities)
        yes_price = None
        no_price = None
        for token in outcomes:
            outcome = (token.get("outcome") or "").lower()
            price = token.get("price")
            if price is not None:
                try:
                    price = float(price)
                except (ValueError, TypeError):
                    price = None
            if outcome == "yes" and price is not None:
                yes_price = price
            elif outcome == "no" and price is not None:
                no_price = price

        market_implied_prob = yes_price if yes_price is not None else 0.5
        volume = float(market.get("volume", 0) or 0)
        liquidity = float(market.get("liquidity", 0) or 0)
        end_date = market.get("endDate") or market.get("end_date_iso") or ""

        state = {
            "market_question": question,
            "market_description": description,
            "market_yes_price": yes_price,
            "market_no_price": no_price,
            "market_implied_prob": market_implied_prob,
            "price_to_beat": price_to_beat,
            "real_time_price": real_time_price,
            "volume": volume,
            "liquidity": liquidity,
            "end_date": end_date,
            "news_summary": news_summary,
            "signal_data": signal_data or {},
            "timestamp": int(time.time()),
        }
        return state

    def ask_jev(self, state: Dict) -> Dict[str, Any]:
        """
        Ask Jev for a decision via OpenRouter decisions endpoint.
        POST https://openrouter.ai/api/alpha/decisions
        body: {model: typesafe/jev-1.13, state, questions}
        """
        questions = [
            {
                "name": "fair_probability",
                "description": "What is the fair probability (0-100) that the market resolves YES?",
                "criteria": [
                    {"name": "market_analysis", "description": "Analyze the market question and context"},
                    {"name": "price_data", "description": "Consider real-time price vs price-to-beat"},
                    {"name": "news_sentiment", "description": "Factor in news sentiment"},
                    {"name": "signal_agreement", "description": "Check signal data agreement"},
                ],
            },
            {
                "name": "mispriced",
                "description": "Is the market mispriced? Answer 'yes' or 'no'.",
                # no criteria (noul)
            },
            {
                "name": "execute_action",
                "description": "What action should be taken?",
                "criteria": {
                    "buy-yes": "Market is underpriced on YES side",
                    "buy-no": "Market is overpriced on YES side (underpriced NO)",
                    "skip": "No clear edge or too risky",
                },
            },
            {
                "name": "confidence",
                "description": "How confident are you in this assessment (0-100)?",
                "criteria": [
                    {"name": "data_quality", "description": "Quality and completeness of available data"},
                    {"name": "signal_strength", "description": "Strength of trading signals"},
                    {"name": "edge_clarity", "description": "Clarity of the pricing edge"},
                ],
            },
        ]

        payload = {
            "model": JEV_MODEL,
            "state": state,
            "questions": questions,
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        try:
            r = requests.post(JEV_API_URL, json=payload, headers=headers, timeout=60)
            r.raise_for_status()
            raw = r.json()
            return self._parse_jev_response(raw)
        except Exception as e:
            log.warning("Jev API call failed: %s", e)
            return self._default_decision()

    def _parse_jev_response(self, raw: Dict) -> Dict[str, Any]:
        """Parse Jev decisions response into structured decision."""
        answers = raw.get("answers") or raw.get("decisions") or raw.get("choices") or {}

        # Extract fair probability
        fair_prob_raw = answers.get("fair_probability", 50)
        if isinstance(fair_prob_raw, dict):
            fair_prob_raw = fair_prob_raw.get("value") or fair_prob_raw.get("answer", 50)
        try:
            fair_prob = float(fair_prob_raw) / 100.0  # map 0-100 to 0-1
        except (ValueError, TypeError):
            fair_prob = 0.5
        fair_prob = max(0.0, min(1.0, fair_prob))

        # Extract mispriced
        mispriced_raw = answers.get("mispriced", "no")
        if isinstance(mispriced_raw, dict):
            mispriced_raw = mispriced_raw.get("value") or mispriced_raw.get("answer", "no")
        mispriced = str(mispriced_raw).lower().strip() == "yes"

        # Extract action
        action_raw = answers.get("execute_action", "skip")
        if isinstance(action_raw, dict):
            action_raw = action_raw.get("value") or action_raw.get("answer", "skip")
        action = str(action_raw).lower().strip()
        if action not in ("buy-yes", "buy-no", "skip"):
            action = "skip"

        # Extract confidence
        conf_raw = answers.get("confidence", 50)
        if isinstance(conf_raw, dict):
            conf_raw = conf_raw.get("value") or conf_raw.get("answer", 50)
        try:
            confidence = float(conf_raw) / 100.0
        except (ValueError, TypeError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))

        return {
            "fair_probability": fair_prob,
            "mispriced": mispriced,
            "action": action,
            "confidence": confidence,
            "raw": raw,
        }

    def _default_decision(self) -> Dict[str, Any]:
        """Default safe decision when Jev is unavailable."""
        return {
            "fair_probability": 0.5,
            "mispriced": False,
            "action": "skip",
            "confidence": 0.0,
            "raw": {},
        }

    def evaluate_market(self, market: Dict, price_to_beat: Optional[float] = None,
                        real_time_price: Optional[float] = None,
                        news_summary: str = "",
                        signal_data: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Full evaluation: build state, ask Jev, compute edge.
        Returns decision with edge calculation.
        """
        state = self.build_state(market, price_to_beat, real_time_price,
                                 news_summary, signal_data)
        decision = self.ask_jev(state)

        market_implied_prob = state.get("market_implied_prob", 0.5) or 0.5
        fair_prob = decision.get("fair_probability", 0.5)

        # Edge = fair_prob - market_implied_prob
        if decision.get("action") == "buy-no":
            edge = market_implied_prob - fair_prob  # we think NO is underpriced
        else:
            edge = fair_prob - market_implied_prob

        should_trade = (
            decision.get("action") != "skip"
            and decision.get("mispriced", False)
            and abs(edge) > self.min_edge
            and decision.get("confidence", 0) > 0.3
        )

        return {
            **decision,
            "state": state,
            "edge": round(edge, 4),
            "should_trade": should_trade,
            "market_implied_prob": market_implied_prob,
        }

    def batch_evaluate(self, markets: List[Dict], **kwargs) -> List[Dict]:
        """Evaluate multiple markets."""
        results = []
        for market in markets:
            try:
                result = self.evaluate_market(market, **kwargs)
                results.append(result)
            except Exception as e:
                log.warning("Failed to evaluate market %s: %s",
                            market.get("question", "?"), e)
                results.append({
                    **self._default_decision(),
                    "edge": 0.0,
                    "should_trade": False,
                    "error": str(e),
                })
        return results