"""会话层：浏览器 cookie 导入 + Clerk JWT 刷新 + 浏览器指纹 HTTP 客户端。

上游 `fnf-api-gw.higgsfield.ai` 有两道门：
  1. Cloudflare 托管挑战 —— 实测裸 curl 403，Chrome 指纹（curl_cffi impersonate）通过；
  2. Clerk 会话 —— `Authorization: Bearer <jwt>`，JWT 寿命约 60 秒，用 `__client` cookie 向
     Clerk Frontend API 换新即可，不需要开浏览器。
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from curl_cffi import requests as cffi

from .config import settings
from .errors import CODE_AUTH, ApiError

COOKIE_DOMAIN_HINT = ("higgsfield",)


# --------------------------------------------------------------------------- cookie

def parse_cookies(raw: str, *, source: str = "cookie") -> dict[str, str]:
    """支持 4 种导出格式：JSON 字典 / Cookie-Editor 数组 / Playwright storage_state / Netscape txt / 原始 Cookie 头。"""
    raw = (raw or "").strip()
    if not raw:
        raise ApiError(CODE_AUTH, f"{source} 内容为空", 401, param="cookie")

    if raw.startswith("{") or raw.startswith("["):
        data = json.loads(raw)
        if isinstance(data, dict) and "cookies" in data:
            data = data["cookies"]
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
        out: dict[str, str] = {}
        for item in data:
            if isinstance(item, dict) and "name" in item and "value" in item:
                out[str(item["name"])] = str(item["value"])
        if out:
            return out

    if "\t" in raw and not raw.lower().startswith("cookie:"):
        # Netscape cookies.txt
        out = {}
        for line in raw.splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                out[parts[5]] = parts[6]
        if out:
            return out

    # 原始 Cookie 头
    text = raw[len("cookie:"):] if raw.lower().startswith("cookie:") else raw
    out = {}
    for chunk in text.replace("\n", ";").split(";"):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            if k.strip():
                out[k.strip()] = v.strip()
    if not out:
        raise ApiError(CODE_AUTH, f"{source} 无法解析", 401, param="cookie")
    return out


SESSION_COOKIE = "__client"

_CURL_HOWTO = (
    "正确姿势：浏览器登录 higgsfield.ai → F12 → Network → 筛 `_clerk` → "
    "点任意一条（如 `v1/client`）→ 右键 Copy as cURL。"
    "发往 fnf-api-gw 的请求只带 Bearer JWT，通常**不带** cookie，从那上面复制是拿不到号的。"
)


def looks_like_curl(text: str) -> bool:
    """判断一段文本是不是 curl 命令（而不是裸 cookie 串）。

    用来堵一个坑：没有 cookie 头的 curl 若走 `parse_cookies` 的兜底分支，
    `sec-ch-ua: "Not-A.Brand";v="99"` 会被解析成 `{v: '"99"', q: '0.9'}` 这种垃圾，
    然后静默入池变成一条永远 401 的死号。
    """
    return bool(re.search(r"^\s*curl\b", text, re.I | re.M)) or bool(re.search(r"-H\s+['\"]", text))


def cookies_from_curl(text: str) -> dict[str, str]:
    """从 DevTools「Copy as cURL」里抠 cookie 头——只有这条路能拿到 HttpOnly 的 __client。"""
    m = re.search(r"-H\s+'cookie:\s*([^']+)'", text, re.I) or re.search(r'-H\s+"cookie:\s*([^"]+)"', text, re.I)
    if not m:
        if re.search(r"authorization:\s*['\"]?\s*bearer\s+eyJ", text, re.I):
            raise ApiError(
                CODE_AUTH,
                "这段是 Bearer JWT，不是 cookie：JWT 寿命只有 60 秒，换新必须靠 __client。" + _CURL_HOWTO,
                400,
                param="curl",
            )
        raise ApiError(CODE_AUTH, "这段文本里没有 `-H 'cookie: …'`。" + _CURL_HOWTO, 400, param="curl")
    return parse_cookies(m.group(1).replace("\n", " "), source="curl cookie 头")


def require_session_cookies(cookies: dict[str, str], *, source: str = "cookie") -> dict[str, str]:
    """号池只收能换 JWT 的 cookie：缺 __client 的进去也是死号，不如在入口就拒掉。"""
    if SESSION_COOKIE in cookies:
        return cookies
    raise ApiError(
        CODE_AUTH,
        f"{source} 里没有 __client（HttpOnly，只有 Copy as cURL 能拿到）。"
        f"这份凭据换不到 JWT，入池后必然 401。当前解析到 {len(cookies)} 项：{', '.join(list(cookies)[:6]) or '空'}。" + _CURL_HOWTO,
        400,
        param="cookie",
    )


def load_cookies(path: Path | None = None) -> dict[str, str]:
    """优先读 HF_COOKIES_JSON（容器里不必挂卷），否则读文件。"""
    env_raw = (os.getenv("HF_COOKIES_JSON") or "").strip()
    if env_raw:
        return parse_cookies(env_raw, source="HF_COOKIES_JSON")

    p = path or settings.cookie_path()
    if not p.exists():
        raise ApiError(
            CODE_AUTH,
            f"cookie 文件不存在：{p}。先跑 `python -m tools.cookie_import --curl req.txt`",
            401,
            param="cookie",
        )
    return parse_cookies(p.read_text(encoding="utf-8", errors="replace"), source=str(p))


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


# --------------------------------------------------------------------------- JWT

def _jwt_exp(jwt: str) -> int:
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:
        return 0


class Session:
    """一个账号的上游会话。cookie 只读，JWT 按需刷新。

    号池里每个账号各持一个实例：JWT 缓存互不干扰，一个号刷 token 不会把别的号顶掉。
    `Session({})` 表示匿名会话（查询用，只读端点本来就匿名可读）。
    """

    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self._cookies: dict[str, str] | None = None if cookies is None else dict(cookies)
        self._jwt: str = ""
        self._jwt_exp: int = 0
        self.client = cffi.Session(impersonate=settings.impersonate)

    # -- cookies
    @property
    def cookies(self) -> dict[str, str]:
        if self._cookies is None:
            self._cookies = load_cookies()
        return self._cookies

    def reload_cookies(self) -> dict[str, str]:
        self._cookies = load_cookies()
        self._jwt, self._jwt_exp = "", 0
        return self._cookies

    def set_cookies(self, cookies: dict[str, str]) -> None:
        """直接注入 cookie（导入工具用：验证的对象必须是刚写下的那份，而不是默认路径里的旧的）。"""
        self._cookies = dict(cookies)
        self._jwt, self._jwt_exp = "", 0

    # -- headers
    def base_headers(self) -> dict[str, str]:
        h = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": settings.site,
            "Referer": f"{settings.site}/ai/video?model=magi_2",
        }
        # 查询 /jobs/{id} 上游不校验 JWT，没有 cookie 也应当能查；只有创建才需要
        try:
            ck = self.cookies
            if ck:
                h["Cookie"] = cookie_header(ck)
        except ApiError:
            pass
        h.update({str(k): str(v) for k, v in settings.extra_headers.items()})
        return h

    def auth_headers(self, timeout: int | None = None) -> dict[str, str]:
        h = self.base_headers()
        h["Authorization"] = f"Bearer {self.jwt(timeout=timeout)}"
        return h

    # -- token
    def jwt(self, force: bool = False, timeout: int | None = None) -> str:
        now = int(time.time())
        if not force and self._jwt and self._jwt_exp - 15 > now:
            return self._jwt

        url = f"{settings.clerk_proxy}/v1/client?_clerk_js_version={settings.clerk_js_version}"
        try:
            cookies = self.cookies
            r = self.client.get(
                url,
                headers={
                    "Accept": "*/*",
                    "Cookie": cookie_header(cookies),
                    "Origin": settings.site,
                    "Referer": f"{settings.site}/",
                },
                timeout=timeout or 30,
            )
        except ApiError:
            raise
        except Exception as exc:  # 网络层
            raise ApiError(CODE_AUTH, f"Clerk 刷新 JWT 失败（网络）：{exc}", 502) from exc

        if r.status_code != 200:
            raise ApiError(CODE_AUTH, f"Clerk 返回 {r.status_code}，cookie 可能已失效", 401, param="cookie")

        body = r.json().get("response") or {}
        sessions = body.get("sessions") or []
        active_id = body.get("last_active_session_id")
        chosen = None
        for s in sessions:
            if active_id and s.get("id") == active_id:
                chosen = s
                break
        if chosen is None:
            for s in sessions:
                if s.get("status") == "active":
                    chosen = s
                    break
        if chosen is None and sessions:
            chosen = sessions[0]
        if chosen is None:
            raise ApiError(CODE_AUTH, "Clerk 会话为空：cookie 缺少 __client 或已登出", 401, param="cookie")

        token = (chosen.get("last_active_token") or {}).get("jwt") or ""
        if not token:
            raise ApiError(CODE_AUTH, "Clerk 会话没有 last_active_token.jwt", 401, param="cookie")

        self._jwt = token
        self._jwt_exp = _jwt_exp(token) or (now + 50)
        return token

    # -- 便捷请求
    def request(self, method: str, url: str, *, json_body: Any = None, authed: bool = True,
                timeout: int | None = None) -> cffi.Response:
        headers = self.auth_headers(timeout=timeout) if authed else self.base_headers()
        return self.client.request(
            method, url, json=json_body, headers=headers,
            timeout=timeout or settings.request_timeout,
        )

    # -- 零计费自检
    def whoami(self, timeout: int | None = None) -> dict[str, Any]:
        """用一次只读请求确认 cookie/JWT 是否可用：GET /jobs?size=1。不产生任何任务。

        自检会传一个短超时：一个连不通的号不该让整批导入卡住 30 秒。
        """
        r = self.request("GET", f"{settings.gateway}/jobs?size=1", timeout=timeout)
        ok = r.status_code == 200
        return {
            "ok": ok,
            "status": r.status_code,
            "jwt_exp_in": max(0, self._jwt_exp - int(time.time())),
            "detail": None if ok else r.text[:300],
        }
