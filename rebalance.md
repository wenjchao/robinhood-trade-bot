# 再平衡策略

`rebalance.py` 的策略規格。改策略時更新這份。

---

## 規則

維持 `TQQQ:SGOV = 1:1`（依美金市值），出帶就再平衡。

| 條件 | action |
|---|---|
| `vt + vs + cash == 0` | `idle` |
| 兩腿都有貨 且 `LOWER_BAND ≤ vt/vs ≤ UPPER_BAND` | `hold` |
| 兩腿都有貨 且 `vt/vs > UPPER_BAND` | `rebalance` — 賣 TQQQ、買 SGOV |
| 兩腿都有貨 且 `vt/vs < LOWER_BAND` | `rebalance` — 賣 SGOV、買 TQQQ |
| 任一腿為 0（含初次建倉） | `rebalance` |

再平衡時兩腿目標：

```
target = (vt + vs + cash) / 2
```

對每腿：`v > target → 賣` / `v < target → 買` / `v == target → 不動`。
現金一併部署 → 再平衡後現金歸零。

---

## 下單方式（`main_bot.py`）

Marketable limit + 整數股：

```
sell_limit = floor_cents(bid × (1 − LIMIT_SLIP))
buy_limit  = ceil_cents (ask × (1 + LIMIT_SLIP))
sell_shares = floor(quantity)
buy_shares  = floor(dollars / buy_limit)
```

股數 round 為 0 的單 → 跳過。bid/ask 為 0（盤外）→ 退用 `last_trade_price`。

---

## 常數

| | 值 | 位置 |
|---|---|---|
| `UPPER_BAND` | `1.05` | `rebalance.py` |
| `LOWER_BAND` | `1 / 1.05` ≈ 0.9524 | `rebalance.py` |
| `LIMIT_SLIP` | `0.005`（0.5%） | `main_bot.py` |
| 標的 | `TQQQ`, `SGOV` | `rebalance.py` |
| 帳戶 | `RH_AGENTIC_ACCOUNT` 環境變數（agentic sub-account） | `main_bot.py` + `.env` |

---

## 注意

- **比例在帶內時，現金不會自動部署**。再入金但比例 1:1 → 現金躺到下次出帶。
- 再平衡完成後恆等式：`sum(buy 金額) − sum(sell 金額) = cash`。對不上代表 bug。
- 同一次再平衡不可能兩腿都 sell（推導矛盾）；但可能兩腿都 buy（cash 足夠時）。

---

## 版本紀錄

| 日期 | 變更 |
|---|---|
| 2026-06-18 | 初版：1:1、±5% 帶、現金併入 target |
