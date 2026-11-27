"""面板鉴权与号池运维端点的行为锁。

红线四条：
  1. 没有 cookie 的 `/panel` 必须 303 到登录页，`/panel/api/*` 必须 401（不能有任何"半开"的读接口）；
  2. 口令错 8 次必须 429，否则就是公开的撞库靶子；
  3. 面板登录后不能绕过 Bearer 直接读 `/tasks`——面板只走自己的 `/panel/api/*`；
  4. 面板**永远不回传 cookie 值**，只回 cookie 名与条数。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hfapi.config import settings

PW = "sk-higgsfield2api"
CURL = ("curl 'https://fnf-api-gw.higgsfield.ai/fnf/jobs?size=1' "
        "-H 'cookie: __client=abc; __client_uat=123'")


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """隔离的号池 + 面板配置。返回 (client, pool)。"""
    from hfapi import panel, server
    from hfapi.pool import AccountPool

    monkeypatch.setenv("SERVICE_TOKEN", PW)
    monkeypatch.setenv("PANEL_PASSWORD", PW)
    monkeypatch.delenv("HF_COOKIES_JSON", raising=False)
    object.__setattr__(settings, "service_token", PW)
    object.__setattr__(settings, "allow_billing", False)
    object.__setattr__(settings, "cookies_file", tmp_path / "cookies.json")

    pool = AccountPool(tmp_path / "accounts.json")
    monkeypatch.setattr(panel, "pool", pool)
    monkeypatch.setattr(server, "pool", pool)
    monkeypatch.setattr(server.upstream, "pool", pool)

    # 单测不打上游：自检一律「通过」
    def fake_probe(account_id: str):
        acc = pool.get(account_id)
        pool.note(acc, ok=True)
        return {"id": account_id, "name": acc.name, "ok": True, "status": 200, "ms": 3}

    monkeypatch.setattr(pool, "probe", fake_probe)
    panel._FAILS.clear()
    return TestClient(server.app, follow_redirects=False), pool


@pytest.fixture()
def client(env) -> TestClient:
    return env[0]


@pytest.fixture()
def pool(env):
    return env[1]


def login(client: TestClient, ip: str = "10.0.0.1") -> None:
    r = client.post("/panel/login", data={"password": PW}, headers={"X-Forwarded-For": ip})
    assert r.status_code == 303, r.text
    assert "hfpanel" in r.cookies


# --------------------------------------------------------------------------- 鉴权

def test_dashboard_redirects_to_login(client: TestClient) -> None:
    r = client.get("/panel")
    assert r.status_code == 303
    assert r.headers["location"] == "/panel/login"


def test_login_page_is_public(client: TestClient) -> None:
    r = client.get("/panel/login")
    assert r.status_code == 200
    assert "登录口令" in r.text


def test_panel_api_requires_session(client: TestClient) -> None:
    for path in ("/panel/api/state", "/panel/api/task/x", "/panel/api/reload",
                 "/panel/api/accounts", "/panel/api/accounts/x/probe"):
        r = client.post(path) if path.endswith(("reload", "probe")) else client.get(path)
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


def test_panel_does_not_bypass_bearer(client: TestClient) -> None:
    login(client)
    # 面板会话是 cookie，不是 Bearer：API 面仍然要 token
    assert client.get("/tasks").status_code == 401


def test_logout_clears_cookie(client: TestClient) -> None:
    login(client)
    r = client.post("/panel/logout")
    assert r.status_code == 303
    assert client.get("/panel/api/state").status_code == 401


# --------------------------------------------------------------------------- 状态

def test_state_shape(client: TestClient) -> None:
    login(client)
    r = client.get("/panel/api/state")
    assert r.status_code == 200
    body = r.json()
    assert body["flags"]["billing"] is False
    assert body["flags"]["auth"] is True
    assert isinstance(body["tasks"], list)
    assert body["pool"]["total"] == 0
    assert body["accounts"] == []


def test_dry_run_from_panel(client: TestClient) -> None:
    login(client)
    body = {"model": "magi_2", "content": [{"type": "text", "text": "橘猫"}], "duration": 4}
    r = client.post("/panel/api/dry-run", json=body)
    assert r.status_code == 200
    assert r.json()["will_charge"] is False


# --------------------------------------------------------------------------- 号池

def test_add_account_and_no_cookie_leak(client: TestClient, pool, tmp_path: Path) -> None:
    login(client)
    r = client.post("/panel/api/accounts?name=%E5%8F%B71", content=CURL,
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == 1
    assert body["results"][0]["probe"]["ok"] is True

    acc = body["accounts"][0]
    assert acc["name"] == "号1"
    assert acc["cookie_count"] == 2
    assert acc["missing"] == []
    assert "cookies" not in acc, "公开投影绝不能带 cookie 值"
    assert "abc" not in json.dumps(body["accounts"], ensure_ascii=False), "cookie 值泄露到响应里了"

    # 落盘：accounts.json 必须 0600
    path = tmp_path / "accounts.json"
    assert path.exists()
    if sys.platform != "win32":
        assert oct(path.stat().st_mode)[-3:] == "600"
    assert len(pool.list()) == 1


def test_batch_import_multiple_curls(client: TestClient, pool) -> None:
    login(client)
    batch = CURL + "\ncurl 'https://fnf-api-gw.higgsfield.ai/fnf/jobs?size=1' -H 'cookie: __client=xyz'\n"
    r = client.post("/panel/api/accounts?name=batch", content=batch,
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == 2
    assert {a["name"] for a in body["accounts"]} == {"batch-1", "batch-2"}


def test_add_garbage_returns_400(client: TestClient) -> None:
    login(client)
    r = client.post("/panel/api/accounts", content="not a cookie at all",
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 400


def test_replace_cookie_keeps_identity(client: TestClient, pool) -> None:
    login(client)
    added = client.post("/panel/api/accounts", content=CURL,
                        headers={"Content-Type": "text/plain"}).json()["accounts"][0]
    acc_id = added["id"]

    r = client.post(f"/panel/api/accounts/{acc_id}/cookie",
                    content="curl 'https://x/y' -H 'cookie: __client=new; __client_uat=9'",
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == acc_id, "换 cookie 不能换 id，否则历史任务归属就断了"
    assert body["probe"]["ok"] is True
    acc = pool.get(acc_id)
    assert acc is not None and acc.cookies["__client"] == "new"


def test_toggle_and_delete(client: TestClient, pool) -> None:
    login(client)
    acc_id = client.post("/panel/api/accounts", content=CURL,
                         headers={"Content-Type": "text/plain"}).json()["accounts"][0]["id"]

    r = client.post(f"/panel/api/accounts/{acc_id}/toggle", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["accounts"][0]["enabled"] is False
    assert r.json()["pool"]["usable"] == 0

    r = client.delete(f"/panel/api/accounts/{acc_id}")
    assert r.status_code == 200
    assert r.json()["accounts"] == []
    assert client.delete(f"/panel/api/accounts/{acc_id}").status_code == 404


def test_rename(client: TestClient, pool) -> None:
    login(client)
    acc_id = client.post("/panel/api/accounts", content=CURL,
                         headers={"Content-Type": "text/plain"}).json()["accounts"][0]["id"]
    r = client.post(f"/panel/api/accounts/{acc_id}/rename", json={"name": "免费额度A"})
    assert r.status_code == 200
    assert r.json()["accounts"][0]["name"] == "免费额度A"


def test_legacy_cookie_endpoint_still_works(client: TestClient, pool) -> None:
    """老的 curl 脚本打在 /panel/api/cookie 上，不能因为改造就 500。"""
    login(client)
    r = client.post("/panel/api/cookie", content=CURL, headers={"Content-Type": "text/plain"})
    assert r.status_code == 200
    assert r.json()["added"] == 1


def test_probe_all(client: TestClient, pool) -> None:
    login(client)
    client.post("/panel/api/accounts", content=CURL, headers={"Content-Type": "text/plain"})
    r = client.post("/panel/api/accounts/probe-all")
    assert r.status_code == 200
    assert r.json()["probe"][0]["ok"] is True


def test_batch_import_caps_sync_probe(client: TestClient, pool) -> None:
    """批量导入只同步自检前 3 个：一个连不通的号要等到超时，10 个就是两分钟。"""
    from hfapi.panel import SYNC_PROBE_LIMIT

    login(client)
    batch = "\n".join(f"curl 'https://x/y' -H 'cookie: __client=c{i}'" for i in range(5))
    r = client.post("/panel/api/accounts", content=batch, headers={"Content-Type": "text/plain"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == 5
    assert body["pending"] == 5 - SYNC_PROBE_LIMIT
    assert sum(1 for x in body["results"] if x["probe"] is None) == 5 - SYNC_PROBE_LIMIT
    assert len(pool.list()) == 5
