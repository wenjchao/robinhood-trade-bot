"""rebalance.decide() 的單元測試。

執行方式（必須從專案根目錄 trade_bot/ 跑，因為要 import 到上一層的 rebalance.py）：
    uv run python -m unittest sample_data.test_rebalance -v

或直接執行此檔：
    uv run python sample_data/test_rebalance.py
"""

from __future__ import annotations

import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

# 把專案根目錄（這個檔案的上一層）加進 sys.path，這樣才能 import rebalance.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rebalance import (  # noqa: E402
    SOXL, SGOV, decide, parse_cash, parse_positions, parse_quotes,
)

SAMPLES = Path(__file__).parent

# 樣本 quotes_live.json 裡的 last_trade_price（用來算測試的預期值）
P_SOXL = 229.61
P_SGOV = 100.60


def _load(positions_file: str, portfolio_file: str = "portfolio_no_cash.json",
          quotes_file: str = "quotes_live.json"):
    positions = parse_positions(json.loads((SAMPLES / positions_file).read_text()))
    quotes = parse_quotes(json.loads((SAMPLES / quotes_file).read_text()))
    cash = parse_cash(json.loads((SAMPLES / portfolio_file).read_text()))
    return decide(positions, quotes, cash)


def _orders_by_side(d):
    return {(o.side, o.symbol): o for o in d.orders}


class TestDecide(unittest.TestCase):
    def test_empty_account_no_cash_is_idle(self):
        """完全空帳戶 → idle（什麼都不做）。"""
        d = _load("positions_empty.json", "portfolio_no_cash.json")
        self.assertEqual(d.action, "idle")
        self.assertEqual(d.orders, [])

    def test_empty_account_with_cash_deploys_1_to_1(self):
        """有 $10k 現金、沒持倉 → 平分買 SOXL + SGOV。"""
        d = _load("positions_empty.json", "portfolio_10k_cash.json")
        self.assertEqual(d.action, "rebalance")
        self.assertEqual(d.target_soxl, Decimal("5000"))
        self.assertEqual(d.target_sgov, Decimal("5000"))
        orders = _orders_by_side(d)
        self.assertEqual(orders[("buy", SOXL)].dollars, Decimal("5000"))
        self.assertEqual(orders[("buy", SGOV)].dollars, Decimal("5000"))
        self.assertNotIn(("sell", SOXL), orders)
        self.assertNotIn(("sell", SGOV), orders)

    def test_in_band_no_cash_holds(self):
        """比例在 [0.9524, 1.05] 帶內、沒現金 → hold。

        50 SOXL × 229.61 = 11480.50, 114 SGOV × 100.60 = 11468.40
        ratio ≈ 1.001 → in band ✓
        """
        d = _load("positions_in_band.json", "portfolio_no_cash.json")
        self.assertEqual(d.action, "hold")
        self.assertGreater(d.ratio, Decimal("0.9524"))
        self.assertLess(d.ratio, Decimal("1.05"))
        self.assertEqual(d.orders, [])

    def test_in_band_with_cash_still_holds(self):
        """比例在帶內、有現金 → 仍 hold（嚴格規則：閒置現金不自動部署）。"""
        d = _load("positions_in_band.json", "portfolio_10k_cash.json")
        self.assertEqual(d.action, "hold")
        self.assertIn("idle", d.reason.lower())

    def test_soxl_high_no_cash_sells_soxl_buys_sgov(self):
        """SOXL 過重（ratio > 1.05）、沒現金 → 賣 SOXL、買 SGOV。

        60 SOXL × 229.61 = 13776.60, 100 SGOV × 100.60 = 10060
        ratio ≈ 1.369 → out of band
        total = 23836.60, target = 11918.30 (each)
        sell SOXL: 13776.60 - 11918.30 = 1858.30 → qty ≈ 8.094
        buy SGOV: 11918.30 - 10060 = 1858.30
        """
        d = _load("positions_soxl_high.json", "portfolio_no_cash.json")
        self.assertEqual(d.action, "rebalance")
        self.assertGreater(d.ratio, Decimal("1.05"))
        orders = _orders_by_side(d)
        self.assertAlmostEqual(float(orders[("sell", SOXL)].quantity),
                               1858.30 / P_SOXL, places=3)
        self.assertAlmostEqual(float(orders[("buy", SGOV)].dollars), 1858.30, places=1)
        self.assertNotIn(("sell", SGOV), orders)

    def test_sgov_high_with_cash_buys_both_no_sell(self):
        """SGOV 過重（ratio < 0.9524）但帳上有 $10k 現金 → 兩腿都買，不用賣。

        40 SOXL × 229.61 = 9184.40, 100 SGOV × 100.60 = 10060, cash = 10000
        ratio ≈ 0.913 → out of band ✓
        total = 29244.40, target = 14622.20 (each)
        buy SOXL: 14622.20 - 9184.40 = 5437.80
        buy SGOV: 14622.20 - 10060 = 4562.20
        sum = 10000 = cash ✓
        """
        d = _load("positions_sgov_high.json", "portfolio_10k_cash.json")
        self.assertEqual(d.action, "rebalance")
        self.assertLess(d.ratio, Decimal("0.9524"))
        orders = _orders_by_side(d)
        self.assertAlmostEqual(float(orders[("buy", SOXL)].dollars), 5437.80, places=2)
        self.assertAlmostEqual(float(orders[("buy", SGOV)].dollars), 4562.20, places=2)
        self.assertNotIn(("sell", SOXL), orders)
        self.assertNotIn(("sell", SGOV), orders)


if __name__ == "__main__":
    unittest.main()
