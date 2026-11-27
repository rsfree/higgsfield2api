"""号池调度、熔断与重试边界的行为锁。

最贵的一条：**`create` 只能在「上游明确没受理」时换号重试**。
网络异常 / 5xx 一律不重发——{id} 是唯一幂等键，重发就是两个计费任务。
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

import pytest

from hfapi.config import settings
from hfapi.errors import CODE_AUTH, CODE_INVALID, ApiError
from hfapi.pool import AccountPool, Account
from hfapi.upstream import Upstream

def upstream_401(msg: str = "cookie 失效") -> ApiError:
    """上游 401 的真实形状：对外 502（不是调用方 token 错），upstream.http=401 才是判据。"""
    return ApiError(CODE_AUTH, msg, 502, upstream={"http": 401, "stage": "probe"})


COOKIES_A = {"__client": "a", "__client_uat": "1"}
COOKIES_B = {"__client": "b", "__client_uat": "1"}
COOKIES_C = {"__client": "c", "__client_uat": "1"}


class Resp:
    def __init__(self, status: int, *, data=None, ctype="application/json") -> None:
        self.status_code = status
        self._data = data if data is not None else ({"ok": True} if status == 200 else {"err": "x"})
        self.text = json.dumps(self._data)
        self.headers = {"content-type": ctype}

    def json(self):
        return self._data


class FakeSession:
    """按脚本出招：元素是 Resp → 返回；是 Exception → 抛出。"""

    def __init__(self, script=None) -> None:
        self.script = list(script or [])
        self.calls: list[tuple[str, str]] = []

    def request(self, method, url, json_body=None, authed=True, timeout=None):
        self.calls.append((method, url))
        if not self.script:
            return Resp(200, data={"id": "js-default", "jobs": [{"id": "job-default"}]})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture()
def mkpool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HF_COOKIES_JSON", raising=False)
    object.__setattr__(settings, "cookies_file", tmp_path / "absent-cookies.json")

    def _make() -> AccountPool:
        return AccountPool(tmp_path / f"accounts-{uuid.uuid4().hex[:6]}.json")

    return _make


def _attach(pool: AccountPool, cookies: dict, script=None, name="") -> Account:
    """加号并把它的 session 换成假实现（跳过真实 curl_cffi）。"""
    acc = pool.add(cookies, name=name)
    acc._session = FakeSession(script)
    return acc


# --------------------------------------------------------------------------- 调度

def test_round_robin_across_accounts(mkpool) -> None:
    pool = mkpool()
    ids = {_attach(pool, c).id for c in (COOKIES_A, COOKIES_B, COOKIES_C)}

    picked = []
    for _ in range(3):
        acc = pool.acquire()
        picked.append(acc.id)
        pool.release(acc, ok=True)

    assert len(set(picked)) == 3, "三个号必须被轮流选中，不能全压在一个号上"
    assert set(picked) == ids


def test_least_inflight_wins(mkpool) -> None:
    pool = mkpool()
    a = _attach(pool, COOKIES_A, name="a")
    b = _attach(pool, COOKIES_B, name="b")

    first = pool.acquire()
    second = pool.acquire()          # a 还占着，必须落到 b
    assert {first.id, second.id} == {a.id, b.id}
    assert second.id != first.id


def test_no_account_raises_503(mkpool) -> None:
    pool = mkpool()
    with pytest.raises(ApiError) as ei:
        pool.acquire()
    assert ei.value.http_status == 503
    assert "号池为空" in ei.value.message


def test_pick_prefers_bound_account(mkpool) -> None:
    pool = mkpool()
    a = _attach(pool, COOKIES_A)
    b = _attach(pool, COOKIES_B)
    assert pool.pick(a.id).id == a.id
    assert pool.pick("nope").id in {a.id, b.id}
    assert pool.pick(None).id in {a.id, b.id}


# --------------------------------------------------------------------------- 熔断

def test_single_401_only_cools(mkpool) -> None:
    pool = mkpool()
    acc = _attach(pool, COOKIES_A)
    pool.release(acc, ok=False, exc=upstream_401("401"))

    assert acc.invalid_at == 0, "单次 401 不该判死——Clerk 抖动也会是 401"
    assert not acc.usable(time.time())  # 冷却中
    with pytest.raises(ApiError):
        pool.acquire()


def test_two_401_marks_invalid(mkpool) -> None:
    pool = mkpool()
    acc = _attach(pool, COOKIES_A)
    for _ in range(settings.pool_auth_strikes):
        pool.release(acc, ok=False, exc=upstream_401())

    assert acc.invalid_at > 0
    assert "cookie 失效" in acc.invalid_reason
    state, _ = acc.health(acc.invalid_at + 1)
    assert state == "invalid"

    # 判死之后，即使冷却期过了也不许再被选中
    acc._cooldown_until = 0.0
    with pytest.raises(ApiError):
        pool.acquire()


def test_cooldown_expires(mkpool) -> None:
    pool = mkpool()
    acc = _attach(pool, COOKIES_A)
    pool.release(acc, ok=False, exc=ApiError("RateLimitExceeded.EndpointRPMExceeded", "限流", 429))
    assert acc._cooldown_until > 0

    acc._cooldown_until = 0.0        # 模拟时间流逝
    assert pool.acquire().id == acc.id


def test_reenable_clears_invalid(mkpool) -> None:
    pool = mkpool()
    acc = _attach(pool, COOKIES_A)
    for _ in range(settings.pool_auth_strikes):
        pool.release(acc, ok=False, exc=upstream_401("x"))
    assert acc.invalid_at > 0

    pool.update(acc.id, enabled=True)
    assert acc.invalid_at == 0
    assert pool.acquire().id == acc.id


# --------------------------------------------------------------------------- 持久化

def test_persist_and_reload(mkpool) -> None:
    pool = mkpool()
    acc = _attach(pool, COOKIES_A, name="号1")
    pool.release(acc, ok=True)

    again = AccountPool(pool.path)
    assert len(again.list()) == 1
    got = again.list()[0]
    assert got.id == acc.id
    assert got.name == "号1"
    assert got.cookies["__client"] == "a"
    assert got.ok_count == 1


def test_file_mode_is_600(mkpool) -> None:
    pool = mkpool()
    _attach(pool, COOKIES_A)
    if sys.platform != "win32":
        assert oct(pool.path.stat().st_mode)[-3:] == "600"


def test_public_projection_hides_cookie_values(mkpool) -> None:
    pool = mkpool()
    acc = _attach(pool, {"__client": "SECRET-VALUE", "__client_uat": "1"})
    pub = acc.to_public()
    assert "cookies" not in pub
    assert "SECRET-VALUE" not in json.dumps(pub)


def test_corrupt_file_is_quarantined(mkpool, tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    pool = AccountPool(path)
    assert pool.list() == []
    assert "损坏" in pool.load_warning
    assert list(tmp_path.glob("broken.corrupt-*")), "坏文件必须留证，不能静默丢掉所有号"


def test_legacy_single_cookie_is_migrated(mkpool, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_COOKIES_JSON", json.dumps(COOKIES_A))
    pool = mkpool()
    accs = pool.list()
    assert len(accs) == 1
    assert accs[0].id == "acc-default"
    assert accs[0].cookies["__client"] == "a"


# --------------------------------------------------------------------------- 重试边界

def test_create_retries_on_401(mkpool) -> None:
    pool = mkpool()
    bad = _attach(pool, COOKIES_A, [Resp(401)])
    good = _attach(pool, COOKIES_B, [Resp(200, data={"id": "js-1", "jobs": [{"id": "job-1"}]})])

    data, acc_id = Upstream(pool).create({"params": {}}, dry_run=False)
    assert data["id"] == "js-1"
    assert acc_id == good.id
    assert bad.fail_count == 1, "401 的号必须被记一次失败"
    assert good.ok_count == 1


def test_create_does_not_retry_on_400(mkpool) -> None:
    pool = mkpool()
    a = _attach(pool, COOKIES_A, [Resp(400, data={"message": "bad param"})])
    b = _attach(pool, COOKIES_B, [Resp(200, data={"id": "js-2"})])

    with pytest.raises(ApiError) as ei:
        Upstream(pool).create({"params": {}}, dry_run=False)
    assert ei.value.code == CODE_INVALID
    assert len(a._session.calls) == 1
    assert b._session.calls == [], "参数错误换号也没用，不该浪费一个好号"


def test_create_does_not_retry_on_network_error(mkpool) -> None:
    pool = mkpool()
    a = _attach(pool, COOKIES_A, [RuntimeError("connection reset")])
    b = _attach(pool, COOKIES_B, [Resp(200, data={"id": "js-3"})])

    with pytest.raises(ApiError) as ei:
        Upstream(pool).create({"params": {}}, dry_run=False)
    assert ei.value.upstream["unknown_outcome"] is True
    assert "不要自动重试" in ei.value.message
    assert len(a._session.calls) == 1
    assert b._session.calls == [], "网络异常可能已扣费，绝不能换号重发"


def test_create_does_not_retry_on_500(mkpool) -> None:
    pool = mkpool()
    a = _attach(pool, COOKIES_A, [Resp(500)])
    b = _attach(pool, COOKIES_B, [Resp(200, data={"id": "js-4"})])

    with pytest.raises(ApiError):
        Upstream(pool).create({"params": {}}, dry_run=False)
    assert b._session.calls == [], "5xx 也可能已受理，不重发"


def test_create_tries_each_account_at_most_once(mkpool) -> None:
    pool = mkpool()
    _attach(pool, COOKIES_A, [Resp(401), Resp(401)])
    _attach(pool, COOKIES_B, [Resp(401), Resp(401)])
    _attach(pool, COOKIES_C, [Resp(401), Resp(401)])

    with pytest.raises(ApiError):
        Upstream(pool).create({"params": {}}, dry_run=False)

    # POOL_RETRY=2 ⇒ 最多试两个号
    used = [a for a in pool.list() if a._session.calls]
    assert len(used) == min(settings.pool_retry, 3)


def test_dry_run_touches_no_account(mkpool) -> None:
    pool = mkpool()
    a = _attach(pool, COOKIES_A)
    data, acc_id = Upstream(pool).create({"params": {}}, dry_run=True)
    assert data["dry_run"] is True
    assert acc_id is None
    assert a._session.calls == []


def test_get_job_uses_bound_account(mkpool) -> None:
    pool = mkpool()
    a = _attach(pool, COOKIES_A, [Resp(404)])
    b = _attach(pool, COOKIES_B, [Resp(200, data={"id": "job-9", "status": "completed"}),
                                  Resp(200, data={"id": "job-9"})])

    job = Upstream(pool).get_job("job-9", b.id)
    assert job["id"] == "job-9"
    assert len(b._session.calls) == 1
    assert a._session.calls == []


# --------------------------------------------------------------------------- 无号可用的对外契约

@pytest.fixture()
def billing_on():
    old = settings.allow_billing
    object.__setattr__(settings, "allow_billing", True)
    yield
    object.__setattr__(settings, "allow_billing", old)


def _client(pool, monkeypatch):
    from fastapi.testclient import TestClient
    from hfapi import server

    object.__setattr__(settings, "service_token", "tok")
    monkeypatch.setattr(server, "pool", pool)
    monkeypatch.setattr(server.upstream, "pool", pool)
    return TestClient(server.app)


def test_empty_pool_returns_503_no_available_account(mkpool, monkeypatch, billing_on) -> None:
    """号池空时不能含糊地说「服务不可用」，要明确告诉调用方是号池问题。"""
    pool = mkpool()
    c = _client(pool, monkeypatch)
    r = c.post("/contents/generations/tasks",
               headers={"Authorization": "Bearer tok"},
               json={"model": "magi_2", "content": [{"type": "text", "text": "x"}], "duration": 4})
    assert r.status_code == 503
    body = r.json()["error"]
    assert body["code"] == "NoAvailableAccount"
    assert "号池为空" in body["message"]


def test_all_invalid_pool_reports_breakdown(mkpool, monkeypatch, billing_on) -> None:
    pool = mkpool()
    acc = _attach(pool, COOKIES_A)
    for _ in range(settings.pool_auth_strikes):
        pool.release(acc, ok=False, exc=upstream_401())

    c = _client(pool, monkeypatch)
    r = c.post("/contents/generations/tasks",
               headers={"Authorization": "Bearer tok"},
               json={"model": "magi_2", "content": [{"type": "text", "text": "x"}], "duration": 4})
    assert r.status_code == 503
    msg = r.json()["error"]["message"]
    assert "失效 1" in msg, f"要给出明细，实际：{msg}"


def test_external_write_is_picked_up_without_restart(tmp_path: Path) -> None:
    """CLI（独立进程）写的号池，服务进程必须能看见。

    真实事故（2026-11-27）：`tools/cookie_import` 报「已加入号池」，
    `/healthz` 仍 accounts_total=0 —— 服务进程只在启动时读过一次文件。
    """
    path = tmp_path / "hot.json"
    pool = AccountPool(path)
    assert pool.list() == []

    path.write_text(json.dumps({"version": 1, "accounts": [
        {"id": "acc-cli", "name": "cli", "cookies": COOKIES_A, "enabled": True, "created_at": 1}
    ]}), encoding="utf-8")

    assert [a.id for a in pool.list()] == ["acc-cli"]
    assert pool.summary()["total"] == 1
    assert pool.acquire().id == "acc-cli"


def test_hot_reload_merges_and_keeps_runtime_state(tmp_path: Path) -> None:
    """外部换凭据要生效，但正在飞的任务计数不能被清零（否则会重复派发）。"""
    import os

    path = tmp_path / "hot2.json"
    pool = AccountPool(path)
    acc = pool.add(COOKIES_A, name="a")
    acc._inflight = 2

    data = json.loads(path.read_text(encoding="utf-8"))
    data["accounts"][0]["cookies"] = COOKIES_B
    data["accounts"][0]["name"] = "renamed"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    bump = time.time_ns() + 10 ** 6
    os.utime(path, ns=(bump, bump))

    got = pool.get(acc.id)
    assert got is not None
    assert got.name == "renamed"
    assert got.cookies == COOKIES_B
    assert got._inflight == 2, "运行态必须保留"

    # 外部删号 → 内存同步移除
    path.write_text(json.dumps({"version": 1, "accounts": []}), encoding="utf-8")
    bump += 10 ** 6
    os.utime(path, ns=(bump, bump))
    assert pool.list() == []


def test_external_write_is_picked_up_without_restart(tmp_path: Path) -> None:
    """CLI（独立进程）写的号池，服务进程必须能看见。

    真实事故（2026-11-27）：`tools/cookie_import` 报「已加入号池」，
    `/healthz` 仍 accounts_total=0 —— 服务进程只在启动时读过一次文件。
    """
    path = tmp_path / "hot.json"
    pool = AccountPool(path)
    assert pool.list() == []

    path.write_text(json.dumps({"version": 1, "accounts": [
        {"id": "acc-cli", "name": "cli", "cookies": COOKIES_A, "enabled": True, "created_at": 1}
    ]}), encoding="utf-8")

    assert [a.id for a in pool.list()] == ["acc-cli"]
    assert pool.summary()["total"] == 1
    assert pool.acquire().id == "acc-cli"


def test_hot_reload_merges_and_keeps_runtime_state(tmp_path: Path) -> None:
    """外部换凭据要生效，但正在飞的任务计数不能被清零（否则会重复派发）。"""
    import os

    path = tmp_path / "hot2.json"
    pool = AccountPool(path)
    acc = pool.add(COOKIES_A, name="a")
    acc._inflight = 2

    data = json.loads(path.read_text(encoding="utf-8"))
    data["accounts"][0]["cookies"] = COOKIES_B
    data["accounts"][0]["name"] = "renamed"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    bump = time.time_ns() + 10 ** 6
    os.utime(path, ns=(bump, bump))

    got = pool.get(acc.id)
    assert got is not None
    assert got.name == "renamed"
    assert got.cookies == COOKIES_B
    assert got._inflight == 2, "运行态必须保留"

    # 外部删号 → 内存同步移除
    path.write_text(json.dumps({"version": 1, "accounts": []}), encoding="utf-8")
    bump += 10 ** 6
    os.utime(path, ns=(bump, bump))
    assert pool.list() == []
