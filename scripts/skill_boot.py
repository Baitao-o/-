#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦门省钱搭子 —— Skill 启动器（供 AI / 用户"一句话拉起前端"）。

用途
----
Skill 被触发后，AI 不应该让用户自己去敲 `python web/server.py`，
而是直接执行本脚本，拿到一句可以直接点开的 URL。

行为
----
1. 先探测 8808~8820 有没有本服务在跑（校验 /api/health 的 service 名，避免误判成别的程序）；
2. 已在跑 → 直接返回它的 URL，不重复启动；
3. 没在跑 → 后台拉起 `web/server.py`（脱离父进程，关掉终端也不会停），
   轮询到就绪后返回 URL；
4. 端口被占用时由 server.py 自动往后找，最终以实际地址为准。

用法
----
    python scripts/skill_boot.py              # 启动并打印可点开的地址（会尝试打开浏览器）
    python scripts/skill_boot.py --json       # 机器可读，供 AI 解析
    python scripts/skill_boot.py --no-browser  # 只起服务，不自动开浏览器
    python scripts/skill_boot.py --status      # 只探测，不启动
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        if _s.encoding and _s.encoding.lower().replace("-", "") != "utf8":
            _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_PY = os.path.join(ROOT, "web", "server.py")
SERVICE_NAME = "maimen-budget-meal"
PORT_START = int(os.environ.get("PORT", "8808"))
PORT_TRIES = 13


def _health(port: int, timeout: float = 0.4) -> bool:
    """确认该端口上跑的是**本服务**，不是别的程序。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("service") == SERVICE_NAME
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return False


def find_running() -> int | None:
    for port in range(PORT_START, PORT_START + PORT_TRIES):
        if _health(port):
            return port
    return None


def start_background(open_browser: bool) -> None:
    """后台拉起服务，脱离当前进程（关终端也不影响）。"""
    if not os.path.isfile(SERVER_PY):
        raise SystemExit(f"找不到服务文件：{SERVER_PY}")

    env = dict(os.environ)
    env["OPEN_BROWSER"] = "1" if open_browser else "0"
    kwargs: dict = {
        "cwd": ROOT,
        "env": env,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：让服务独立于本终端存活
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen([sys.executable, SERVER_PY, ], **kwargs)


def wait_ready(timeout: float = 30.0) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        port = find_running()
        if port:
            return port
        time.sleep(0.6)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="麦门省钱搭子 · Skill 启动器")
    ap.add_argument("--json", action="store_true", help="输出 JSON（供 AI 解析）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--status", action="store_true", help="只探测状态，不启动")
    ap.add_argument("--timeout", type=float, default=30.0, help="等待就绪超时秒数")
    args = ap.parse_args()

    port = find_running()
    started = False
    action = "already_running"

    if port is None and args.status:
        if args.json:
            print(json.dumps({"ok": False, "running": False,
                              "hint": "服务未运行，去掉 --status 即可启动"}, ensure_ascii=False))
        else:
            print("服务未运行。执行 `python scripts/skill_boot.py` 可启动。")
        return 1

    if port is None:
        start_background(open_browser=not args.no_browser)
        port = wait_ready(args.timeout)
        started = True
        action = "started"
        if port is None:
            msg = f"启动失败：等了 {args.timeout:.0f} 秒服务仍未就绪，请查看 web/server.py 的输出"
            if args.json:
                print(json.dumps({"ok": False, "running": False, "error": msg}, ensure_ascii=False))
            else:
                print(f"[失败] {msg}", file=sys.stderr)
            return 2

    url = f"http://127.0.0.1:{port}/"

    if args.json:
        print(json.dumps({
            "ok": True,
            "running": True,
            "started": started,
            "action": action,
            "port": port,
            "url": url,
            "tabs": [
                {"name": "按结构配 · 求最低价", "hint": "填预算 + 主食/小吃/甜品/饮品份数"},
                {"name": "帮我搭配 · 推荐方案", "hint": "只填预算，出 2~3 个方案"},
                {"name": "🎰 美食转盘 · 随机抽", "hint": "四个盘各转一格，抽完真实算价"},
            ],
        }, ensure_ascii=False, indent=2))
        return 0

    print()
    print("=" * 58)
    print("  麦门省钱搭子 已就绪")
    print("=" * 58)
    print(f"  浏览器打开： {url}")
    if started:
        print("  （首次启动，浏览器会自动弹出；没弹就手动点上面的地址）")
    else:
        print("  （服务本来就在运行，直接点开即可）")
    print()
    print("  三个模式：")
    print("    · 按结构配 · 求最低价   —— 填预算 + 主食/小吃/甜品/饮品各几份")
    print("    · 帮我搭配 · 推荐方案   —— 只填预算，出 2~3 个方案")
    print("    · 🎰 美食转盘 · 随机抽 —— 四个盘各转一格，抽完用真实算价结算")
    print()
    print("  不想开网页？直接在对话里说需求，或用命令行：")
    print("    python src/meal_planner.py --people 2 --budget 100 --mode pickup \\")
    print("      --city 绍兴 --keyword 上虞")
    print("=" * 58)
    return 0


if __name__ == "__main__":
    sys.exit(main())
