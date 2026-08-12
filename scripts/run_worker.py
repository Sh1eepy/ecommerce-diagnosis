"""启动任务 Worker。

用法：
  python scripts/run_worker.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import init_db  # noqa: E402
from app.tasks import run_worker  # noqa: E402


def main() -> None:
    init_db()
    print("Worker 启动，Ctrl+C 停止 ...")
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        print("Worker 已停止")


if __name__ == "__main__":
    main()
