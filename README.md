# trade_bot

TQQQ / SGOV 1:1 自動再平衡，透過 Robinhood Agentic Trading MCP。

---

## 這是在做什麼

維持帳戶裡 TQQQ（3 倍槓桿 NASDAQ 100 ETF）和 SGOV（短期美國國債 ETF）的「總值」是 1:1，當比例偏離過大時自動再平衡回去。

策略規則的快速摘要請見下面表格；完整的邏輯細節、設計選擇、邊界情況、修改指南，請看 **[rebalance.md](rebalance.md)**。

### 策略規則（摘要）

| 條件 | 動作 |
|---|---|
| TQQQ 總值 / SGOV 總值 在 `[0.9524, 1.05]` 內 | 不動（即使帳上有閒置現金也不部署）|
| 比例 > 1.05 | 賣 TQQQ、買 SGOV |
| 比例 < 0.9524 | 賣 SGOV、買 TQQQ |
| 任一腿是空的 | 視為「需要再平衡」，把現金一起部署 |

再平衡時兩腿的目標金額 = `(現金 + TQQQ 總值 + SGOV 總值) / 2`。
也就是說 —— 再平衡會把所有現金一起投入，達到 1:1 後現金歸零。

下單方式：marketable limit 單（賣單掛 bid × 0.995，買單掛 ask × 1.005），整數股。

---

## 開發階段

| 階段 | 狀態 | 內容 |
|---|---|---|
| Phase 1 | ✅ 完成 | 純策略邏輯，用 sample_data/ 離線測試 |
| Phase 2 | ✅ 完成 | 接 Robinhood MCP，跑到 `review_equity_order` 為止（不下單）|
| Phase 3a | ✅ 完成 | `--execute` 旗標，真的呼叫 `place_equity_order`（含開放單安全檢查）|
| Phase 3b | ✅ 完成 | GitHub Actions 排程（每日三時段），須設好 GH Secrets 才會生效 |

---

## 檔案結構

```
trade_bot/
├── README.md               ← 你正在看的這份（概觀 + 使用方法）
├── rebalance.md            ← 策略邏輯文件（要改策略先讀這份）
├── pyproject.toml          ← uv 的專案設定（Python 版本、依賴清單）
├── uv.lock                 ← uv 鎖定的精確版本（自動產生，不要手改）
├── .gitignore              ← 哪些檔不要進 git
├── .env.example            ← 環境變數模板（複製成 .env 並填值）
│
├── .github/
│   └── workflows/
│       └── rebalance.yml    ← GitHub Actions 排程（每日三時段自動跑）
│
├── rebalance.py            ← 【純策略邏輯】吃 JSON 吐決策，不碰網路
├── mcp_client.py           ← 【MCP 連線】OAuth、token 快取、session 管理
├── main_bot.py             ← 【主程式】串起上面兩個，連 Robinhood 跑一次
│
├── sample_data/             ← 測試資料 + 單元測試
│   ├── test_rebalance.py     ← rebalance.py 的單元測試
│   ├── quotes_live.json
│   ├── portfolio_live.json
│   ├── portfolio_10k_cash.json
│   ├── portfolio_no_cash.json
│   ├── positions_empty.json
│   ├── positions_in_band.json
│   ├── positions_tqqq_high.json
│   └── positions_sgov_high.json
│
├── .env                    ← (你自己建) 帳號等本機設定，chmod 600，已 gitignore
├── .token.json             ← (自動產生) OAuth 憑證，chmod 600，已 gitignore
├── .venv/                  ← (自動產生) uv 管理的虛擬環境
└── __pycache__/            ← (自動產生) Python bytecode 快取
```

---

## 環境準備

需要 macOS 上的 `uv`：

```bash
# 如果還沒裝
brew install uv

# 進入專案 → 安裝依賴
cd <你的 trade_bot 路徑>
uv sync
```

`uv sync` 會根據 `uv.lock` 把 mcp、httpx 等依賴裝進 `.venv/`。
不需要手動 activate venv —— 用 `uv run` 自動帶起來。

### 環境變數

複製模板並填上你的 Agentic sub-account 編號：

```bash
cp .env.example .env
chmod 600 .env
# 編輯 .env，把 RH_AGENTIC_ACCOUNT= 後面加上你的帳號
```

`.env` 已加進 `.gitignore`，**絕對不會進版控**。

---

## 怎麼使用

### 1. 離線測試策略邏輯（完全不碰真實帳戶）

跑單元測試：

```bash
uv run python -m unittest sample_data.test_rebalance -v
```

或手動用樣本資料跑一次：

```bash
# 例：TQQQ 過重 + 無現金 → 該賣 TQQQ 買 SGOV
uv run rebalance.py \
    --positions sample_data/positions_tqqq_high.json \
    --quotes    sample_data/quotes_live.json \
    --portfolio sample_data/portfolio_no_cash.json
```

### 2. 真實連線測試（review-only，不會下單）

```bash
uv run main_bot.py
```

**第一次跑會**：
1. 開瀏覽器 → 跳到 Robinhood 登入
2. 你登入後 → 看到「授權 trade_bot rebalancer」的頁面 → 按同意
3. 瀏覽器跳回 `localhost:33418/callback`（會看到「Authorization received」）
4. 終端機印出帳戶現況、決策、review 結果
5. `.token.json` 被建立（之後每次自動沿用）

**之後每次跑**：直接讀 `.token.json`，不彈瀏覽器。
access_token 過期了會自動用 refresh_token 換新的。

**如果帳戶錢太少（少於 1 股價）**：策略會說「沒有可執行的整數股訂單」。
想驗證 review API 的串接，加 `--smoke-review`：

```bash
uv run main_bot.py --smoke-review
```

會合成一張一定不會成交的假單去測 review，純為了確認 broker 端的串接 OK。

### 3. 真的下單（不可逆，慎用）

```bash
uv run main_bot.py --execute
```

對每張具體訂單的處理流程：
1. 先呼叫 `review_equity_order` 模擬
2. 若回傳的 `order_checks` **是空的**（無警示）→ 呼叫 `place_equity_order` 真的送單
3. 若 `order_checks` **非空**（任何警示）→ 拒絕這張單，印出警示，繼續下一張

**整輪跳過的情況**：執行前若發現 TQQQ 或 SGOV 已有未成交單（state ∈ {new, queued, confirmed, unconfirmed, partially_filled}），整輪不送任何單。這避免 cron 連續觸發時下重複單。

**互斥**：`--execute` 跟 `--smoke-review` 不能同時用（argparse 會擋）。

### 4. 重新授權

```bash
rm .token.json
uv run main_bot.py
```

下次跑會重新彈瀏覽器要你授權。

### 5. 在 GitHub Actions 自動排程跑（Phase 3b）

排程定義在 `.github/workflows/rebalance.yml`，預設每個交易日跑三次：

| 時間 (ET) | 用途 |
|---|---|
| 9:35 AM | 開盤後 5 分鐘（避開開盤波動）|
| 12:30 PM | 盤中 |
| 3:55 PM | 收盤前 5 分鐘 |

**啟用步驟**（必須先做完才會真的自動跑）：

#### 步驟 1：產生 base64 編碼的 token

在你本機 trade_bot/ 目錄裡跑（先確認 .token.json 存在且有效，例如本機剛跑過 `uv run main_bot.py`）：

```bash
base64 -i .token.json | pbcopy   # macOS 把 base64 字串複製到剪貼簿
```

#### 步驟 2：在 GitHub 設定兩個 Secret

到 GitHub repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**，新增兩個：

| Secret 名稱 | 值 |
|---|---|
| `RH_TOKEN_JSON_B64` | 上一步剪貼簿裡的 base64 字串 |
| `RH_AGENTIC_ACCOUNT` | 你的 Agentic sub-account 編號（同 `.env` 裡那個）|

#### 步驟 3：測試

到 GitHub repo → **Actions** 頁面 → 選 "TQQQ/SGOV rebalance" → **Run workflow** 手動觸發一次，看 log 是否成功。

#### DST（日光節約時間）轉換

GitHub cron 用 UTC。`rebalance.yml` 預設是 EDT（夏令）時間。冬令時間（11 月～3 月初）要把每個 cron 的小時 **+1**：

```yaml
# 夏令 (EDT, 3 月～11 月)
- cron: '35 13 * * 1-5'
- cron: '30 16 * * 1-5'
- cron: '55 19 * * 1-5'

# 冬令 (EST, 11 月～3 月)
- cron: '35 14 * * 1-5'
- cron: '30 17 * * 1-5'
- cron: '55 20 * * 1-5'
```

#### 萬一 GH Actions 連續失敗

最可能的原因：refresh_token 過期。修復：

```bash
rm .token.json
uv run main_bot.py             # 重新瀏覽器授權，產生新 .token.json
base64 -i .token.json | pbcopy # 複製新 base64
# 到 GitHub Secrets 更新 RH_TOKEN_JSON_B64 的值
```

---

## 安全注意事項

1. **`.token.json` 絕對不能進 git 或外洩**
   - 已加進 `.gitignore`，權限自動設成 `0600`
   - 等同於密碼，洩漏後別人可以操作你的 Robinhood Agentic 帳戶
   - 萬一洩漏：立刻到 Robinhood 撤銷 agent 授權

2. **下單能力是顯式 opt-in**
   - 不加旗標跑 → 只呼叫 `get_*` 和 `review_equity_order`，零下單
   - `--execute` 才會呼叫 `place_equity_order`
   - 任何 review 警示都會阻止該張單送出
   - 開放單存在時整輪跳過，防止重複下單

3. **Robinhood Agentic 帳戶是獨立的 sub-account**
   - 帳號存在 `.env`（`RH_AGENTIC_ACCOUNT`），`.env` 已加 `.gitignore`，不會進版控
   - 跟你主要的 brokerage 帳戶分開
   - agent 拿到 token 也只能操作這個 sub-account，動不到主帳戶
   - 想限制風險就少轉錢進來

4. **三倍槓桿 TQQQ 提醒**
   - TQQQ 是日重設的 3x 槓桿，長期持有有衰減（volatility decay）
   - 頻繁再平衡會吃稅（短期資本利得）
   - 1.05 的帶可能太窄，未來可能要調

---

## 故障排除

| 症狀 | 通常的原因 / 解法 |
|---|---|
| `Session termination failed: 400` | mcp SDK 的已知無害警告，可忽略 |
| 瀏覽器沒自動開 | 終端機會印出 URL，手動複製貼到瀏覽器即可 |
| `OAuth callback did not arrive within timeout` | 5 分鐘沒授權完。重跑就好 |
| `Port 33418 already in use` | 上次的 callback server 沒收乾淨。重開 terminal 或 `lsof -i :33418` 找出來 kill |
| review 一直回 `INSUFFICIENT_BUYING_POWER` | 帳戶錢不夠。轉錢進 Agentic sub-account |
| review 回 `NOT_REGULAR_HOURS` | 非交易時段（美東 9:30–16:00 以外）。等開盤再跑 |
| GH Actions 跑了但時間怪怪的 | DST 轉換了。改 `.github/workflows/rebalance.yml` 裡的 cron 小時數 ±1 |
| GH Actions 連續失敗、log 看到 401/403 | refresh_token 失效。本機重新授權後更新 `RH_TOKEN_JSON_B64` Secret |

---

## 主要程式碼導覽

每支 `.py` 檔開頭都有完整中文 docstring，這裡只列各檔角色：

- **`rebalance.py`** —— 純策略大腦。`decide(positions, quotes, cash)` 是核心；不認識網路。詳細的邏輯請看 [rebalance.md](rebalance.md)。
- **`mcp_client.py`** —— OAuth + 連線管理。`rh_session()` 給你一個已登入的 MCP session。
- **`main_bot.py`** —— 主程式。連 Robinhood、跑策略、review，視 `--execute` 決定要不要真送單。

想改策略 → 先讀 `rebalance.md`，再編 `rebalance.py`。
想改連線或 token 管理 → 編 `mcp_client.py`。
想改 CLI、限價計算、下單時機 → 編 `main_bot.py`。
