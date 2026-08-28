"""启动任务 Worker。

用法：
  python scripts/run_worker.py
"""
from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import init_db  # noqa: E402
from app.tasks import run_worker  # noqa: E402


async def _serve() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop(signum, frame):
        print("Worker 停止领取，正在等待已领取的任务结束 ...", flush=True)
        loop.call_soon_threadsafe(stop.set)

    previous = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        await run_worker(stop)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main() -> None:
    init_db()
    print("Worker 启动，Ctrl+C 停止领取并等待在途任务结束 ...")
    asyncio.run(_serve())
    print("Worker 已停止")


if __name__ == "__main__":
    main()
