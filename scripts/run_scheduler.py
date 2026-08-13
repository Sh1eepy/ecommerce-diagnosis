"""常驻调度器：每天定时自动跑异常检测 + 自动入队诊断（全自动闭环的一环）。

用法：
  python scripts/run_scheduler.py                  # 每天 00:00 检测
  python scripts/run_scheduler.py --hour 8 --minute 30 --days 7

说明：检测窗口默认"今天往前 days 天"；对本数据集（历史固定数据）可用
      scripts/run_detection.py --end 2015-09-18 手动扫历史窗口。
      生产环境中数据是每日更新的，调度器会每天自动发现新异常并触发 Agent 诊断。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apscheduler.schedulers.blocking import BlockingScheduler  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # noqa: E402

from app.db import init_db  # noqa: E402
from app.detection.detector import run_detection  # noqa: E402


def make_job(days: int):
    def _job() -> None:
        init_db()
        end = date.today()
        start = end - timedelta(days=days + 7)  # 前扩 7 天支持周环比
        try:
            n = run_detection(start, end, auto_diagnose=True)
        except Exception as e:  # noqa: BLE001  检测失败不中断调度器
            print(f"[{datetime.now()}] 检测异常: {type(e).__name__}: {e}", flush=True)
            return
        print(f"[{datetime.now()}] 检测完成（{start}~{end}），新异常 {n} 个，已自动入队诊断", flush=True)

    return _job


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hour", type=int, default=0, help="每天几点跑（0-23）")
    ap.add_argument("--minute", type=int, default=0, help="每天几分跑（0-59）")
    ap.add_argument("--days", type=int, default=7, help="检测窗口天数（往前 N 天）")
    args = ap.parse_args()

    init_db()
    scheduler = BlockingScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(make_job(args.days), CronTrigger(hour=args.hour, minute=args.minute),
                      id="daily_detection", replace_existing=True)
    print(f"调度器启动：每天 {args.hour:02d}:{args.minute:02d} 自动检测（窗口 {args.days} 天）", flush=True)
    print("（Ctrl+C 停止）", flush=True)
    try:
        scheduler.start()
    except KeyboardInterrupt:
        print("调度器已停止")


if __name__ == "__main__":
    main()
