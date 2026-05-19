from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class RRGQuadrant(str, Enum):
    LEADING   = "Leading"
    WEAKENING = "Weakening"
    LAGGING   = "Lagging"
    IMPROVING = "Improving"


class QuarterlyData(BaseModel):
    quarter:    str
    eps:        Optional[float] = None
    qoq_eps:    Optional[float] = None
    yoy_eps:    Optional[float] = None
    sales:      Optional[float] = None
    qoq_sales:  Optional[float] = None
    yoy_sales:  Optional[float] = None
    opm:        Optional[float] = None


class SectorData(BaseModel):
    name: str
    performance_1d:       Optional[float] = None
    performance_1w:       Optional[float] = None
    performance_1m:       Optional[float] = None
    performance_3m:       Optional[float] = None
    quadrant:             Optional[RRGQuadrant] = None
    rs_ratio:             Optional[float] = None
    rs_momentum:          Optional[float] = None
    stock_count:          Optional[int]   = None
    industry_avg_trend:   float           = 0.0

    def rrg_score(self) -> float:
        """
        Multi-factor sector score.  Higher = stronger candidate.

        Avoids three common false-signal traps:

        1. Extended Leading (RS-Ratio > 115 or Momentum > 115):
           The sector has already moved far; sweet spot is 102–110 for both.
           Values above 115 are penalised — the move may be finishing.

        2. Leading with only high Momentum + low RS:
           Momentum is rising but the sector hasn't actually outperformed the
           benchmark yet.  RS below 102 gets a small penalty.

        3. Leading with only high RS + fading Momentum:
           Sector has been strong but is rolling over.  Momentum below 102
           in a Leading sector is penalised.

        Both Leading AND Improving quadrants can score positively.
        Improving with strong momentum signals a sector rotating INTO Leading.
        """
        # Base score by quadrant
        base: float = {
            RRGQuadrant.LEADING:    100.0,
            RRGQuadrant.IMPROVING:   60.0,   # potential next leader
            RRGQuadrant.WEAKENING:  -50.0,
            RRGQuadrant.LAGGING:   -150.0,
        }.get(self.quadrant, 0.0)             # type: ignore[arg-type]

        rs  = self.rs_ratio    or 100.0
        mom = self.rs_momentum or 100.0

        # ---- RS-Ratio score (sweet spot 102–110, penalise > 115)
        if rs <= 100:
            rs_score = (rs - 100.0) * 2.0          # negative: below benchmark
        elif rs <= 110:
            rs_score = (rs - 100.0) * 3.0          # 0–30 pts, ideal range
        elif rs <= 115:
            rs_score = 30.0 - (rs - 110.0) * 2.0  # 20–30 pts, tapering
        else:
            rs_score = 20.0 - (rs - 115.0) * 4.0  # penalise extension > 115

        # ---- Momentum score (interpretation differs by quadrant)
        if self.quadrant == RRGQuadrant.IMPROVING:
            # Rising momentum here = entering Leading — strongest buy signal
            mom_score = (mom - 100.0) * 2.5 if mom > 100 else (mom - 100.0)
        else:
            # In Leading: moderate is better than extreme
            if mom <= 100:
                mom_score = (mom - 100.0) * 2.0    # fading — penalise
            elif mom <= 110:
                mom_score = (mom - 100.0) * 2.0    # 0–20 pts, ideal
            elif mom <= 115:
                mom_score = 20.0 - (mom - 110.0) * 1.5   # tapering
            else:
                mom_score = 12.5 - (mom - 115.0) * 3.0   # penalise overbought

        # Small weight for recent 1-day performance — don't chase intraday noise
        perf_score = (self.performance_1d or 0.0) * 0.5

        # Industry confirmation: industries in this sector improving their ranks
        industry_bonus = self.industry_avg_trend * 0.3

        return base + rs_score + mom_score + perf_score + industry_bonus


class IndustryData(BaseModel):
    name:            str
    sector:          str
    performance_1d:  Optional[float] = None
    performance_1w:  Optional[float] = None   # was performance_5d
    performance_1m:  Optional[float] = None
    performance_3m:  Optional[float] = None
    rank_1w:         Optional[int]   = None
    rank_1m:         Optional[int]   = None
    rank_3m:         Optional[int]   = None
    stock_count:        Optional[int]   = None
    market_cap:         Optional[float] = None
    from_52w_high_pct:  Optional[float] = None
    quadrant:           Optional[RRGQuadrant] = None
    rs_ratio:           Optional[float] = None
    rs_momentum:        Optional[float] = None

    def trend_score(self) -> float:
        """
        Measures whether this industry is genuinely strengthening.

        Components:
        1. Rank improvement — 3M → 1M → 1W rank getting better (smaller = higher)
        2. Consistent improvement — each window better than the previous
        3. Performance consistency across 1W, 1M, 3M (breadth of the move)
        4. Short-term acceleration — 1W pace faster than 1M pace
        5. RRG quadrant confirmation
        """
        score = 0.0

        r1w = self.rank_1w
        r1m = self.rank_1m
        r3m = self.rank_3m

        # Overall rank improvement over 3 months (% improvement)
        if r1w is not None and r3m is not None and r3m > 0:
            improvement_pct = (r3m - r1w) / r3m * 100.0
            score += improvement_pct   # e.g. rank 20→5 = +75 pts

        # Stepwise consistency: each period better than the previous
        if r1w is not None and r1m is not None and r1m > r1w:
            score += 20.0   # 1M → 1W rank improved
        if r1m is not None and r3m is not None and r3m > r1m:
            score += 15.0   # 3M → 1M rank improved

        # Performance consistency across timeframes
        p1w = self.performance_1w or 0.0
        p1m = self.performance_1m or 0.0
        p3m = self.performance_3m or 0.0

        positive_periods = sum(1 for p in [p1w, p1m, p3m] if p > 0)
        score += positive_periods * 10.0   # up to +30 for all positive

        # Short-term acceleration: is the 1W pace stronger than the 1M pace?
        if p1w > 0 and p1m > 0 and (p1w * 4.0) > p1m:
            score += 15.0

        # RRG quadrant confirmation
        if self.quadrant == RRGQuadrant.LEADING:
            score += 40.0
        elif self.quadrant == RRGQuadrant.IMPROVING:
            score += 25.0   # rotating into leading = good entry signal

        return score


class StockData(BaseModel):
    ticker:              str
    name:                Optional[str]   = None
    sector:              Optional[str]   = None
    industry:            Optional[str]   = None
    exchange:            Optional[str]   = None
    price:               Optional[float] = None
    change_pct:          Optional[float] = None
    volume:              Optional[int]   = None
    volume_20d_ma:       Optional[int]   = None
    circuit_status:      Optional[str]   = None
    eps_growth_pct:      Optional[float] = None
    revenue_growth_pct:  Optional[float] = None
    market_cap:          Optional[float] = None
    pe_ratio:            Optional[float] = None
    rs_rating:           Optional[float] = None
    returns_1m:          Optional[float] = None
    returns_3m:          Optional[float] = None
    from_52w_high_pct:   Optional[float] = None

    def tv_symbol(self) -> str:
        """Return the TradingView symbol string, normalising ticker separators."""
        exch   = (self.exchange or "NSE").strip().upper()
        # TradingView uses underscores; ChartsMaze may store hyphens or spaces
        symbol = self.ticker.upper().replace("-", "_").replace(" ", "_")
        return f"{exch}:{symbol}"
