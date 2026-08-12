"""导入 Retailrocket 数据集 → MySQL，并聚合出 daily_item_stat。

流程：
  1. events.csv 分块加载 → raw_events（staging 表，2.76M 事件）
  2. category_tree.csv → category_tree 表
  3. 计算 visitor_first_seen（新老用户判定）
  4. SQL 聚合 → daily_item_stat，生成三个维度切片：
     - all         整体
     - day_type    workday / weekend
     - new_user    new / returning

设计说明：
- 原始数据保留在 raw_events，便于重新聚合与 SQL 核对（"数据错"可追溯）
- 聚合用 SQL 而非 Python 内存计算，符合"SQL 负责发现问题"的原则
- item_properties 890MB 且多为编码属性、价值低，本版不导入

用法：
  python scripts/import_retailrocket.py
"""
from __future__ import annotations

import csv
import re
import sys
from math import isnan
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from sqlalchemy import text

from app.db import get_write_engine, init_db


def _to_int(v) -> int:
    """兼容 NaN/None → 0（根分类无父节点）。"""
    try:
        f = float(v)
        return int(f) if not isnan(f) else 0
    except (ValueError, TypeError):
        return 0

DATA_DIR = Path(__file__).resolve().parent.parent / "数据"
EVENTS_CSV = DATA_DIR / "events.csv"
CATEGORY_CSV = DATA_DIR / "category_tree.csv"
ITEM_PROPS_FILES = [DATA_DIR / "item_properties_part1.csv", DATA_DIR / "item_properties_part2.csv"]
CHUNK = 300_000

# 价格属性：item_properties 中 prop=790 全为纯数值（如 n5400.000）
PRICE_PROP = "790"
_NUM_PAT = re.compile(r"^n(-?\d+)\.\d{3}$")

AGG_COLS = (
    "item_id, stat_date, dimension_type, dimension, uv, view_count, "
    "click_count, addtocart_count, transaction_count, gmv"
)

# 聚合基数：COUNT(DISTINCT visitor_id) 等（注意别名冲突时需加 r. 前缀）
BASE_COLS = (
    "COUNT(DISTINCT visitor_id) AS uv, "
    "SUM(event='view') AS view_count, "
    "SUM(event='view') AS click_count, "
    "SUM(event='addtocart') AS addtocart_count, "
    "SUM(event='transaction') AS transaction_count, "
    "SUM(event='transaction') AS gmv"
)
# 新老用户查询专用（含 r. 前缀避免 visitor_id 歧义）
BASE_COLS_R = (
    "COUNT(DISTINCT r.visitor_id) AS uv, "
    "SUM(r.event='view') AS view_count, "
    "SUM(r.event='view') AS click_count, "
    "SUM(r.event='addtocart') AS addtocart_count, "
    "SUM(r.event='transaction') AS transaction_count, "
    "SUM(r.event='transaction') AS gmv"
)


def load_events(engine) -> None:
    print("[1/5] 加载 events.csv -> raw_events ...")
    total = 0
    for i, chunk in enumerate(pd.read_csv(EVENTS_CSV, chunksize=CHUNK, dtype={"transactionid": str})):
        df = pd.DataFrame({
            "ts_ms": chunk["timestamp"],
            "visitor_id": chunk["visitorid"],
            "event": chunk["event"],
            "item_id": chunk["itemid"],
            "transaction_id": chunk["transactionid"].fillna(""),
            "event_date": pd.to_datetime(chunk["timestamp"], unit="ms").dt.date,
        })
        df.to_sql("raw_events", engine, if_exists="append", index=False, method="multi", chunksize=20000)
        total += len(df)
        print(f"   chunk {i + 1}: 累计 {total} 行")
    print(f"   事件总数: {total}")


def load_category(engine) -> None:
    print("[2/5] 加载 category_tree.csv -> category_tree ...")
    df = pd.read_csv(CATEGORY_CSV)
    rows = [{"cid": _to_int(r.categoryid), "pid": _to_int(r.parentid)} for r in df.itertuples()]
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE category_tree"))
        # 关键坑：源数据含 categoryid=0 的根分类。
        # AUTO_INCREMENT 列收到显式 0 时默认会被当成"取下一个自增值"，
        # 导致该行被分配成 1692 等已存在 ID 而撞主键。
        # 开启 NO_AUTO_VALUE_ON_ZERO 后，0 按字面值存储。
        conn.execute(text("SET SESSION sql_mode = CONCAT(@@session.sql_mode, ',NO_AUTO_VALUE_ON_ZERO')"))
        conn.execute(
            text("INSERT INTO category_tree (category_id, parent_id) VALUES (:cid, :pid)"),
            rows,
        )
    print(f"   分类节点数: {len(rows)}")


def build_visitor_first_seen(engine) -> None:
    with engine.connect() as conn:
        exists = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = 'visitor_first_seen'"
        )).scalar()
        if exists:
            n = conn.execute(text("SELECT COUNT(*) FROM visitor_first_seen")).scalar()
            if n:
                print(f"[3/5] visitor_first_seen 已存在（{n} 行），跳过")
                return
    print("[3/5] 计算 visitor_first_seen（新老用户判定依据）...")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS visitor_first_seen"))
        conn.execute(text(
            "CREATE TABLE visitor_first_seen AS "
            "SELECT visitor_id, MIN(event_date) AS first_date FROM raw_events GROUP BY visitor_id"
        ))
        conn.execute(text("ALTER TABLE visitor_first_seen ADD INDEX idx_vfs (visitor_id)"))


def extract_item_properties(engine) -> None:
    """从 item_properties（890MB/2000万行）提取三张表：
    - item_price:        prop=790 纯数值 → 每个商品最新价格（V1 近似）
    - item_category:     categoryid（未哈希）→ 每个商品类目
    - item_availability: available（未哈希）→ 可用性变更日志
    """
    print("[4/5] 从 item_properties 提取 价格/类目/可用性 ...")
    price: dict[int, tuple[int, float]] = {}
    category: dict[int, int] = {}
    avail_rows: list[dict] = []

    for path in ITEM_PROPS_FILES:
        with open(path, newline="", encoding="utf-8") as f:
            r = csv.DictReader(f)
            for row in r:
                item = int(row["itemid"])
                ts = int(row["timestamp"])
                prop, val = row["property"], row["value"]
                if prop == PRICE_PROP:
                    m = _NUM_PAT.match(val)
                    if m:
                        pv = float(m.group(1))
                        cur = price.get(item)
                        if cur is None or ts >= cur[0]:
                            price[item] = (ts, pv)
                elif prop == "categoryid":
                    if item not in category:
                        try:
                            category[item] = int(float(val))
                        except (ValueError, TypeError):
                            category[item] = 0
                elif prop == "available":
                    avail_rows.append({"item_id": item, "ts_ms": ts, "available": 1 if val == "1" else 0})

    with engine.begin() as conn:
        for t in ("item_price", "item_category", "item_availability"):
            conn.execute(text(f"TRUNCATE TABLE {t}"))
        conn.execute(
            text("INSERT INTO item_price (item_id, price, ts_ms) VALUES (:item_id, :price, :ts_ms)"),
            [{"item_id": k, "price": v[1], "ts_ms": v[0]} for k, v in price.items()],
        )
        conn.execute(
            text("INSERT INTO item_category (item_id, category_id) VALUES (:item_id, :category_id)"),
            [{"item_id": k, "category_id": v} for k, v in category.items()],
        )
        # 分批写可用性日志，避免一次 exec 太大
        for i in range(0, len(avail_rows), 50000):
            conn.execute(
                text("INSERT INTO item_availability (item_id, ts_ms, available) VALUES (:item_id, :ts_ms, :available)"),
                avail_rows[i:i + 50000],
            )
    print(f"   价格 {len(price)} 项，类目 {len(category)} 项，可用性变更 {len(avail_rows)} 条")


def aggregate_daily(engine) -> None:
    """聚合 daily_item_stat：all/day_type/new_user/category 四维度，GMV=成交笔数×最新价（V1）。"""

    def _tune(conn) -> None:
        # 临时表放内存，避免 GROUP BY 落到磁盘
        conn.execute(text(
            "SET SESSION tmp_table_size = 1073741824, max_heap_table_size = 1073741824"
        ))

    # GMV = 成交笔数 × 商品最新价格（V1 近似；LEFT JOIN 保证无价格商品不丢）
    _gmv_sql = "SUM(r.event='transaction') * COALESCE(p.price, 0) AS gmv"
    _base_cols = (
        "COUNT(DISTINCT r.visitor_id) AS uv, "
        "SUM(r.event='view') AS view_count, "
        "SUM(r.event='view') AS click_count, "
        "SUM(r.event='addtocart') AS addtocart_count, "
        "SUM(r.event='transaction') AS transaction_count, "
        + _gmv_sql
    )
    _from = "FROM raw_events r LEFT JOIN item_price p ON p.item_id = r.item_id"

    with engine.begin() as conn:
        _tune(conn)
        conn.execute(text("TRUNCATE TABLE daily_item_stat"))
        # 维度: all
        conn.execute(text(f"""
            INSERT INTO daily_item_stat ({AGG_COLS})
            SELECT r.item_id, r.event_date, 'all', 'all', {_base_cols}
            {_from}
            GROUP BY r.item_id, r.event_date
        """))
        print("   all 维度完成")

    with engine.begin() as conn:
        _tune(conn)
        # 维度: day_type (workday/weekend)
        conn.execute(text(f"""
            INSERT INTO daily_item_stat ({AGG_COLS})
            SELECT r.item_id, r.event_date, 'day_type',
                   CASE WHEN DAYOFWEEK(r.event_date) IN (1,7) THEN 'weekend' ELSE 'workday' END,
                   {_base_cols}
            {_from}
            GROUP BY r.item_id, r.event_date,
                     CASE WHEN DAYOFWEEK(r.event_date) IN (1,7) THEN 'weekend' ELSE 'workday' END
        """))
        print("   day_type 维度完成")

    with engine.begin() as conn:
        _tune(conn)
        # 维度: new_user (new/returning)
        conn.execute(text(f"""
            INSERT INTO daily_item_stat ({AGG_COLS})
            SELECT r.item_id, r.event_date, 'new_user',
                   CASE WHEN f.first_date = r.event_date THEN 'new' ELSE 'returning' END,
                   {_base_cols}
            {_from}
            LEFT JOIN visitor_first_seen f ON f.visitor_id = r.visitor_id
            GROUP BY r.item_id, r.event_date,
                     CASE WHEN f.first_date = r.event_date THEN 'new' ELSE 'returning' END
        """))
        print("   new_user 维度完成")

    with engine.begin() as conn:
        _tune(conn)
        # 维度: category（商品类目，可回答"异常是否类目级"）
        conn.execute(text(f"""
            INSERT INTO daily_item_stat ({AGG_COLS})
            SELECT r.item_id, r.event_date, 'category',
                   COALESCE(CAST(ic.category_id AS CHAR), 'unknown'),
                   {_base_cols}
            {_from}
            LEFT JOIN item_category ic ON ic.item_id = r.item_id
            GROUP BY r.item_id, r.event_date, COALESCE(CAST(ic.category_id AS CHAR), 'unknown')
        """))
        print("   category 维度完成")


def main() -> None:
    init_db()
    engine = get_write_engine()
    with engine.begin() as conn:
        cnt = conn.execute(text("SELECT COUNT(*) FROM raw_events")).scalar()
    if cnt:
        print(f"raw_events 已有 {cnt} 行，跳过事件加载（断点续传）")
    else:
        load_events(engine)
    load_category(engine)
    build_visitor_first_seen(engine)
    extract_item_properties(engine)
    aggregate_daily(engine)

    with engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) AS c FROM daily_item_stat")).scalar()
        row = conn.execute(text(
            "SELECT MIN(stat_date) mn, MAX(stat_date) mx, COUNT(DISTINCT item_id) items "
            "FROM daily_item_stat WHERE dimension_type='all'"
        )).one()
    print(f"完成：daily_item_stat 共 {n} 行，{row.items} 个商品，日期范围 {row.mn} ~ {row.mx}")


if __name__ == "__main__":
    main()
