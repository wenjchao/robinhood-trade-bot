"""TQQQ/VBIL 2:1 再平衡演算法 — Phase 1：純邏輯，不碰網路。

這支檔案只負責「決定」：拿到帳戶資料就回傳「該做什麼」。
不認識 Robinhood、不會送單、不會碰 OAuth。
可以單獨用 sample_data/ 裡的測試 JSON 跑來驗證邏輯，
也會被 main_bot.py（Phase 2）呼叫去處理真實的 MCP 回傳。

完整的策略文件請見 rebalance.md（跟這支檔案同名，配對閱讀）。

策略規則：
  1. 永遠維持 TQQQ 總值 : VBIL 總值 = 2:1
  2. 比例（vt/vs）超出 [TARGET_RATIO / 1.05, TARGET_RATIO × 1.05] 就再平衡
     ≈ [1.905, 2.10]，等效於「比例偏離目標 ±5%」
  3. 任一腿是空的也算「需要再平衡」(初次建倉自動處理)
  4. 再平衡時：
       target_tqqq = total × 2/3
       target_vbil = total × 1/3
     其中 total = 現金 + TQQQ 總值 + VBIL 總值
     現金在這時會被一起部署 → 達成 2:1 後現金歸零
  5. 比例在帶內時，現金不會自動投入（嚴格照使用者規則）

執行範例（搭配 sample_data/ 裡的測試資料）：
    uv run rebalance.py \
        --positions sample_data/positions_tqqq_high.json \
        --quotes    sample_data/quotes_live.json \
        --portfolio sample_data/portfolio_10k_cash.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from decimal import Decimal  # 金錢計算一律用 Decimal，禁止 float（浮點誤差會累積）
from pathlib import Path
from typing import Any

# 股票代號常數
# TQQQ：3 倍槓桿那斯達克 100 ETF（進攻腿）
# VBIL：Vanguard 0-3 個月短期美國國債 ETF（防守腿）
TQQQ = "TQQQ"
VBIL = "VBIL"

# 目標市值比 = vt / vs
TARGET_RATIO = Decimal("2")

# 觸發帶：比例偏離目標 ±5%
# UPPER_BAND: vt/vs > TARGET_RATIO × 1.05 → 觸發，要砍 TQQQ
# LOWER_BAND: vt/vs < TARGET_RATIO / 1.05 → 觸發，要砍 VBIL
# 為什麼用 ÷1.05 而非 ×0.95：保持對數對稱
BAND_FACTOR = Decimal("1.05")
UPPER_BAND = TARGET_RATIO * BAND_FACTOR     # = 2.10
LOWER_BAND = TARGET_RATIO / BAND_FACTOR     # ≈ 1.905

# 目標權重（總值的多少比例分給每一腿）
# 1 份 + 2 份 = 3 份，所以 TQQQ 占 2/3，VBIL 占 1/3
WEIGHT_TQQQ = TARGET_RATIO / (TARGET_RATIO + Decimal("1"))   # = 2/3
WEIGHT_VBIL = Decimal("1") / (TARGET_RATIO + Decimal("1"))   # = 1/3


# ─── 資料結構（dataclass，純儲存用） ────────────────────────────────────

@dataclass
class Position:
    """一筆持倉。

    quantity  - 帳戶名義上有的股數
    sellable  - 此刻可以「賣」的股數（Robinhood 會因結算未完成而暫時鎖住一部份）
                我們算總值用 quantity；Phase 3 真實下單時要用 sellable 防超賣
    """
    symbol: str
    quantity: Decimal
    sellable: Decimal


@dataclass
class Quote:
    """單一股票的即時報價。"""
    symbol: str
    last_trade_price: Decimal  # 最近一筆成交價，當「現價」用
    bid_price: Decimal         # 買方願意付的最高價（賣方要快速成交就掛在這附近）
    ask_price: Decimal         # 賣方願意收的最低價（買方要快速成交就掛在這附近）


@dataclass
class Order:
    """抽象的單張交易計畫（dollar 概念，還不是真的可送出的單）。

    side == "sell"：quantity 有值 — 要賣多少股（Decimal，會有小數）
    side == "buy" ：dollars  有值 — 要花多少錢買（會在 live.py 轉成股數）
    """
    side: str
    symbol: str
    quantity: Decimal | None = None
    dollars: Decimal | None = None


@dataclass
class Decision:
    """decide() 的回傳：這次該做什麼。

    action: "hold"（不動）/ "rebalance"（要動）/ "idle"（帳戶完全空，沒事做）
    """
    action: str
    ratio: Decimal | None             # TQQQ總值/VBIL總值；任一腿為 0 時為 None
    tqqq_value: Decimal
    vbil_value: Decimal
    cash: Decimal
    target_tqqq: Decimal | None       # 再平衡時 TQQQ 的目標金額（total × 2/3）
    target_vbil: Decimal | None       # 再平衡時 VBIL 的目標金額（total × 1/3）
    orders: list[Order] = field(default_factory=list)
    reason: str = ""


# ─── Robinhood JSON 解析（把 MCP 回的 dict 變成上面的 dataclass） ─────────

def parse_positions(payload: dict[str, Any]) -> dict[str, Position]:
    """把 get_equity_positions 的回傳變成 {symbol: Position}。

    payload 結構：{"data": {"positions": [{...}, {...}, ...]}}
    每筆 position 至少有 symbol、quantity 兩個欄位。
    """
    out: dict[str, Position] = {}
    for p in payload["data"]["positions"]:
        sym = p["symbol"]
        # Robinhood 回的是字串（"100.000000"），轉 Decimal 才能算
        qty = Decimal(str(p["quantity"]))
        # shares_available_for_sells 不一定存在；缺則退回 quantity
        sellable = Decimal(str(p.get("shares_available_for_sells", p["quantity"])))
        out[sym] = Position(symbol=sym, quantity=qty, sellable=sellable)
    return out


def parse_quotes(payload: dict[str, Any]) -> dict[str, Quote]:
    """把 get_equity_quotes 的回傳變成 {symbol: Quote}。

    安全檢查：拒絕停牌（state != "active"）或從未成交的標的，
    避免拿到死掉的報價算出錯誤的決策。
    """
    out: dict[str, Quote] = {}
    for r in payload["data"]["results"]:
        q = r["quote"]
        sym = q["symbol"]
        if not q.get("has_traded", True) or q.get("state") != "active":
            # 寧可炸掉，也不要用無效報價交易
            raise RuntimeError(
                f"{sym} not tradable: has_traded={q.get('has_traded')} state={q.get('state')}"
            )
        out[sym] = Quote(
            symbol=sym,
            last_trade_price=Decimal(q["last_trade_price"]),
            # bid/ask 可能是 None 或 "0"；後者代表沒有掛單
            bid_price=Decimal(q.get("bid_price") or "0"),
            ask_price=Decimal(q.get("ask_price") or "0"),
        )
    return out


def parse_cash(payload: dict[str, Any]) -> Decimal:
    """從 get_portfolio 回傳裡取「能花的錢」。

    優先用 buying_power.buying_power（broker 認可的「現在可花」），
    而不是 cash 欄位 —— cash 含結算中的金額，可能比實際可花高。
    """
    bp = payload["data"].get("buying_power", {})
    if "buying_power" in bp:
        return Decimal(str(bp["buying_power"]))
    # 退路：舊版或部分回應沒有 buying_power 物件
    return Decimal(str(payload["data"].get("cash", "0")))


# ─── 策略大腦 ───────────────────────────────────────────────────────────

def decide(
    positions: dict[str, Position],
    quotes: dict[str, Quote],
    cash: Decimal,
) -> Decision:
    """核心決策函數。輸入帳戶現況，輸出該做什麼。

    流程：
      1. 算出 TQQQ 和 VBIL 各自的「市值」(vt, vs)
      2. 判斷是否觸發再平衡（兩腿都 > 0 才看 ratio；任一腿是 0 直接觸發）
      3. 若不觸發 → 回傳 hold
      4. 若觸發 → 算每腿目標金額（含現金、加上 2:1 權重）→ 產生 sell/buy 計畫
    """
    # 從 positions 字典取股數；沒持倉的代號用 0 處理
    qt = positions.get(TQQQ, Position(TQQQ, Decimal(0), Decimal(0))).quantity
    qs = positions.get(VBIL, Position(VBIL, Decimal(0), Decimal(0))).quantity
    # 用最近成交價算「現在的市值」；買賣的限價會在 main_bot.py 另外算
    pt = quotes[TQQQ].last_trade_price
    ps = quotes[VBIL].last_trade_price
    vt = qt * pt   # TQQQ 的當前總值
    vs = qs * ps   # VBIL 的當前總值

    # 判斷是否觸發再平衡
    # 只有兩腿都 > 0 的時候才能算 ratio（避免除以 0）
    if vt > 0 and vs > 0:
        ratio: Decimal | None = vt / vs
        out_of_band = ratio > UPPER_BAND or ratio < LOWER_BAND
    else:
        # 任一腿空 → 視為「需要再平衡」(初次建倉或極端漂移)
        ratio = None
        out_of_band = True

    # 帳戶完全空（沒股票、沒現金）→ 真的沒事可做
    if vt + vs + cash == 0:
        return Decision("idle", None, vt, vs, cash, None, None, [], "Account is empty.")

    # 比例在帶內 → hold
    # 注意：即使帳上有現金，只要兩腿比例在目標附近，按使用者規則就是不動
    # （想要「閒置現金自動部署」要改規則 — 目前嚴格照原指示）
    if not out_of_band:
        reason = (f"ratio {ratio:.4f} within [{LOWER_BAND:.4f}, {UPPER_BAND}] "
                  f"(target {TARGET_RATIO}) — no action.")
        if cash > 0:
            reason += f" (Note: ${cash:,.2f} cash sits idle — strategy says don't touch it.)"
        return Decision("hold", ratio, vt, vs, cash, None, None, [], reason)

    # ─── 進入再平衡分支 ───
    # TQQQ 占 2/3、VBIL 占 1/3 的全部帳戶價值（含現金）
    # 把 cash 算進去 → 再平衡這一刻，現金全部投入，達成 2:1 後現金歸零
    total = vt + vs + cash
    target_tqqq = total * WEIGHT_TQQQ
    target_vbil = total * WEIGHT_VBIL
    orders: list[Order] = []

    # TQQQ：跟自己 target 比，太多就賣、太少就買；剛好就不動
    if vt > target_tqqq:
        sell_dollars = vt - target_tqqq
        # sell_dollars / pt = 要賣多少股；整數化／市價/限價在 main_bot.py 處理
        orders.append(Order("sell", TQQQ, quantity=sell_dollars / pt))
    elif vt < target_tqqq:
        orders.append(Order("buy", TQQQ, dollars=target_tqqq - vt))

    # VBIL：同上邏輯，獨立判斷
    # 邏輯上 TQQQ 和 VBIL 不可能同時 sell（兩腿都 > target 代表總值算錯）
    # 但可能兩腿同時 buy（cash 大到讓兩腿目前都低於 target），或一買一賣
    if vs > target_vbil:
        sell_dollars = vs - target_vbil
        orders.append(Order("sell", VBIL, quantity=sell_dollars / ps))
    elif vs < target_vbil:
        orders.append(Order("buy", VBIL, dollars=target_vbil - vs))

    if ratio is None:
        reason = f"One or both legs empty — deploy cash to reach {TARGET_RATIO}:1."
    else:
        reason = (f"ratio {ratio:.4f} outside [{LOWER_BAND:.4f}, {UPPER_BAND}] "
                  f"— rebalance to {TARGET_RATIO}:1.")
    return Decision("rebalance", ratio, vt, vs, cash, target_tqqq, target_vbil,
                    orders, reason)


# ─── 結果顯示 ───────────────────────────────────────────────────────────

def render(d: Decision) -> str:
    """把 Decision 轉成方便人看的多行字串。"""
    ratio_str = f"{d.ratio:.4f}" if d.ratio is not None else "n/a"
    lines = [
        f"TQQQ value:     ${d.tqqq_value:,.2f}",
        f"VBIL value:     ${d.vbil_value:,.2f}",
        f"Cash:           ${d.cash:,.2f}",
        f"Total:          ${d.tqqq_value + d.vbil_value + d.cash:,.2f}",
        f"Ratio (T/V):    {ratio_str}  (target {TARGET_RATIO})",
        f"Action:         {d.action}",
        f"Reason:         {d.reason}",
    ]
    if d.target_tqqq is not None and d.target_vbil is not None:
        lines.append(f"Target TQQQ:    ${d.target_tqqq:,.2f}")
        lines.append(f"Target VBIL:    ${d.target_vbil:,.2f}")
    for o in d.orders:
        if o.side == "sell":
            assert o.quantity is not None
            lines.append(f"  SELL {o.quantity:.6f} {o.symbol}")
        else:
            assert o.dollars is not None
            lines.append(f"  BUY  ${o.dollars:,.2f} of {o.symbol}")
    return "\n".join(lines)


# ─── CLI 入口（用 JSON 檔離線跑） ───────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    """命令列入口：吃三個 JSON 檔，印出決策。"""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--positions", required=True, type=Path,
                    help="JSON from get_equity_positions")
    ap.add_argument("--quotes", required=True, type=Path,
                    help="JSON from get_equity_quotes (必須含 TQQQ 和 VBIL)")
    ap.add_argument("--portfolio", required=True, type=Path,
                    help="JSON from get_portfolio (用來取現金/買力)")
    args = ap.parse_args(argv)

    positions = parse_positions(json.loads(args.positions.read_text()))
    quotes = parse_quotes(json.loads(args.quotes.read_text()))
    cash = parse_cash(json.loads(args.portfolio.read_text()))

    # 安全檢查：兩支股票的報價都必須存在
    for sym in (TQQQ, VBIL):
        if sym not in quotes:
            print(f"error: quotes file missing {sym}", file=sys.stderr)
            return 2

    print(render(decide(positions, quotes, cash)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
