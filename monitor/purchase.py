from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlparse


@dataclass(frozen=True)
class PurchasePlan:
    key: str
    name: str
    short_name: str
    days: int
    price: Decimal

    @property
    def price_text(self) -> str:
        return f"¥{self.price.normalize()}"


PURCHASE_PLANS = (
    PurchasePlan("trial_1d", "1 天体验", "体验码", 1, Decimal("4.8")),
    PurchasePlan("monthly_30d", "30 天", "月卡", 30, Decimal("88")),
    PurchasePlan("quarterly_90d", "90 天", "季卡", 90, Decimal("238")),
    PurchasePlan("annual_365d", "365 天", "年卡", 365, Decimal("798")),
)


def purchase_url(config: dict, plan_key: str) -> str:
    value = str(
        config.get("licensing", {}).get("purchase_urls", {}).get(plan_key, "")
    ).strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        return ""
    return value

