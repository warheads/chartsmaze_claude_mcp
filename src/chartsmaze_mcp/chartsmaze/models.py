from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class RRGQuadrant(str, Enum):
    LEADING = "Leading"
    WEAKENING = "Weakening"
    LAGGING = "Lagging"
    IMPROVING = "Improving"


class SectorData(BaseModel):
    name: str
    performance_1d: Optional[float] = None
    performance_5d: Optional[float] = None
    performance_1m: Optional[float] = None
    performance_3m: Optional[float] = None
    quadrant: Optional[RRGQuadrant] = None
    rs_ratio: Optional[float] = None
    rs_momentum: Optional[float] = None
    stock_count: Optional[int] = None

    def rrg_score(self) -> float:
        """Higher score = more desirable sector. Leading quadrant gets biggest boost."""
        quadrant_bonus = {
            RRGQuadrant.LEADING: 200.0,
            RRGQuadrant.IMPROVING: 50.0,
            RRGQuadrant.WEAKENING: -50.0,
            RRGQuadrant.LAGGING: -200.0,
        }
        base = self.performance_1d or 0.0
        bonus = quadrant_bonus.get(self.quadrant, 0.0) if self.quadrant else 0.0
        # rs_ratio > 100 means outperforming the benchmark
        rs_bonus = (self.rs_ratio - 100.0) * 2 if self.rs_ratio is not None else 0.0
        return base + bonus + rs_bonus


class IndustryData(BaseModel):
    name: str
    sector: str
    performance_1d: Optional[float] = None
    performance_5d: Optional[float] = None
    quadrant: Optional[RRGQuadrant] = None
    rs_ratio: Optional[float] = None
    rs_momentum: Optional[float] = None


class StockData(BaseModel):
    ticker: str
    name: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    price: Optional[float] = None
    change_pct: Optional[float] = None
    volume: Optional[int] = None
    volume_20d_ma: Optional[int] = None
    # "Upper", "Lower", or None
    circuit_status: Optional[str] = None
    eps_growth_pct: Optional[float] = None
    revenue_growth_pct: Optional[float] = None
    market_cap: Optional[float] = None
    pe_ratio: Optional[float] = None
