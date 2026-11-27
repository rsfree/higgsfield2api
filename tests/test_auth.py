"""SERVICE_TOKEN 鉴权 + 计费闸门的行为锁。

这三个断言是部署红线：没有 401 就等于把「花别人额度」的入口挂到公网。
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from hfapi.config import settings


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    from hfapi import server

    monkeypatch.setenv("SERVICE_TOKEN", "t0ken")
    object.__setattr__(settings, "service_token", "t0ken")
    object.__setattr__(settings, "allow_billing", False)
    return TestClient(server.app)


BODY = {
    "model": "magi_2",
    "content": [{"type": "text", "text": "一只橘猫在雨天的霓虹街头慢跑"}],
    "duration": 4,
}

AUTH = {"Authorization": "Bearer t0ken"}


def test_healthz_stays_open(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["auth_required"] is True


def test_no_token_rejected(client: TestClient) -> None:
    r = client.post("/contents/generations/tasks", json=BODY)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AuthenticationError"


def test_wrong_token_rejected(client: TestClient) -> None:
    r = client.post("/contents/generations/tasks", json=BODY, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_x_api_key_accepted(client: TestClient) -> None:
    r = client.post("/contents/generations/tasks", json=BODY, headers={"X-Api-Key": "t0ken"})
    # 通过鉴权后撞上计费闸门 —— 这正是「没花钱」的证据
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "Forbidden.BillingDisabled"


def test_dry_run_never_charges(client: TestClient) -> None:
    r = client.post("/admin/dry-run", json=BODY, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert body["will_charge"] is False
    assert body["upstream_body"]["params"]["prompt"].startswith("一只橘猫")


def test_bad_param_is_400_before_billing(client: TestClient) -> None:
    r = client.post("/admin/dry-run", json={**BODY, "resolution": "720p"}, headers=AUTH)
    assert r.status_code == 400
