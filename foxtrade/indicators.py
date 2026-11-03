from __future__ import annotations

from typing import Any


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def candle_reversal_flags(candles: list[dict[str, Any]]) -> dict[str, bool]:
    """Simple 5m-style pattern flags from OHLC list (oldest→newest)."""
    out = {
        "shooting_star": False,
        "inverted_hammer": False,
        "hammer": False,
        "bearish_engulf": False,
        "bullish_engulf": False,
    }
    if len(candles) < 2:
        return out
    c = candles[-1]
    p = candles[-2]
    body = abs(c["close"] - c["open"])
    upper = c["high"] - max(c["open"], c["close"])
    lower = min(c["open"], c["close"]) - c["low"]
    rng = max(c["high"] - c["low"], 1e-6)

    if upper > body * 2 and lower < body * 0.5 and c["close"] < c["open"]:
        out["shooting_star"] = True
    if upper > body * 2 and lower < body * 0.5 and c["close"] >= c["open"]:
        out["inverted_hammer"] = True
    if lower > body * 2 and upper < body * 0.5:
        out["hammer"] = True
    if (
        c["close"] < c["open"]
        and p["close"] > p["open"]
        and c["open"] >= p["close"]
        and c["close"] <= p["open"]
    ):
        out["bearish_engulf"] = True
    if (
        c["close"] > c["open"]
        and p["close"] < p["open"]
        and c["open"] <= p["close"]
        and c["close"] >= p["open"]
    ):
        out["bullish_engulf"] = True
    # unused rng keeps lint calm if extended
    _ = rng
    return out


def support_resistance(closes: list[float], lookback: int = 40) -> dict[str, float | None]:
    if len(closes) < 5:
        return {"support": None, "resistance": None}
    window = closes[-lookback:]
    return {"support": min(window), "resistance": max(window)}


def atr(candles: list[dict[str, Any]], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    window = trs[-period:]
    return sum(window) / len(window)
