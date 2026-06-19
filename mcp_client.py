"""Robinhood Agentic MCP 連線工具（OAuth + token 快取）。

用法：
    async with rh_session() as session:
        result = await session.call_tool("get_portfolio", {"account_number": "..."})

第一次跑會做完整 OAuth 流程：
  1. 用 Dynamic Client Registration 跟 Robinhood 註冊我們這個應用（取得 client_id）
  2. 本機開一個 HTTP server 在 localhost:33418，用來接 OAuth 跳轉回來的網址
  3. 開瀏覽器讓使用者登入 Robinhood、授權我們的應用
  4. 瀏覽器把 authorization code 送回 localhost:33418/callback
  5. 我們拿 code 去換 access_token + refresh_token
  6. 寫入 .token.json（chmod 600，只有檔案擁有者讀得到）

之後每次跑會直接讀 .token.json：
  - access_token 還有效 → 直接用
  - 過期了 → 用 refresh_token 自動換新的
  - refresh_token 也壞了 → 重新開瀏覽器（極少發生）

安全注意：
  - .token.json 是「等同密碼」的東西，已加入 .gitignore，絕對不可進版控
  - 對外公開帳戶會被有心人盜走，後果嚴重
  - 改帳戶或想重新授權：rm .token.json 即可
"""

from __future__ import annotations

import asyncio
import json
import threading
import webbrowser
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import parse_qs, urlparse

from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

# Robinhood Agentic Trading MCP 的固定端點
MCP_URL = "https://agent.robinhood.com/mcp/trading"

# token 快取檔的位置（跟這支腳本同目錄）
TOKEN_FILE = Path(__file__).parent / ".token.json"

# OAuth 跳轉回來時要打到哪個本機 port
# 任意挑一個不太可能被別的程式佔用的高位 port
# 注意：這個 port 在 DCR 註冊時會被一起送出去，之後不能隨意換
# （換了的話，下次 Robinhood 會拒絕跳轉，要刪 .token.json 重來）
REDIRECT_PORT = 33418
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"


# ─── token 儲存（讀寫 .token.json） ─────────────────────────────────────

class FileTokenStorage(TokenStorage):
    """把 OAuth 的 client_info 和 tokens 存到一個 JSON 檔。

    mcp SDK 在跑 OAuth 流程時會反覆問我們 "之前的 token 有沒有快取？"，
    這個類別就是回答這個問題並把新拿到的 token 寫回去。
    """

    def __init__(self, path: Path = TOKEN_FILE):
        self.path = path

    def _read(self) -> dict:
        """讀整個 JSON 檔；不存在就回空 dict（第一次跑的情境）。"""
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text())

    def _write(self, data: dict) -> None:
        """整個 JSON 檔覆蓋寫；寫完立刻 chmod 600 把權限收緊。"""
        self.path.write_text(json.dumps(data, indent=2))
        # 600 = owner read/write 只有自己；其他人連讀都不行
        # 沒有這行的話，預設權限可能是 644（其他用戶能讀到 token！）
        self.path.chmod(0o600)

    # 下面四個是 mcp SDK 的 TokenStorage 介面要求的方法。
    # SDK 都是 async 的，但我們的讀寫本身是同步的小檔案 I/O —— OK。

    async def get_tokens(self) -> OAuthToken | None:
        data = self._read()
        return OAuthToken(**data["tokens"]) if "tokens" in data else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        # exclude_none：Robinhood 的 token response 有額外欄位（user_uuid 等），
        # 用 exclude_none 確保我們只存 SDK 認得的欄位
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        data = self._read()
        return OAuthClientInformationFull(**data["client_info"]) if "client_info" in data else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(data)


# ─── OAuth 跳轉接收器（本機暫時開的 HTTP server） ───────────────────────

class _CallbackServer:
    """暫時在 localhost:33418 開一個 HTTP server，等 Robinhood 跳轉回來。

    為什麼要這樣做：
    OAuth 流程是「瀏覽器→Robinhood→使用者按授權→Robinhood 跳轉到一個網址」。
    跳轉的網址要在 OAuth 一開始就告訴 Robinhood，且必須是 Robinhood 能連到的。
    最簡單的方法就是叫 Robinhood 把瀏覽器導到 http://localhost:PORT/callback，
    我們自己在那個 port 開個小 server 接住，把 query string 裡的 code 撈出來。

    這是 OAuth 桌面應用的標準作法（PKCE flow），不需要公開的 callback URL。
    """

    def __init__(self, port: int = REDIRECT_PORT):
        self.port = port
        self.code: str | None = None    # 收到的 authorization code
        self.state: str | None = None   # 對應的 state（防 CSRF）
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        # threading.Event 用來通知 async 等待方「callback 來了」
        self._done = threading.Event()

    def start(self) -> None:
        """在 daemon thread 裡啟動 HTTP server，背景監聽。"""
        outer = self  # 內層 class 要透過閉包存取外層 self

        class Handler(BaseHTTPRequestHandler):
            """處理單一個 GET 請求 —— 就是 Robinhood 跳轉回來的那一下。"""
            def do_GET(self_):  # noqa: N802
                # path 形如 "/callback?code=xxx&state=yyy"
                qs = parse_qs(urlparse(self_.path).query)
                outer.code = qs.get("code", [None])[0]
                outer.state = qs.get("state", [None])[0]
                # 回一個 HTML 給瀏覽器顯示，避免使用者看到一片空白以為壞了
                self_.send_response(200)
                self_.send_header("Content-Type", "text/html; charset=utf-8")
                self_.end_headers()
                msg = ("<h2>Authorization received.</h2>"
                       "<p>You can close this tab and return to the terminal.</p>")
                self_.wfile.write(msg.encode())
                # 喚醒在 wait() 的 async caller
                outer._done.set()

            def log_message(self_, *args, **kwargs):
                """靜音預設的 access log，不要污染我們的 stdout。"""
                return

        self._server = HTTPServer(("localhost", self.port), Handler)
        # daemon=True：主程式結束時這條 thread 也跟著死，不會卡住程式退出
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    async def wait(self, timeout: float = 300.0) -> tuple[str, str | None]:
        """非同步等 callback；最多等 5 分鐘（使用者輸帳密、看授權頁的時間）。"""
        loop = asyncio.get_running_loop()
        # threading.Event 不是 awaitable，丟到 executor 裡讓 async loop 不被卡
        await loop.run_in_executor(None, self._done.wait, timeout)
        if self.code is None:
            # 5 分鐘還沒收到 → 大概使用者放棄了，或瀏覽器有問題
            raise RuntimeError("OAuth callback did not arrive within timeout.")
        return self.code, self.state

    def shutdown(self) -> None:
        """關閉 server。每次 rh_session() 結束都要叫，否則 port 會卡住。"""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


# ─── 對外的主入口 ───────────────────────────────────────────────────────

@asynccontextmanager
async def rh_session() -> AsyncIterator[ClientSession]:
    """產生一個「已登入、初始化完成的 MCP session」。

    用法：
        async with rh_session() as session:
            await session.call_tool("...")

    背後做了什麼：
      1. 開本機 callback server（即使這次不需要 OAuth 也先開，反正便宜）
      2. 建立 OAuthClientProvider —— SDK 會用它處理整個 token 生命週期
      3. 跟 MCP server 建連線（如果沒有有效 token，SDK 自動觸發 OAuth 流程）
      4. yield session 給 caller 使用
      5. caller 用完之後，with 結束時關掉 server 和連線
    """
    callback = _CallbackServer()
    callback.start()

    async def open_browser(url: str) -> None:
        """SDK 需要使用者授權時會呼叫這個 → 開瀏覽器。"""
        print(f"\n  → Opening browser to authorize the agent.")
        print(f"    If it does not open, copy this URL:\n    {url}\n")
        webbrowser.open(url)

    async def wait_for_callback() -> tuple[str, str | None]:
        """SDK 呼叫這個來等使用者授權完成 → 阻塞直到 callback 來。"""
        return await callback.wait()

    # OAuthClientProvider：mcp SDK 內建的 OAuth 處理器
    # 它會自動：DCR 註冊 → 跑 PKCE flow → 拿 token → 偷偷 refresh
    auth = OAuthClientProvider(
        server_url=MCP_URL,
        client_metadata=OAuthClientMetadata(
            client_name="trade_bot rebalancer",
            redirect_uris=[REDIRECT_URI],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            # "none" = 我們是 public client，沒有 client_secret（PKCE 取代 secret）
            token_endpoint_auth_method="none",
            # Robinhood 唯一支援的 scope
            scope="internal",
        ),
        storage=FileTokenStorage(),
        redirect_handler=open_browser,
        callback_handler=wait_for_callback,
    )

    try:
        # streamablehttp_client：MCP 的 HTTP 傳輸層
        # 它會自動把 access_token 塞進每個 request 的 Authorization header
        async with streamablehttp_client(MCP_URL, auth=auth) as (read, write, _):
            async with ClientSession(read, write) as session:
                # MCP 規定：呼叫 tool 之前要先 initialize（交換版本、能力）
                await session.initialize()
                yield session
    finally:
        # 不管成功失敗都要關 server，否則 port 33418 會被卡住
        callback.shutdown()


# ─── 工具回傳的解析輔助 ────────────────────────────────────────────────

def parse_tool_json(result) -> dict:
    """從 MCP 工具回傳的 result 物件取出 JSON payload。

    MCP 的 tool result 結構：
        result.content = [TextContent(type="text", text="..."), ...]
    Robinhood 的工具都把 JSON 字串塞在第一個 text block，我們就抓那一塊。
    """
    for block in result.content:
        if getattr(block, "type", None) == "text":
            return json.loads(block.text)
    raise RuntimeError("Tool result had no text content")
