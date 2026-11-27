"""运行时配置。全部来自环境变量，任何密钥都不落代码、不落库。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

try:  # 可选依赖：有 .env 就加载，没有也不影响
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except Exception:  # pragma: no cover
    pass


def _path(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    if not raw:
        return default
    p = Path(raw)
    return p if p.is_absolute() else (BASE_DIR / p).resolve()


def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    v = os.getenv(name)
    if not v:
        return default
    return int(v)


@dataclass(frozen=True)
class Settings:
    # ---- 上游 ----
    gateway: str = os.getenv("HF_GATEWAY", "https://fnf-api-gw.higgsfield.ai/fnf")
    site: str = os.getenv("HF_SITE", "https://higgsfield.ai")
    clerk_proxy: str = os.getenv("HF_CLERK_PROXY", "https://higgsfield.ai/_clerk")
    clerk_js_version: str = os.getenv("HF_CLERK_JS_VERSION", "6.46.3")
    impersonate: str = os.getenv("HF_IMPERSONATE", "chrome")

    # ---- 凭据（只放路径） ----
    cookies_file: Path = _path("HF_COOKIES_FILE", BASE_DIR / "secrets" / "cookies.json")

    # ---- 计费红线 ----
    allow_billing: bool = _bool("ALLOW_BILLING", False)
    use_free_gens: bool = _bool("USE_FREE_GENS", True)
    max_duration: int = _int("MAX_DURATION", 5)          # 单任务最长秒数，防止误烧额度
    max_batch: int = _int("MAX_BATCH", 1)                # 单任务并发条数
    max_credits_per_task: int = _int("MAX_CREDITS_PER_TASK", 10)

    # ---- 服务 ----
    task_db: Path = _path("TASK_DB", BASE_DIR / "data" / "tasks.db")
    # 空 = 不鉴权（只允许绑回环）。对外发布时必须设置，否则任何人都能建计费任务。
    service_token: str = (os.getenv("SERVICE_TOKEN") or "").strip()
    request_timeout: int = _int("REQUEST_TIMEOUT", 60)
    poll_interval: float = float(os.getenv("POLL_INTERVAL", "5"))
    execution_expires_after: int = _int("EXECUTION_EXPIRES_AFTER", 172800)

    extra_headers: dict = field(default_factory=lambda: json.loads(os.getenv("HF_EXTRA_HEADERS", '{"hf-surface":"web"}')))

    def cookie_path(self) -> Path:
        return self.cookies_file


settings = Settings()
