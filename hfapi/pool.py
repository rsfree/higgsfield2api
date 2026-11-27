"""号池：多个 Higgsfield 账号的存储、调度与熔断。

一个账号 = 一套浏览器 cookie（`__client` / `__client_uat` / `cf_clearance`）+ 自己的 JWT 缓存
+ 自己的 curl_cffi 客户端。号与号之间完全隔离：A 号 JWT 过期不影响 B 号正在跑的任务。

调度策略（`acquire`）：在「可用」账号里按 `(当前占用数, 最后使用时间)` 升序取第一个，
即 **最少在飞 + 最久未用**。效果等价于加权轮询，但天然处理不同耗时的任务——
一个 5 秒的短任务不会让某个号被反复选中。

熔断（`note`）：
  · 401（cookie / Clerk 会话失效）连续 `POOL_AUTH_STRIKES` 次 → 判死（invalid），
    必须人工换 cookie 才恢复。单次只冷却 `POOL_AUTH_COOLDOWN` 秒，因为 Clerk 偶发 5xx
    也会表现为刷新失败，一次就判死会误杀好号。
  · 429 → 冷却 `POOL_RATE_COOLDOWN` 秒。
  · 其它（5xx / 网络）→ 指数退避，上限 `POOL_MAX_COOLDOWN` 秒。
冷却到期自动恢复，不需要重启——这就是半开状态：下一次调用就是探测。

持久化：`data/accounts.json`（0600）。只落「结构性」状态：cookie、名称、启停、失效标记、
计数。在飞数、冷却截止时间、JWT 都是内存态，重启即重置——重启后让所有号重新参与调度
比继承一个陈旧的冷却更有用。
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import settings
from .errors import CODE_AUTH, ApiError, is_upstream_401
from .session import Session, load_cookies, parse_cookies

log = logging.getLogger("hfapi.pool")

SCHEMA_VERSION = 1

# 状态（展示用，由 enabled / invalid_at / cooldown 推导，不落盘）
ST_OK = "ok"
ST_NEW = "new"
ST_BUSY = "busy"
ST_COOLING = "cooling"
ST_INVALID = "invalid"
ST_DISABLED = "disabled"


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return "acc-" + secrets.token_hex(4)


# --------------------------------------------------------------------------- 账号

@dataclass
class Account:
    id: str
    name: str
    cookies: dict[str, str]
    enabled: bool = True
    created_at: int = 0
    note: str = ""

    # --- 持久化的健康事实 ---
    invalid_at: int = 0           # >0 = cookie 已被判失效，需人工换
    invalid_reason: str = ""
    ok_count: int = 0
    fail_count: int = 0
    last_ok_at: int = 0
    last_fail_at: int = 0
    last_error: str = ""

    # --- 内存态，不落盘 ---
    _session: Session | None = field(default=None, repr=False, compare=False)
    _cooldown_until: float = field(default=0.0, repr=False, compare=False)
    _inflight: int = field(default=0, repr=False, compare=False)
    _last_used: float = field(default=0.0, repr=False, compare=False)
    _consec: int = field(default=0, repr=False, compare=False)

    # -- 会话
    @property
    def session(self) -> Session:
        if self._session is None:
            self._session = Session(self.cookies)
        return self._session

    def set_cookies(self, cookies: dict[str, str]) -> None:
        """换 cookie：清掉失效标记、冷却与旧 JWT。"""
        self.cookies = dict(cookies)
        self.invalid_at = 0
        self.invalid_reason = ""
        self._cooldown_until = 0.0
        self._consec = 0
        if self._session is not None:
            self._session.set_cookies(self.cookies)

    # -- 可用性
    def usable(self, now: float) -> bool:
        return self.enabled and not self.invalid_at and self._cooldown_until <= now

    def health(self, now: float) -> tuple[str, str]:
        if not self.enabled:
            return ST_DISABLED, "已停用"
        if self.invalid_at:
            return ST_INVALID, "cookie 失效"
        if self._cooldown_until > now:
            return ST_COOLING, f"冷却 {int(self._cooldown_until - now) + 1}s"
        if self._inflight > 0:
            return ST_BUSY, f"在飞 {self._inflight}"
        if not self.ok_count and not self.fail_count:
            return ST_NEW, "未验证"
        return ST_OK, "可用"

    # -- 结果记账
    def mark_ok(self) -> None:
        self.ok_count += 1
        self.last_ok_at = int(_now())
        self._consec = 0
        self._cooldown_until = 0.0

    def mark_fail(self, exc: ApiError) -> None:
        now = _now()
        self.fail_count += 1
        self.last_fail_at = int(now)
        self.last_error = (exc.message or "")[:300]
        self._consec += 1

        # 只有「上游明确 401」才可能是 cookie 失效。
        # Clerk 刷新失败也抛 CODE_AUTH，但那是网络问题——一次抖动把号全判死是最糟的结果。
        if is_upstream_401(exc):
            if self._consec >= max(1, settings.pool_auth_strikes):
                self.invalid_at = int(now)
                self.invalid_reason = self.last_error
                self._cooldown_until = 0.0
            else:
                self._cooldown_until = now + settings.pool_auth_cooldown
            return

        if exc.http_status == 429:
            self._cooldown_until = now + settings.pool_rate_cooldown
            return

        backoff = min(
            settings.pool_max_cooldown,
            settings.pool_base_cooldown * (2 ** (self._consec - 1)),
        )
        self._cooldown_until = now + backoff

    # -- 序列化
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "cookies": self.cookies,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "note": self.note,
            "invalid_at": self.invalid_at,
            "invalid_reason": self.invalid_reason,
            "ok_count": self.ok_count,
            "fail_count": self.fail_count,
            "last_ok_at": self.last_ok_at,
            "last_fail_at": self.last_fail_at,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Account":
        cookies = raw.get("cookies")
        if not isinstance(cookies, dict) or not cookies:
            raise ValueError("account 缺少 cookies")
        return cls(
            id=str(raw.get("id") or _new_id()),
            name=str(raw.get("name") or "未命名"),
            cookies={str(k): str(v) for k, v in cookies.items()},
            enabled=bool(raw.get("enabled", True)),
            created_at=int(raw.get("created_at") or _now()),
            note=str(raw.get("note") or ""),
            invalid_at=int(raw.get("invalid_at") or 0),
            invalid_reason=str(raw.get("invalid_reason") or ""),
            ok_count=int(raw.get("ok_count") or 0),
            fail_count=int(raw.get("fail_count") or 0),
            last_ok_at=int(raw.get("last_ok_at") or 0),
            last_fail_at=int(raw.get("last_fail_at") or 0),
            last_error=str(raw.get("last_error") or ""),
        )

    def to_public(self, now: float | None = None) -> dict[str, Any]:
        """对外投影：**绝不包含 cookie 值**，只给 cookie 名，够诊断「有没有 __client」。"""
        now = now if now is not None else _now()
        state, label = self.health(now)
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "state": state,
            "state_label": label,
            "note": self.note,
            "cookie_count": len(self.cookies),
            "cookie_names": sorted(self.cookies),
            "missing": [k for k in ("__client", "__client_uat") if k not in self.cookies],
            "ok_count": self.ok_count,
            "fail_count": self.fail_count,
            "last_ok_at": self.last_ok_at,
            "last_fail_at": self.last_fail_at,
            "last_error": self.last_error,
            "invalid_reason": self.invalid_reason,
            "cooldown_left": max(0, int(self._cooldown_until - now)),
            "inflight": self._inflight,
            "created_at": self.created_at,
        }


# --------------------------------------------------------------------------- 池

class AccountPool:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings.accounts_file
        self._lock = threading.RLock()
        self._accounts: dict[str, Account] = {}
        self.load_warning = ""
        self._load()
        if not self._accounts:
            self._import_legacy()

    # -- 持久化
    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            self.load_warning = f"读取号池文件失败：{exc}"
            log.warning(self.load_warning)
            return

        try:
            data = json.loads(raw)
            items = data.get("accounts") if isinstance(data, dict) else data
            accounts = [Account.from_dict(x) for x in (items or [])]
        except Exception as exc:  # noqa: BLE001
            # 不能静默当空池——那等于把所有号悄悄扔掉。留证 + 在面板上报警。
            bad = self.path.with_suffix(f".corrupt-{int(_now())}")
            try:
                os.replace(self.path, bad)
                self.load_warning = f"号池文件损坏（{exc}），已另存为 {bad.name}，请重新导入账号"
            except OSError:
                self.load_warning = f"号池文件损坏（{exc}）且无法另存，请手工检查 {self.path}"
            log.error(self.load_warning)
            return

        for acc in accounts:
            self._accounts[acc.id] = acc

    def _save(self) -> None:
        payload = {
            "version": SCHEMA_VERSION,
            "saved_at": int(_now()),
            "accounts": [a.to_dict() for a in self._accounts.values()],
        }
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as exc:
            # 落盘失败不能影响正在跑的任务，但必须留痕
            log.error("号池写盘失败：%s", exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _import_legacy(self) -> None:
        """老部署升级：把单账号配置（HF_COOKIES_JSON / cookies.json）收编成 1 号。"""
        try:
            cookies = load_cookies()
        except ApiError:
            return
        except Exception:  # noqa: BLE001
            return
        if not cookies:
            return
        acc = Account(id="acc-default", name="default（继承自单账号配置）",
                      cookies=cookies, created_at=int(_now()))
        self._accounts[acc.id] = acc
        self._save()

    # -- 增删查
    def list(self) -> list[Account]:
        with self._lock:
            return list(self._accounts.values())

    def get(self, account_id: str | None) -> Account | None:
        if not account_id:
            return None
        with self._lock:
            return self._accounts.get(account_id)

    def add(self, cookies: dict[str, str], *, name: str = "", enabled: bool = True) -> Account:
        if not cookies:
            raise ApiError("InvalidParameter", "cookie 为空", 400, param="cookie")
        with self._lock:
            acc = Account(
                id=self._unique_id(),
                name=(name or "").strip() or f"账号 {len(self._accounts) + 1}",
                cookies=dict(cookies),
                enabled=enabled,
                created_at=int(_now()),
            )
            self._accounts[acc.id] = acc
            self._save()
            return acc

    def _unique_id(self) -> str:
        for _ in range(8):
            cand = _new_id()
            if cand not in self._accounts:
                return cand
        raise ApiError("InternalServiceError", "账号 id 生成冲突", 500)

    def remove(self, account_id: str) -> bool:
        with self._lock:
            if self._accounts.pop(account_id, None) is None:
                return False
            self._save()
            return True

    def replace_cookies(self, account_id: str, cookies: dict[str, str]) -> Account:
        """换掉某号的 cookie 并清掉失效标记：丢了号不用删了重建，统计与任务归属都还在。"""
        if not cookies:
            raise ApiError("InvalidParameter", "cookie 为空", 400, param="cookie")
        with self._lock:
            acc = self._accounts.get(account_id)
            if acc is None:
                raise ApiError("NotFound", f"账号不存在：{account_id}", 404)
            acc.set_cookies(cookies)
            self._save()
            return acc

    def save(self) -> None:
        with self._lock:
            self._save()

    def update(self, account_id: str, **fields: Any) -> Account:
        with self._lock:
            acc = self._accounts.get(account_id)
            if acc is None:
                raise ApiError("NotFound", f"账号不存在：{account_id}", 404)
            if "name" in fields and fields["name"] is not None:
                acc.name = str(fields["name"]).strip() or acc.name
            if "enabled" in fields and fields["enabled"] is not None:
                acc.enabled = bool(fields["enabled"])
                if acc.enabled:
                    # 重新启用 = 人在说「再试一次」：清掉失效标记与冷却，给它一次干净的机会。
                    # 判错了也没关系——真失效的话下一次调用会重新判死。
                    acc.invalid_at = 0
                    acc.invalid_reason = ""
                    acc._cooldown_until = 0.0
                    acc._consec = 0
            if "note" in fields and fields["note"] is not None:
                acc.note = str(fields["note"]).strip()
            if fields.get("reset_invalid"):
                acc.invalid_at = 0
                acc.invalid_reason = ""
                acc._cooldown_until = 0.0
                acc._consec = 0
            self._save()
            return acc

    # -- 调度
    def acquire(self, exclude: frozenset[str] = frozenset()) -> Account:
        """取出一个可用账号并**占用**（inflight +1）。用完必须 `release`。"""
        with self._lock:
            now = _now()
            cands = [a for a in self._accounts.values() if a.id not in exclude and a.usable(now)]
            if not cands:
                raise ApiError("NoAvailableAccount", self._no_account_reason(now), 503)

            cands.sort(key=lambda a: (a._inflight, a._last_used))
            acc = cands[0]
            acc._inflight += 1
            acc._last_used = now
            return acc

    def pick(self, preferred: str | None = None) -> Account | None:
        """只读查询用：不占 inflight，允许挑一个当前不可用但曾经正常的号。"""
        with self._lock:
            if preferred and preferred in self._accounts:
                return self._accounts[preferred]
            now = _now()
            usable = [a for a in self._accounts.values() if a.usable(now)]
            if usable:
                usable.sort(key=lambda a: (a._inflight, a._last_used))
                return usable[0]
            alive = [a for a in self._accounts.values() if a.enabled]
            return alive[0] if alive else None

    def _no_account_reason(self, now: float) -> str:
        total = len(self._accounts)
        if total == 0:
            return "号池为空：打开 /panel 导入至少一个账号的 cookie（Copy as cURL）"
        buckets = {"停用": 0, "失效": 0, "冷却": 0}
        for a in self._accounts.values():
            state, _ = a.health(now)
            if state == ST_DISABLED:
                buckets["停用"] += 1
            elif state == ST_INVALID:
                buckets["失效"] += 1
            elif state == ST_COOLING:
                buckets["冷却"] += 1
        detail = "、".join(f"{k} {v}" for k, v in buckets.items() if v)
        return f"号池里没有可用账号（共 {total} 个：{detail or '全部异常'}）。打开 /panel 处理"

    def release(self, acc: Account, *, ok: bool, exc: ApiError | None = None) -> None:
        with self._lock:
            acc._inflight = max(0, acc._inflight - 1)
            if ok:
                acc.mark_ok()
            elif exc is not None:
                acc.mark_fail(exc)
            self._save()

    def note(self, acc: Account, *, ok: bool, exc: ApiError | None = None) -> None:
        """不占 inflight 的记账（自检用）。"""
        with self._lock:
            if ok:
                acc.mark_ok()
            elif exc is not None:
                acc.mark_fail(exc)
            self._save()

    # -- 自检
    def probe(self, account_id: str) -> dict[str, Any]:
        """单号零计费自检：GET /jobs?size=1，不建任务、不花钱。"""
        acc = self.get(account_id)
        if acc is None:
            raise ApiError("NotFound", f"账号不存在：{account_id}", 404)

        t0 = _now()
        try:
            info = acc.session.whoami(timeout=settings.probe_timeout)
        except ApiError as exc:
            self.note(acc, ok=False, exc=exc)
            return {"id": acc.id, "name": acc.name, "ok": False, "status": exc.http_status,
                    "detail": exc.message, "ms": int((_now() - t0) * 1000)}
        except Exception as exc:  # noqa: BLE001 网络层任何异常都不该把面板打挂
            err = ApiError("NoAvailableService", f"{type(exc).__name__}: {exc}", 502)
            self.note(acc, ok=False, exc=err)
            return {"id": acc.id, "name": acc.name, "ok": False, "status": 0,
                    "detail": str(exc), "ms": int((_now() - t0) * 1000)}

        ok = bool(info.get("ok"))
        if not ok:
            err = ApiError("AuthenticationError", str(info.get("detail") or "自检失败")[:300],
                           int(info.get("status") or 401))
            self.note(acc, ok=False, exc=err)
        else:
            self.note(acc, ok=True)
        return {
            "id": acc.id,
            "name": acc.name,
            "ok": ok,
            "status": info.get("status"),
            "jwt_exp_in": info.get("jwt_exp_in"),
            "detail": info.get("detail") or acc.last_error or None,
            "ms": int((_now() - t0) * 1000),
        }

    def probe_all(self) -> list[dict[str, Any]]:
        """自检全部启用账号，最多 4 路并发。

        完全串行会让 10 个号 × 12 秒超时 = 两分钟，浏览器和 nginx 都等不到；
        并发再高就容易触发上游风控。4 是这两个约束的折中。
        """
        targets = [a for a in self.list() if a.enabled]
        if not targets:
            return []
        for a in targets:
            _ = a.session          # 预热：避免多线程同时首建同一个 session
        with ThreadPoolExecutor(max_workers=min(4, len(targets))) as ex:
            return list(ex.map(self.probe, [a.id for a in targets]))

    def reload(self) -> dict[str, Any]:
        """从磁盘重读号池（多副本/手工改文件后用）。"""
        with self._lock:
            before = set(self._accounts)
            self._accounts.clear()
            self.load_warning = ""
            self._load()
            if not self._accounts:
                self._import_legacy()
            after = set(self._accounts)
            return {"loaded": len(after), "added": sorted(after - before),
                    "removed": sorted(before - after), "warning": self.load_warning}

    # -- 概览
    def summary(self) -> dict[str, Any]:
        now = _now()
        accs = self.list()
        states = [a.health(now)[0] for a in accs]
        usable = sum(1 for a in accs if a.usable(now))
        return {
            "total": len(accs),
            "usable": usable,
            "invalid": states.count(ST_INVALID),
            "disabled": states.count(ST_DISABLED),
            "cooling": states.count(ST_COOLING),
            "warning": self.load_warning,
            "strategy": "最少在飞 + 最久未用（轮询）",
            "retry": settings.pool_retry,
        }


# 进程级单例。测试里用 AccountPool(tmp_path) 单独构造，不碰这个。
pool = AccountPool()
