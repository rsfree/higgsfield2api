"""把浏览器里的 Higgsfield 会话倒进本机，并零计费验证。

用法：
  # 1) 在 higgsfield.ai 登录后，DevTools → Console 执行 copy(document.cookie)
  #    然后： python -m tools.cookie_import --clipboard
  # 2) 或者把 Cookie-Editor / EditThisCookie 导出的 JSON 存成文件：
  #    python -m tools.cookie_import --file cookies.json
  # 3) 验证（只打 GET /jobs?size=1，不消耗额度）：
  #    python -m tools.cookie_import --verify

注意：`document.cookie` 拿不到 HttpOnly 的 __client。若 --verify 报「会话为空」，
改用 DevTools → Network → 任意 XHR 请求 → 右键 Copy as cURL，把整段 curl 存成文件后用 --curl。
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
from hfapi.session import Session, load_cookies  # noqa: E402


def _from_clipboard() -> str:
    if sys.platform == "win32":
        return subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                              capture_output=True, text=True, check=True).stdout
    return subprocess.run(["pbpaste"], capture_output=True, text=True, check=True).stdout


def _cookies_from_curl(text: str) -> dict[str, str]:
    m = re.search(r"-H\s+'cookie:\s*([^']+)'", text, re.I)
    if not m:
        m = re.search(r'-H\s+"cookie:\s*([^"]+)"', text, re.I)
    if not m:
        raise SystemExit("这段 curl 里没有 -H 'cookie: ...'，请在登录态下复制一个 XHR 请求")
    out = {}
    for chunk in m.group(1).split(";"):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clipboard", action="store_true", help="从剪贴板读取 document.cookie")
    ap.add_argument("--file", help="cookie JSON / Netscape txt 路径")
    ap.add_argument("--curl", help="包含 cookie 头的 curl 文本文件（可拿到 HttpOnly）")
    ap.add_argument("--verify", action="store_true", help="验证会话（零计费）")
    ap.add_argument("--out", default=str(settings.cookie_path()))
    args = ap.parse_args()

    if args.verify:
        try:
            print(json.dumps(Session().whoami(), ensure_ascii=False, indent=2))
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"ok": False, "detail": str(exc)}, ensure_ascii=False, indent=2))
            raise SystemExit(2)
        return

    if args.curl:
        cookies = _cookies_from_curl(Path(args.curl).read_text(encoding="utf-8", errors="replace"))
    elif args.file:
        cookies = load_cookies(Path(args.file))
    elif args.clipboard:
        raw = _from_clipboard().strip()
        tmp = Path(args.out).with_suffix(".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(raw, encoding="utf-8")
        cookies = load_cookies(tmp)
        tmp.unlink(missing_ok=True)
    else:
        ap.error("需要 --clipboard / --file / --curl / --verify 之一")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
    print(f"已写入 {out}（{len(cookies)} 个 cookie）")

    missing = [k for k in ("__client", "__client_uat") if k not in cookies]
    if missing:
        print(f"⚠️ 缺少 {missing} —— Clerk 会话多半拿不到，建议改用 --curl 的方式")

    try:
        print(json.dumps(Session().whoami(), ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "detail": str(exc)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
