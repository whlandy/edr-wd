#!/usr/bin/env python3
"""双击打开本机的 EDR-WD 录制面板（目标机本地入口）。

这是给「在目标机前操作的人」用的入口，不经过 Agent、不经过 LLM：

    python3 open_recorder.py [--url http://127.0.0.1:8765/mcp]

行为：
- 本机 MCP 服务在跑  → 打开/恢复录制面板（面板在交互桌面上，不在 Agent 机上）；
- 服务没在跑        → 打印启动指引后以退出码 1 结束，不假报成功。

桌面快捷方式安装（一次性）：
- Windows：右键本脚本 → 发送到桌面快捷方式；目标改为
  `<python> <仓库>\\scripts\\open_recorder.py`，起始位置为仓库根。
- macOS：新建 `open_recorder.command`，内容两行
  `cd "$(dirname "$0")/.." && python3 scripts/open_recorder.py`，
  `chmod +x open_recorder.command` 后放到桌面。

面板与录制状态由已在运行的目标服务持有；本脚本只负责「叫它把面板亮出来」。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from urllib.parse import urlsplit


def _host_reachable(url: str) -> bool:
    parts = urlsplit(url)
    host, port = parts.hostname or "127.0.0.1", parts.port or 8765
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp",
                        help="本机 EDR-WD MCP 服务地址")
    args = parser.parse_args(argv)

    if not _host_reachable(args.url):
        print("EDR-WD 目标服务没有在运行，未能打开录制面板。")
        print("启动指引：在目标机上执行 `python server.py`（或既有的服务安装方式），")
        print(f"确认 {args.url} 可达后再运行本脚本。")
        return 1   # 未能打开面板就是失败；指引不冒充操作成功

    try:
        from fastmcp import Client
    except ImportError:
        print("缺少 fastmcp（pip install fastmcp）；无法连接本机 MCP 服务。")
        return 1

    import asyncio

    async def _open() -> tuple[int, str]:
        # 已安装的 fastmcp Client 仅支持异步上下文（无 __enter__），
        # 同步入口在这里显式驱动事件循环。
        async with Client(args.url) as client:
            result = await client.call_tool("open_recording_ui", {})
            payload = json.loads(result.content[0].text)
            if not payload.get("ok"):
                return 1, f"打开面板失败：{payload.get('error') or payload}"
            snapshot = payload.get("snapshot") or {}
            lines = ["录制面板已在本机交互桌面打开。",
                     f"当前状态：{snapshot.get('viewState')}"]
            pending = snapshot.get("pendingResults") or []
            if pending:
                lines.append(f"有 {len(pending)} 份已停止录制的结果待 Agent 收集。")
            return 0, "\n".join(lines)

    try:
        code, message = asyncio.run(_open())
        print(message)
        return code   # 成功=0 必须来自服务端真实回执；失败=1 如实呈现
    except Exception as exc:  # noqa: BLE001 - entry point must stay readable
        print(f"未能打开录制面板：{exc}")
        print("启动指引：确认目标服务在运行（或其隧道端口可达）后重试本脚本；")
        print("也可在 Agent 侧使用 `python3 -m agent.cli --target <t> record ui`。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
