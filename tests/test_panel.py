"""面板鉴权与运维端点的行为锁。

红线三条：
  1. 没有 cookie 的 `/panel` 必须 302 到登录页，`/panel/api/*` 必须 401（不能有任何"半开"的读接口）；
  2. 口令错 8 次必须 429，否则就是公开的撞库靶子；
  3. 面板登录后不能绕过 Bearer 直接读 `/tasks`——面板只走自己的 `/panel/api/*`。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hfapi.config import settings

PW = "sk-higgsfield2api"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from hfapi import panel, server

    monkeypatch.setenv("SERVICE_TOKEN", PW)
    monkeypatch.setenv("PANEL_PASSWORD", PW)
    object.__setattr__(settings, "service_token", PW)
    object.__setattr__(settings, "allow_billing", False)
    object.__setattr__(settings, "cookies_file", tmp_path / "cookies.json")
    panel._FAILS.clear()
    panel._SESSION.update(at=0.0, value=None)
    return TestClient(server.app, follow_redirects=False)


def login(client: TestClient, ip: str = "10.0.0.1") -> None:
    r = client.post("/panel/login", data={"password": PW}, headers={"X-Forwarded-For": ip})
    assert r.status_code == 303, r.text
    assert "hfpanel" in r.cookies


def test_dashboard_redirects_to_login(client: TestClient) -> None:
    r = client.get("/panel")
    assert r.status_code == 303
    assert r.headers["location"] == "/panel/login"


def test_login_page_is_public(client: TestClient) -> None:
    r = client.get("/panel/login")
    assert r.status_code == 200
    assert "登录口令" in r.text


def test_panel_api_requires_session(client: TestClient) -> None:
    for path in ("/panel/api/state", "/panel/api/task/x", "/panel/api/reload"):
        r = client.get(path) if path != "/panel/api/reload" else client.post(path)
        assert r.status_code == 401, path
        assert r.json()["error"]["code"] == "AuthenticationError"


def test_wrong_password_rejected(client: TestClient) -> None:
    r = client.post("/panel/login", data={"password": "nope"}, headers={"X-Forwarded-For": "10.0.0.2"})
    assert r.status_code == 401
    assert "hfpanel" not in r.cookies


def test_bruteforce_locked(client: TestClient) -> None:
    ip = {"X-Forwarded-For": "10.0.0.3"}
    for _ in range(8):
        client.post("/panel/login", data={"password": "bad"}, headers=ip)
    r = client.post("/panel/login", data={"password": PW}, headers=ip)
    assert r.status_code == 429, "第 9 次必须被限速挡住，即使口令正确"


def test_login_then_state(client: TestClient) -> None:
    login(client)
    r = client.get("/panel/api/state")
    assert r.status_code == 200
    body = r.json()
    assert body["flags"]["billing"] is False
    assert body["flags"]["auth"] is True
    assert body["cookie"]["present"] is False
    assert isinstance(body["tasks"], list)


def test_panel_does_not_bypass_bearer(client: TestClient) -> None:
    login(client)
    # 面板会话是 cookie，不是 Bearer：API 面仍然要 token
    assert client.get("/tasks").status_code == 401


def test_dry_run_from_panel(client: TestClient) -> None:
    login(client)
    body = {"model": "magi_2", "content": [{"type": "text", "text": "橘猫"}], "duration": 4}
    r = client.post("/panel/api/dry-run", json=body)
    assert r.status_code == 200
    assert r.json()["will_charge"] is False


def test_cookie_import_writes_file(client: TestClient) -> None:
    login(client)
    curl = ("curl 'https://fnf-api-gw.higgsfield.ai/fnf/jobs?size=1' "
            "-H 'cookie: __client=abc; __client_uat=123'")
    r = client.post("/panel/api/cookie", content=curl, headers={"Content-Type": "text/plain"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["missing"] == []
    # 上游此时是假 cookie，自检必然失败——但文件必须落地
    assert Path(body["written"]).exists()
    if sys.platform != "win32":      # Windows 上 chmod 不生效，只在 POSIX 断言 600
        assert oct(Path(body["written"]).stat().st_mode)[-3:] == "600"
    assert body["session"]["ok"] is False


def test_logout_clears_cookie(client: TestClient) -> None:
    login(client)
    r = client.post("/panel/logout")
    assert r.status_code == 303
    assert client.get("/panel/api/state").status_code == 401
