"""日期参数回归：检查边界、查询结果与参数绑定，不依赖 sqlite3 默认适配器。"""
from datetime import date

import pytest

from app.db import write_session
from app.detection import detector
from app.metrics import compute
from app.models import DailyItemStat


@pytest.fixture
def calendar_item():
    item_id, category_id = 900017, 900018
    days = [date(2024, 2, 28), date(2024, 2, 29), date(2024, 3, 1), date(2024, 3, 2)]
    with write_session() as session:
        for number, day in enumerate(days, 1):
            for dimension_type, dimension in [("all", "all"), ("category", str(category_id))]:
                session.add(DailyItemStat(
                    item_id=item_id, stat_date=day, uv=number * 10,
                    dimension_type=dimension_type, dimension=dimension,
                ))
        session.commit()
    try:
        yield item_id, category_id
    finally:
        with write_session() as session:
            session.query(DailyItemStat).filter_by(item_id=item_id).delete()
            session.commit()


@pytest.mark.filterwarnings("error::DeprecationWarning")
@pytest.mark.parametrize("start,end,expected", [
    (date(2024, 2, 28), date(2024, 3, 2), [10, 20, 30, 40]),
    (date(2024, 2, 29), date(2024, 3, 1), [20, 30]),
    (date(2024, 2, 29), date(2024, 2, 29), [20]),
    (date(2024, 3, 3), date(2024, 3, 4), []),
])
def test_date_boundaries_agree_across_query_paths(calendar_item, start, end, expected):
    item_id, category_id = calendar_item
    daily = compute.daily_series(item_id, start, end, ["uv"])
    assert [row["uv"] for row in daily] == expected
    assert all(start <= date.fromisoformat(row["date"]) <= end for row in daily)
    assert [value for _, value in detector._fetch_series(item_id, "uv", start, end)] == expected
    items = detector._load_all_series(start, end)
    categories = detector._load_all_category_series(start, end)
    assert [row["uv"] for _, row in items.get(item_id, [])] == expected
    assert [row["uv"] for _, row in categories.get(category_id, [])] == expected
    assert detector.find_anchor_item(category_id, start, end) == (item_id if expected else None)
    assert compute.item_summary(item_id, start, end, ["uv"])["current"]["uv"] == sum(expected)


def test_raw_query_values_remain_bound_parameters():
    payload = "' OR 1=1; DROP TABLE daily_item_stat; --"
    assert compute._rows("SELECT :value AS value", {"value": payload}) == [{"value": payload}]


def test_daily_series_and_summary_share_daily_aggregation(calendar_item):
    item_id, _ = calendar_item
    with write_session() as session:
        session.add(DailyItemStat(item_id=item_id, stat_date=date(2024, 3, 2), uv=5,
                                  dimension_type="all", dimension="all"))
        session.commit()
    # 同日两行只算一个观测日；求和保持可加口径，并不替代导入幂等或访客去重。
    comparison = compute.item_comparison(item_id, date(2024, 3, 2), date(2024, 3, 2), ["uv"])
    assert comparison["series"] == [{"date": "2024-03-02", "uv": 45}]
    assert compute.daily_series(item_id, date(2024, 3, 2), date(2024, 3, 2), ["uv"]) == comparison["series"]
    assert comparison["summary"]["current"]["uv"] == 45
    assert comparison["summary"]["coverage"]["current"]["observed_days"] == 1
