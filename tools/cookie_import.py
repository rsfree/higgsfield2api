"""把浏览器里的 Higgsfield 会话倒进号池，并零计费验证。

推荐直接用面板（`/panel` → 号池 → 粘贴 Copy as cURL），本工具是脚本化等价路径。

用法：
  # 1) 加一个号（默认进号池）。DevTools → Network → 任意 XHR → Copy as cURL → 存成 req.txt
  python -m tools.cookie_import --curl req.txt --name "免费额度A"

  # 2) 从管道读（服务器上常用，不必落临时文件）
  docker compose exec -T higgsfield2api python -m tools.cookie_import --curl - --name 号1 < req.txt

  # 3) 一次性加多个号：多段 cURL 用空行或直接连着贴，按「行首 curl」自动切分
  python -m tools.cookie_import --curl batch.txt

  # 4) 只验证号池第一个可用号（零计费）
  python -m tools.cookie_import --verify

  # 5) 老行为：写单文件（兼容旧脚本；号池为空时会被自动收编成 acc-default）
  python -m tools.cookie_import --curl req.txt --out secrets/cookies.json

注意：`document.cookie` 拿不到 HttpOnly 的 `__client`。若自检报「会话为空」，
说明导错了来源，必须用 Copy as cURL。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hfapi.config import settings  # noqa: E402
from hfapi.pool import pool  # noqa: E402
from hfapi.session import (  # noqa: E402
    Session,
    cookies_from_curl,
    looks_like_curl,
    parse_cookies,
    require_session_cookies,
)

def _from_clipboard() -> str:
    if sys.platform == "win32":
        return subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                              capture_output=True, text=True, check=True).stdout
    return subprocess.run(["pbpaste"], capture_output=True, text=True, check=True).stdout


def _read_curl_source(spec: str) -> str:
    """`-` 或 `/dev/stdin` 都表示标准输入。"""
    if spec in {"-", "/dev/stdin"}:
        return sys.stdin.read()
    return Path(spec).read_text(encoding="utf-8", errors="replace")


def _segments(text: str) -> list[str]:
    """多段 cURL 按「行首 curl」切分；不是 cURL 时整段当 cookie 串。"""
    parts = [p.strip() for p in re.split(r"(?m)^[ \t]*(?=curl\s)", text) if p.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def _parse(segment: str) -> dict[str, str]:
    if looks_like_curl(segment) or re.search(r"(?:^|\s)(?:-b|--cookie)\s+['\"]", segment):
        cookies = cookies_from_curl(segment)
    else:
        cookies = parse_cookies(segment, source="输入")
    return require_session_cookies(cookies, source="输入")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clipboard", action="store_true", help="从剪贴板读取 document.cookie")
    ap.add_argument("--file", help="cookie JSON / Netscape txt 路径")
    ap.add_argument("--curl", help="包含 cookie 头的 curl 文本文件（可拿到 HttpOnly）；`-` = stdin")
    ap.add_argument("--name", default="", help="账号备注名（进号池时用；批量时自动加 -1/-2 后缀）")
    ap.add_argument("--verify", action="store_true", help="验证号池里的号（零计费）")
    ap.add_argument("--out", default="", help="写单文件而不是加入号池（兼容老脚本）")
    args = ap.parse_args()

    if args.verify:
        acc = pool.pick()
        if acc is None:
            print(json.dumps({"ok": False, "detail": "号池为空，先导入一个号"}, ensure_ascii=False))
            raise SystemExit(2)
        print(json.dumps(pool.probe(acc.id), ensure_ascii=False, indent=2))
        return

    # -- 取 cookie
    source: str
    if args.curl:
        source = _read_curl_source(args.curl)
    elif args.file:
        source = Path(args.file).read_text(encoding="utf-8", errors="replace")
    elif args.clipboard:
        source = _from_clipboard()
    else:
        ap.error("需要 --clipboard / --file / --curl / --verify 之一")

    segments = _segments(source)
    parsed: list[dict[str, str]] = []
    for i, seg in enumerate(segments, 1):
        try:
            parsed.append(_parse(seg))
        except Exception as exc:  # noqa: BLE001
            print(f"第 {i} 段解析失败，跳过：{exc}")

    if not parsed:
        print(json.dumps({"ok": False, "detail": "一段 cookie 都没解析出来"}, ensure_ascii=False))
        raise SystemExit(2)

    for cookies in parsed:
        if "__client_uat" not in cookies:
            print("提示：没有 __client_uat，一般仍可用（刷新 JWT 只依赖 __client）")

    # -- 落库
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(parsed[0], indent=2), encoding="utf-8")
        out.chmod(0o600)
        print(f"已写入 {out}（{len(parsed[0])} 个 cookie）")
        s = Session()
        s.set_cookies(parsed[0])
        try:
            print(json.dumps(s.whoami(), ensure_ascii=False, indent=2))
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"ok": False, "detail": str(exc)}, ensure_ascii=False, indent=2))
        return

    added = []
    for i, cookies in enumerate(parsed, 1):
        label = f"{args.name}-{i}" if args.name and len(parsed) > 1 else args.name
        added.append(pool.add(cookies, name=label))
        print(f"已加入号池：{added[-1].id}  {added[-1].name}（{len(cookies)} 个 cookie）")

    # -- 零计费自检
    for acc in added:
        print(json.dumps(pool.probe(acc.id), ensure_ascii=False, indent=2))

    print(f"\n号池共 {len(pool.list())} 个号，"
          f"{pool.summary()['usable']} 个可用。管理入口：/panel")


if __name__ == "__main__":
    main()
