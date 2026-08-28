"""默认只预览；停服务并备份后，使用 --apply --workers-stopped 明确应用。"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_write_engine  # noqa: E402
from app.task_schema import migrate_task_schema, migration_statements  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--workers-stopped", action="store_true", help="确认所有旧 Worker/API/调度器已停止，且已备份")
    args = parser.parse_args()
    if args.apply and not args.workers_stopped:
        parser.error("应用升级前必须停服务、备份，并传入 --workers-stopped")
    try:
        engine = get_write_engine()
        statements = migrate_task_schema(engine) if args.apply else migration_statements(engine)
        for statement in statements:
            print(statement)
        print("已应用" if args.apply else "仅预览，未修改数据库")
        if not statements:
            print("无需增列；若为新库，启动时将创建完整表")
    except Exception as error:
        print(f"升级检查/执行失败: {type(error).__name__}；请检查数据库权限和连接（未输出连接凭证）", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
