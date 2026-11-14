from __future__ import annotations

import unittest

from monitor.purchase import PURCHASE_PLANS, purchase_url


class PurchaseTests(unittest.TestCase):
    def test_all_plans_have_expected_price_and_duration(self) -> None:
        self.assertEqual(
            [(plan.key, plan.days, plan.price_text) for plan in PURCHASE_PLANS],
            [
                ("trial_1d", 1, "¥4.8"),
                ("monthly_30d", 30, "¥88"),
                ("quarterly_90d", 90, "¥238"),
                ("annual_365d", 365, "¥798"),
            ],
        )

    def test_only_configured_https_purchase_url_is_returned(self) -> None:
        config = {
            "licensing": {
                "purchase_urls": {
                    "trial_1d": "https://shop.example.test/trial",
                    "monthly_30d": "http://unsafe.example.test/monthly",
                }
            }
        }
        self.assertEqual(
            purchase_url(config, "trial_1d"),
            "https://shop.example.test/trial",
        )
        self.assertEqual(purchase_url(config, "monthly_30d"), "")
        self.assertEqual(purchase_url(config, "annual_365d"), "")


if __name__ == "__main__":
    unittest.main()

