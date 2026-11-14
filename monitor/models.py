from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


PRICE_TREND_LABELS = {
    "stable": "稳定",
    "falling": "连续下降",
    "insufficient": "样本不足",
}


def price_trend_label(value: str) -> str:
    return PRICE_TREND_LABELS.get(value, value)


@dataclass(frozen=True)
class Bounds:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def center(self) -> tuple[int, int]:
        return ((self.left + self.right) // 2, (self.top + self.bottom) // 2)


@dataclass
class ProductCard:
    title: str
    sku: str
    price: Decimal
    bounds: Bounds


@dataclass
class VerifiedProduct:
    platform: str
    title: str
    sku: str
    base_price: Decimal
    variant: str = ""
    size_prices: dict[str, Decimal] = field(default_factory=dict)
    stock_states: dict[str, bool] = field(default_factory=dict)
    stock_labels: dict[str, str] = field(default_factory=dict)
    recent_sales: list["RecentSale"] = field(default_factory=list)
    recent_sale_prices: dict[str, Decimal] = field(default_factory=dict)
    evaluation_sale_prices: dict[str, Decimal] = field(default_factory=dict)
    sales_profiles: dict[str, "SalesProfile"] = field(default_factory=dict)
    estimated_incomes: dict[str, Decimal] = field(default_factory=dict)
    source_sku: str = ""


@dataclass(frozen=True)
class RecentSale:
    size: str
    price: Decimal
    sold_at: str
    buyer: str = ""


@dataclass(frozen=True)
class SalesProfile:
    observed_sales_7d: int
    active_days_7d: int
    latest_age_hours: float | None
    max_gap_hours: float | None
    latest_price: Decimal
    median_price: Decimal
    minimum_price: Decimal
    maximum_price: Decimal
    conservative_price: Decimal
    price_range_percent: Decimal
    price_trend: str


@dataclass(frozen=True)
class AlertAssessment:
    level: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Opportunity:
    sku: str
    title: str
    size: str
    pdd_price: Decimal
    dewu_price: Decimal
    recent_sale_price: Decimal | None = None
    evaluation_sale_price: Decimal | None = None
    sales_profile: SalesProfile | None = None

    @property
    def estimated_income(self) -> Decimal:
        return self.dewu_price

    @property
    def spread(self) -> Decimal:
        return self.dewu_price - self.pdd_price
