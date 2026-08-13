"""监控指标 CLI：打印最近 N 小时运行时指标。

用法：
  python scripts/monitoring_report.py --hours 24
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.monitoring import collect_monitoring  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    args = ap.parse_args()
    print(json.dumps(collect_monitoring(args.hours), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
