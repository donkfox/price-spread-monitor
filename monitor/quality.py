from __future__ import annotations

from decimal import Decimal

from .models import AlertAssessment, Opportunity


def assess_opportunity(opportunity: Opportunity, settings: dict | None = None) -> AlertAssessment:
    rules = settings or {}
    minimum_sales = int(rules.get("a_min_observed_sales_7d", 5))
    minimum_active_days = int(rules.get("a_min_active_days_7d", 3))
    maximum_latest_age = float(rules.get("a_max_latest_sale_age_hours", 48))
    maximum_gap = float(rules.get("a_max_sale_gap_hours", 72))
    maximum_price_range = Decimal(str(rules.get("a_max_price_range_percent", 10)))
    a_plus_score = float(rules.get("a_plus_min_score", 96))
    a_score = float(rules.get("a_min_score", 75))
    b_plus_score = float(rules.get("b_plus_min_score", 55))
    a_plus_profit = Decimal(str(rules.get("a_plus_min_profit_yuan", 80)))

    profile = opportunity.sales_profile
    if profile is None:
        return AlertAssessment("B", ("同规格7天成交样本不足",))

    reasons: list[str] = []
    score = 100.0
    if profile.latest_age_hours is None or profile.latest_age_hours > maximum_latest_age:
        reasons.append(f"最近成交超过{maximum_latest_age:g}小时")
        score -= 18 if profile.latest_age_hours is not None and profile.latest_age_hours <= maximum_latest_age * 2 else 30
    if profile.observed_sales_7d < minimum_sales:
        reasons.append(f"7天同规格仅观察到{profile.observed_sales_7d}次成交")
        score -= min(32, (minimum_sales - profile.observed_sales_7d) * 8)
    if profile.active_days_7d < minimum_active_days:
        reasons.append(f"7天仅{profile.active_days_7d}个活跃成交日")
        score -= min(24, (minimum_active_days - profile.active_days_7d) * 8)
    if profile.max_gap_hours is None or profile.max_gap_hours > maximum_gap:
        reasons.append("成交样本不足以确认间隔" if profile.max_gap_hours is None else f"最大成交间隔{profile.max_gap_hours:g}小时")
        score -= 12 if profile.max_gap_hours is not None else 18
    if profile.price_trend == "falling":
        reasons.append("最近成交价连续明显下降")
        score -= 22
    if profile.price_range_percent > maximum_price_range:
        reasons.append(f"7天成交价波动{profile.price_range_percent:.1f}%")
        score -= 10 if profile.price_range_percent <= maximum_price_range * Decimal("1.5") else 18

    if score >= a_plus_score and opportunity.spread >= a_plus_profit:
        level = "A+"
    elif score >= a_score:
        level = "A"
    elif score >= b_plus_score:
        level = "B+"
    else:
        level = "B"
    return AlertAssessment(level, tuple(reasons))
