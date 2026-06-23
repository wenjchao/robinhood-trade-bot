# 再平衡策略

`rebalance.py` 的策略規格。改策略時更新這份。

---

## 規則

維持 `TQQQ:VBIL = 2:1`（依美金市值），出帶就再平衡。

| 條件 | action |
|---|---|
| `vt + vs + cash == 0` | `idle` |
| 兩腿都有貨 且 `LOWER_BAND ≤ vt/vs ≤ UPPER_BAND` | `hold` |
| 兩腿都有貨 且 `vt/vs > UPPER_BAND` | `rebalance` — 賣 TQQQ、買 VBIL |
| 兩腿都有貨 且 `vt/vs < LOWER_BAND` | `rebalance` — 賣 VBIL、買 TQQQ |
| 任一腿為 0（含初次建倉） | `rebalance` |

再平衡時兩腿目標（含現金一併部署）：

```
total = vt + vs + cash
target_tqqq = total × 2/3
target_vbil = total × 1/3
```

對每腿：`v > target → 賣` / `v < target → 買` / `v == target → 不動`。
現金一併部署 → 再平衡後現金歸零。

---

## 下單方式（`main_bot.py`）

由 `ORDER_TYPE` 常數切換，兩條路徑都在程式碼裡。

### `ORDER_TYPE = "market"`（目前預設）

```
賣：type=market, quantity = floor(quantity, 6 位小數)
買：type=market, dollar_amount = floor(dollars, 分位)
```

允許 fractional 股，精準到 6 位小數，現金完全部署不殘留。代價是無價格保護
（成交價就是當下 bid/ask）。TQQQ/VBIL 在 regular hours 內 spread <0.1%，
滑價極小。

### `ORDER_TYPE = "limit"`（備援路徑，可隨時切回）

```
sell_limit = floor_cents(bid × (1 − LIMIT_SLIP))
buy_limit  = ceil_cents (ask × (1 + LIMIT_SLIP))
sell_shares = floor(quantity)              # 整數股
buy_shares  = floor(dollars / buy_limit)   # 整數股
```

整數股 + 「最差成交價」保護。代價：不到 1 股的訂單會被跳過，小金額無法部署。
股數 round 為 0 的單 → 跳過。bid/ask 為 0（盤外）→ 退用 `last_trade_price`。

### 最小訂單金額過濾

兩條路徑都套用 `MIN_ORDER_USD = $1.00`：估算金額 < $1 的單一律跳過。原因：
- Robinhood market 模式的 `dollar_amount` 最小 $1（不過會回 `EQUITY_DOLLAR_BASED_MINIMUM_AMOUNT_ERROR`）
- 避免在「已經幾乎平衡、只差幾分錢」的情境下浪費 review

---

## 常數

| | 值 | 位置 |
|---|---|---|
| `TARGET_RATIO` | `2`（TQQQ:VBIL 目標市值比） | `rebalance.py` |
| `BAND_FACTOR` | `1.05`（±5% 觸發帶） | `rebalance.py` |
| `UPPER_BAND` | `TARGET_RATIO × BAND_FACTOR` = `2.10` | `rebalance.py` |
| `LOWER_BAND` | `TARGET_RATIO / BAND_FACTOR` ≈ `1.905` | `rebalance.py` |
| `WEIGHT_TQQQ` | `TARGET_RATIO / (TARGET_RATIO + 1)` = `2/3` | `rebalance.py` |
| `WEIGHT_VBIL` | `1 / (TARGET_RATIO + 1)` = `1/3` | `rebalance.py` |
| `ORDER_TYPE` | `"market"`（預設）/ `"limit"` | `main_bot.py` |
| `LIMIT_SLIP` | `0.005`（0.5%） | `main_bot.py`（只在 limit 模式用）|
| `MIN_ORDER_USD` | `1.00` | `main_bot.py` |
| 標的 | `TQQQ`, `VBIL` | `rebalance.py` |
| 帳戶 | `RH_AGENTIC_ACCOUNT` 環境變數（agentic sub-account） | `main_bot.py` + `.env` |

---

## 注意

- **比例在帶內時，現金不會自動部署**。再入金但比例 2:1 → 現金躺到下次出帶。
- 再平衡完成後恆等式：`sum(buy 金額) − sum(sell 金額) = cash`。對不上代表 bug。
- 同一次再平衡不可能兩腿都 sell（推導矛盾）；但可能兩腿都 buy（cash 足夠時）。
- VBIL = Vanguard 0-3 Month Treasury ETF。會從 SGOV 換是因為 Agentic sub-account
  對 SGOV 跳 `EQUITY_SUITABILITY` alert，VBIL 是 Vanguard 等價物，預期無此限制。

---

## 版本紀錄

| 日期 | 變更 |
|---|---|
| 2026-06-18 | 初版：1:1、±5% 帶、現金併入 target |
| 2026-06-19 | 加 `ORDER_TYPE` 切換；預設 `"market"` 支援 fractional 股、現金完全部署；`"limit"` 路徑保留 |
| 2026-06-23 | SGOV → VBIL（解 EQUITY_SUITABILITY）；比例 1:1 → 2:1（依市值，TQQQ 攻擊腿 2 份、VBIL 防守腿 1 份）；加 `MIN_ORDER_USD = $1` 過濾 |
