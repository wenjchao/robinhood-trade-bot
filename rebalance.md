# 再平衡策略

`rebalance.py` 的策略規格。改策略時更新這份。

---

## 規則

維持 `SOXL:SGOV = 1:1`（依美金市值），出帶就再平衡。

| 條件 | action |
|---|---|
| `vr + vs + cash == 0` | `idle` |
| 兩腿都有貨 且 `LOWER_BAND ≤ vr/vs ≤ UPPER_BAND` | `hold` |
| 兩腿都有貨 且 `vr/vs > UPPER_BAND` | `rebalance` — 賣 SOXL、買 SGOV |
| 兩腿都有貨 且 `vr/vs < LOWER_BAND` | `rebalance` — 賣 SGOV、買 SOXL |
| 任一腿為 0（含初次建倉） | `rebalance` |

> `vr` = SOXL 總值，`vs` = SGOV 總值。

再平衡時兩腿目標（含現金一併部署）：

```
total = vr + vs + cash
target_soxl = total × 1/2
target_sgov = total × 1/2
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
（成交價就是當下 bid/ask）。SOXL/SGOV 在 regular hours 內 spread <0.1%，
滑價極小（SOXL 偶有極端波動，例外處理時請注意）。

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
| `TARGET_RATIO` | `1`（SOXL:SGOV 目標市值比） | `rebalance.py` |
| `BAND_FACTOR` | `1.05`（±5% 觸發帶） | `rebalance.py` |
| `UPPER_BAND` | `TARGET_RATIO × BAND_FACTOR` = `1.05` | `rebalance.py` |
| `LOWER_BAND` | `TARGET_RATIO / BAND_FACTOR` ≈ `0.9524` | `rebalance.py` |
| `WEIGHT_SOXL` | `TARGET_RATIO / (TARGET_RATIO + 1)` = `1/2` | `rebalance.py` |
| `WEIGHT_SGOV` | `1 / (TARGET_RATIO + 1)` = `1/2` | `rebalance.py` |
| `ORDER_TYPE` | `"market"`（預設）/ `"limit"` | `main_bot.py` |
| `LIMIT_SLIP` | `0.005`（0.5%） | `main_bot.py`（只在 limit 模式用）|
| `MIN_ORDER_USD` | `1.00` | `main_bot.py` |
| 標的 | `SOXL`, `SGOV` | `rebalance.py` |
| 帳戶 | `RH_AGENTIC_ACCOUNT` 環境變數（agentic sub-account） | `main_bot.py` + `.env` |

---

## 注意

- **比例在帶內時，現金不會自動部署**。再入金但比例 1:1 → 現金躺到下次出帶。
- 再平衡完成後恆等式：`sum(buy 金額) − sum(sell 金額) = cash`。對不上代表 bug。
- 同一次再平衡不可能兩腿都 sell（推導矛盾）；但可能兩腿都 buy（cash 足夠時）。
- SOXL = Direxion Daily Semiconductor Bull 3X Shares。3 倍槓桿 → 高波動、有衰減。

---

## 版本紀錄

| 日期 | 變更 |
|---|---|
| 2026-06-18 | 初版：1:1、±5% 帶、現金併入 target |
| 2026-06-19 | 加 `ORDER_TYPE` 切換；預設 `"market"` 支援 fractional 股、現金完全部署；`"limit"` 路徑保留 |
| 2026-06-23 | SGOV → VBIL（嘗試解 EQUITY_SUITABILITY）；比例 1:1 → 2:1；加 `MIN_ORDER_USD = $1` 過濾 |
| 2026-06-23 | 使用者填完 suitability 表單，broker 解禁；改回 SGOV、改用 SOXL；策略改為 `SOXL:SGOV = 1:1` |
