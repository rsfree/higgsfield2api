"""凭据解析的护栏：错误来源必须在入口就被拦住，不能静默变成一条 401 死号。

真实事故（2026-11-27）：用户从 `fnf-api-gw.higgsfield.ai` 的请求 Copy as cURL，
那上面只有 `authorization: Bearer <jwt>`、没有 cookie 头。兜底解析把
`sec-ch-ua: "Not-A.Brand";v="99"` 拆成 `{v: '"99"', q: '0.9'}` 进场，号池多一条死号。
"""
from __future__ import annotations

import pytest

from hfapi.errors import ApiError
from hfapi.session import cookies_from_curl, looks_like_curl, parse_cookies, require_session_cookies

JWT_ONLY_CURL = """curl --url 'https://fnf-api-gw.higgsfield.ai/fnf/user/free-gens/v2' \\
-H 'accept: */*' \\
-H 'authorization: Bearer eyJhbGciOiJSUzI1NiJ9.eyJleHAiOjE3OTU3Njc3MTh9.sig' \\
-H 'sec-ch-ua: "Not-A.Brand";v="99", "Google Chrome";v="157"' \\
-H 'user-agent: Mozilla/5.0'"""

WITH_COOKIE_CURL = """curl 'https://higgsfield.ai/_clerk/v1/client?_clerk_js_version=5' \\
-H 'accept: */*' \\
-H 'cookie: __client=eyJhbGciOiJIUzI1NiJ9.client; __client_uat=1795767806' \\
-H 'user-agent: Mozilla/5.0'"""


def test_looks_like_curl() -> None:
    assert looks_like_curl(JWT_ONLY_CURL)
    assert looks_like_curl("-H 'accept: */*'")
    assert not looks_like_curl("__client=abc; __client_uat=123")
    assert not looks_like_curl('{"__client": "abc"}')


def test_curl_with_cookie_ok() -> None:
    ck = cookies_from_curl(WITH_COOKIE_CURL)
    assert ck["__client"] == "eyJhbGciOiJIUzI1NiJ9.client"
    assert ck["__client_uat"] == "1795767806"


def test_bearer_only_curl_is_rejected_with_real_reason() -> None:
    """不能只说「没找到 cookie 头」——要说清 JWT 60 秒过期、去哪复制。"""
    with pytest.raises(ApiError) as ei:
        cookies_from_curl(JWT_ONLY_CURL)
    msg = ei.value.message
    assert "Bearer JWT" in msg
    assert "60 秒" in msg
    assert "_clerk" in msg
    assert ei.value.http_status == 400


def test_plain_curl_without_cookie_is_rejected() -> None:
    with pytest.raises(ApiError) as ei:
        cookies_from_curl("curl 'https://fnf-api-gw.higgsfield.ai/fnf/jobs?size=1' -H 'accept: */*'")
    assert "没有 `-H 'cookie" in ei.value.message


def test_require_session_cookies_blocks_junk() -> None:
    junk = parse_cookies('sec-ch-ua: "Not-A.Brand";v="99"', source="t")
    assert "__client" not in junk  # 兜底解析确实会产出垃圾
    with pytest.raises(ApiError):
        require_session_cookies(junk, source="面板输入")
    assert require_session_cookies({"__client": "x"}) == {"__client": "x"}


def test_panel_parse_one_never_falls_back_to_junk() -> None:
    """面板入口：curl 一律走 cookie 头解析，缺 cookie 直接报错。"""
    from hfapi.panel import _parse_one

    with pytest.raises(ApiError):
        _parse_one(JWT_ONLY_CURL)
    assert _parse_one(WITH_COOKIE_CURL)["__client"].startswith("eyJ")
