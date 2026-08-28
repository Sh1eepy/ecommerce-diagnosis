"""测试基础设施：强制走 SQLite 临时库，避免污染 MySQL 与真实日志。

临时目录用项目内相对路径（tests/.pytest_runtime）而非系统 Temp：
- 中文项目路径经环境变量（TMP/TEMP）传递时可能被损坏（编码问题），
  改用相对路径后完全规避；
- 沙箱/CI 环境下对项目内目录可写，测试在任何环境可跑。
"""
import os
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

_tmpdir = Path(__file__).resolve().parent / ".pytest_runtime"
_tmpdir.mkdir(parents=True, exist_ok=True)
os.environ["DB_DRIVER"] = "sqlite"
os.environ["SQLITE_PATH"] = str(_tmpdir / "test.db")
os.environ["LLM_API_KEY"] = ""  # 空 Key → 强制 MockLLM
os.environ["APP_ENV"] = "test"
os.environ["API_KEY_SCOPES"] = "{}"
os.environ["ALERT_WEBHOOK_URL"] = ""
os.environ["ALERT_WEBHOOK_SECRET"] = ""
os.environ["LOG_DIR"] = str(_tmpdir / "logs")
os.environ["API_KEYS"] = "test-key"
os.environ["TASK_POLL_INTERVAL_SECONDS"] = "0.05"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from app.db import get_write_engine, init_db, write_session  # noqa: E402
from app.models import DailyItemStat, ItemCategory, ItemPrice  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_rate_limit():
    from app.security import _requests

    _requests.clear()
    yield
    _requests.clear()


@pytest.fixture(scope="session", autouse=True)
def _setup_db():
    # 全新环境：清掉可能残留的旧库/日志（Windows 下文件占用可能使上次 rmtree 失败）
    for p in (_tmpdir / "test.db", _tmpdir / "logs"):
        try:
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass
    init_db()
    _seed_daily_stats()
    yield
    shutil.rmtree(_tmpdir, ignore_errors=True)  # 会话结束清理自建临时目录


def _seed_daily_stats() -> None:
    """构造演示数据（幂等：先清空再插入，避免残留库导致 UNIQUE 冲突）：
    - item 1: 前 10 天平稳（cvr=8%），后 4 天 cvr 连续下降 7→6→5→4% → 触发规则
    - item 2: 平稳，不触发
    同时写入 day_type / new_user 维度切片供 Tool 测试。
    """
    base = date(2015, 6, 1)
    with write_session() as s:
        s.query(DailyItemStat).delete()
        s.query(ItemCategory).delete()
        s.query(ItemPrice).delete()
        for item in (1, 2):
            for i in range(14):
                d = base + timedelta(days=i)
                trans = [70, 60, 50, 40][i - 10] if (item == 1 and i >= 10) else 80
                uv, view, add = 1000, 3000, 90

                s.add(DailyItemStat(
                    item_id=item, stat_date=d, dimension_type="all", dimension="all",
                    uv=uv, view_count=view, click_count=view,
                    addtocart_count=add, transaction_count=trans, gmv=float(trans),
                ))

                dim = "weekend" if d.weekday() >= 5 else "workday"
                s.add(DailyItemStat(
                    item_id=item, stat_date=d, dimension_type="day_type", dimension=dim,
                    uv=uv, view_count=view, click_count=view,
                    addtocart_count=add, transaction_count=trans, gmv=float(trans),
                ))

                new_uv, new_add = (400, add // 2)
                ret_uv = uv - new_uv
                s.add(DailyItemStat(
                    item_id=item, stat_date=d, dimension_type="new_user", dimension="new",
                    uv=new_uv, view_count=view, click_count=view,
                    addtocart_count=new_add, transaction_count=trans // 2, gmv=float(trans // 2),
                ))
                s.add(DailyItemStat(
                    item_id=item, stat_date=d, dimension_type="new_user", dimension="returning",
                    uv=ret_uv, view_count=view, click_count=view,
                    addtocart_count=add - new_add, transaction_count=trans - trans // 2,
                    gmv=float(trans - trans // 2),
                ))

                # category 维度（两个商品同属类目 100，数据与 all 一致）
                s.add(DailyItemStat(
                    item_id=item, stat_date=d, dimension_type="category", dimension="100",
                    uv=uv, view_count=view, click_count=view,
                    addtocart_count=add, transaction_count=trans, gmv=float(trans),
                ))

        s.add(ItemCategory(item_id=1, category_id=100))
        s.add(ItemCategory(item_id=2, category_id=100))
        s.add(ItemPrice(item_id=1, price=100.0, ts_ms=0))
        s.add(ItemPrice(item_id=2, price=50.0, ts_ms=0))
        s.commit()
