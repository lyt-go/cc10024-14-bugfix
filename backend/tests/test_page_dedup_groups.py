"""页面重复组端到端测试：字段换序等价、代表页选举、运行回执与逐组复采。

场景：同一商品页先由甲入口抓取，乙入口晚抓、正文相同（字段换序）但复采定位更全；
第三次抓取改了价格；第四次缺字段，需与字段明确留空的页面区分。确认运行出具回执，
从错误恢复的发起重试入口逐组触发复采；再补一份定位更全的同内容记录后，
新复采目标指向新代表，旧回执仍能核对当时使用的页面。
"""
from __future__ import annotations

import os
import sys

# 确保后端目录在导入路径中
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app import persistence as persist

# 清理本场景涉及的持久化文件，保证测试可重复执行
for sub, name in [
    ("reports", "page_captures.jsonl"),
    ("reports", "page_dedup_groups.json"),
    ("reports", "recrawl_plans.json"),
    ("reports", "run_receipts.jsonl"),
    ("reports", "dedup_report.jsonl"),
    ("runs", "recrawl_tasks.jsonl"),
]:
    path = persist.data_path(sub, name)
    if os.path.exists(path):
        os.remove(path)

from app.store import store
from app.services.page_dedup import PageDedupService

PageDedupService.reset_registry()

# ---------------------------------------------------------------------------
# 场景数据：同一商品页的多次采集
# ---------------------------------------------------------------------------
CONTENT = {
    "title": "机械键盘K870",
    "price": 129.0,
    "tags": ["新品", "包邮"],
    "detail": {"color": "红", "size": ["S", "M"], "stock": {"warehouse": 3, "locked": 0}},
}
# 乙入口：正文相同，仅字段排列不同
CONTENT_REORDERED = {
    "detail": {"stock": {"locked": 0, "warehouse": 3}, "size": ["S", "M"], "color": "红"},
    "tags": ["新品", "包邮"],
    "price": 129.0,
    "title": "机械键盘K870",
}
# 己入口：正文相同，字段再换一种排列
CONTENT_REORDERED_AGAIN = {
    "price": 129.0,
    "title": "机械键盘K870",
    "detail": {"color": "红", "size": ["S", "M"], "stock": {"warehouse": 3, "locked": 0}},
    "tags": ["新品", "包邮"],
}
CONTENT_PRICE_CHANGED = {**CONTENT, "price": 139.0}                       # 第三次：改了价格
CONTENT_MISSING_FIELD = {k: v for k, v in CONTENT.items() if k != "tags"}  # 第四次：缺字段
CONTENT_EMPTY_FIELD = {**CONTENT, "tags": None}                            # 对照：字段明确留空

LOCATOR_JIA = {"url": "https://shop.example.com/item/1001"}
LOCATOR_YI = {
    "url": "https://shop.example.com/item/1001",
    "selector": "#item-1001 .price",
    "xpath": "//div[@id='item-1001']//span[@class='price']",
}
LOCATOR_JI = {
    "url": "https://shop.example.com/item/1001",
    "selector": "#item-1001 .price",
    "xpath": "//div[@id='item-1001']//span[@class='price']",
    "snapshot": "PAGE-9001",
    "account": "crawler-acc-07",
}


def test_content_equivalence_rules():
    """字段位置不影响判定；类型、嵌套列表顺序、缺字段与空值差异仍计入。"""
    svc = PageDedupService()
    # 字段换序（含嵌套字典换序）→ 等价
    assert svc.content_equivalent(CONTENT, CONTENT_REORDERED)
    assert svc.content_equivalent(CONTENT, CONTENT_REORDERED_AGAIN)
    # 类型差异 → 不等价
    assert not svc.content_equivalent({"price": 129.0}, {"price": "129.0"})
    assert not svc.content_equivalent({"n": 1}, {"n": True})
    # 嵌套列表顺序 → 不等价
    assert not svc.content_equivalent({"size": ["S", "M"]}, {"size": ["M", "S"]})
    # 缺字段 vs 字段明确留空 → 不等价
    assert not svc.content_equivalent(CONTENT_MISSING_FIELD, CONTENT_EMPTY_FIELD)
    assert not svc.content_equivalent({"a": 1}, {"a": 1, "b": None})
    assert not svc.content_equivalent({"a": 1}, {"a": 1, "b": ""})


def test_deduplicate_pages_field_order():
    """仅字段换序的页面不再被算成两个唯一页，重采候选不多出重复内容。"""
    svc = PageDedupService()
    rule_id = store.rows("page_dedup")[0]["id"]
    pages = [
        {"url": "https://a.com/p1", "content": {"a": 1, "b": 2}},
        {"url": "https://a.com/p1", "content": {"b": 2, "a": 1}},        # 仅换序 → 重复
        {"url": "https://a.com/p2", "content": {"a": 1, "b": 2, "c": None}},  # 留空 → 唯一
        {"url": "https://a.com/p3", "content": {"a": 1, "b": 2}},        # 同内容 → 重复
    ]
    result = svc.deduplicate_pages(pages, rule_id)
    assert result["total"] == 4
    assert result["unique"] == 2, f"字段换序被误算成多个唯一页: {result}"
    assert result["duplicates"] == 2


def _register_scenario_captures(svc: PageDedupService) -> dict[str, str]:
    """登记甲/乙/丙/丁/戊五次采集，返回各捕获ID。"""
    r_jia = svc.register_capture({
        "source_entry": "甲入口",
        "url": "https://shop.example.com/item/1001?from=entry_a",
        "content": CONTENT,
        "recrawl_locator": LOCATOR_JIA,
        "captured_at": "2026-11-20T09:00:00",
    })
    r_yi = svc.register_capture({
        "source_entry": "乙入口",
        "url": "https://shop.example.com/item/1001?from=entry_b",
        "content": CONTENT_REORDERED,
        "recrawl_locator": LOCATOR_YI,
        "captured_at": "2026-11-20T10:00:00",
    })
    r_bing = svc.register_capture({
        "source_entry": "甲入口",
        "url": "https://shop.example.com/item/1001?from=entry_a&t=3",
        "content": CONTENT_PRICE_CHANGED,
        "recrawl_locator": LOCATOR_JIA,
        "captured_at": "2026-11-20T11:00:00",
    })
    r_ding = svc.register_capture({
        "source_entry": "甲入口",
        "url": "https://shop.example.com/item/1001?from=entry_a&t=4",
        "content": CONTENT_MISSING_FIELD,
        "recrawl_locator": LOCATOR_JIA,
        "captured_at": "2026-11-20T12:00:00",
    })
    r_wu = svc.register_capture({
        "source_entry": "乙入口",
        "url": "https://shop.example.com/item/1001?from=entry_b&t=5",
        "content": CONTENT_EMPTY_FIELD,
        "recrawl_locator": LOCATOR_JIA,
        "captured_at": "2026-11-20T13:00:00",
    })
    return {
        "jia": (r_jia["capture"]["capture_id"], r_jia["group"]["group_id"]),
        "yi": (r_yi["capture"]["capture_id"], r_yi["group"]["group_id"]),
        "bing": (r_bing["capture"]["capture_id"], r_bing["group"]["group_id"]),
        "ding": (r_ding["capture"]["capture_id"], r_ding["group"]["group_id"]),
        "wu": (r_wu["capture"]["capture_id"], r_wu["group"]["group_id"]),
    }


def test_duplicate_group_run_receipt_recrawl_flow():
    """完整链路：归组→代表选举→运行确认出回执→发起重试逐组复采→换代表后新旧口径一致。"""
    dedup = PageDedupService()
    ids = _register_scenario_captures(dedup)
    cap_jia, gid_main = ids["jia"]
    cap_yi, gid_yi = ids["yi"]
    _, gid_bing = ids["bing"]
    _, gid_ding = ids["ding"]
    _, gid_wu = ids["wu"]

    # 字段换序的甲乙同组；改价/缺字段/留空各自独立成组
    assert gid_main == gid_yi, "字段换序的两次抓取应归同一重复组"
    assert len({gid_main, gid_bing, gid_ding, gid_wu}) == 4
    counts = dedup.dedup_counts()
    assert counts == {"total_captures": 5, "unique_groups": 4, "duplicates": 1}, counts

    # 每组只有一个正式代表：乙入口定位更全，当选主组代表
    group_main = next(g for g in dedup.duplicate_groups() if g["group_id"] == gid_main)
    assert group_main["representative_id"] == cap_yi
    assert sum(1 for g in dedup.duplicate_groups() if g.get("representative_id")) == 4
    # 代表页之外的捕获来源仍按原地址取证
    members = {m["capture_id"]: m for m in group_main["members"]}
    assert members[cap_jia]["evidence_url"] == "https://shop.example.com/item/1001?from=entry_a"
    assert members[cap_jia]["source_entry"] == "甲入口"

    # 运行历史确认：按页面重复组出实际去重计数，出具运行回执
    from app.services.run_history import RunHistoryService
    run_svc = RunHistoryService()
    run_entry, missing = run_svc.create_entry({
        "记录编号": "RUN-DEDUP-0001",
        "关联计划": "SCRI-DEDUP",
        "开始时间": "2026-11-20T14:00:00",
    })
    assert run_entry is not None, f"运行记录创建失败: {missing}"
    result, msg = run_svc.run_action(run_entry["id"], "确认完成")
    assert result is not None and result["status"] == "已完成", msg

    receipts = persist.read_jsonl("reports", "run_receipts.jsonl")
    assert len(receipts) == 1
    receipt = receipts[-1]
    assert receipt["total_captures"] == 5
    assert receipt["unique_groups"] == 4
    assert receipt["duplicates"] == 1
    assert receipt["representatives"][gid_main] == cap_yi
    assert receipt["representative_locators"][gid_main]["selector"] == "#item-1001 .price"

    # 去重报告中的计数与重复组口径一致
    dedup_reports = persist.read_jsonl("reports", "dedup_report.jsonl")
    confirm_reports = [r for r in dedup_reports if r.get("trigger") == "run_history_confirm"]
    assert confirm_reports, "运行确认未产生去重报告"
    assert confirm_reports[-1]["unique_groups"] == 4
    assert confirm_reports[-1]["duplicates"] == 1

    # 代表页的采集定位已关联到复采计划
    plans = (persist.read_json("reports", "recrawl_plans.json") or {}).get("plans", [])
    assert len(plans) == 4
    plan_main = next(p for p in plans if p["group_id"] == gid_main)
    assert plan_main["representative_id"] == cap_yi
    assert plan_main["locator"]["xpath"] == "//div[@id='item-1001']//span[@class='price']"

    # 错误恢复的发起重试入口：逐组触发复采
    from app.services.error_recover import ErrorRecoverService
    rec_svc = ErrorRecoverService()
    task_entry, missing = rec_svc.create_entry({
        "任务编号": "ERRO-DEDUP-0001",
        "关联计划": "SCRI-DEDUP",
        "失败步骤": "page_fetch",
    })
    assert task_entry is not None, f"恢复任务创建失败: {missing}"
    rec_result, msg = rec_svc.run_action(task_entry["id"], "发起重试")
    assert rec_result is not None and rec_result["status"] == "重试中", msg

    tasks = persist.read_jsonl("runs", "recrawl_tasks.jsonl")
    assert len(tasks) == 4, f"应逐组触发4条复采任务: {len(tasks)}"
    assert {t["group_id"] for t in tasks} == {gid_main, gid_bing, gid_ding, gid_wu}
    task_main = next(t for t in tasks if t["group_id"] == gid_main)
    assert task_main["representative_id"] == cap_yi
    assert task_main["locator"]["selector"] == "#item-1001 .price"
    assert task_main["recovery_task"] == "ERRO-DEDUP-0001"

    # 补一份定位更全的同内容记录（字段再次换序）→ 代表更换
    r_ji = dedup.register_capture({
        "source_entry": "丙入口",
        "url": "https://shop.example.com/item/1001?from=entry_c",
        "content": CONTENT_REORDERED_AGAIN,
        "recrawl_locator": LOCATOR_JI,
        "captured_at": "2026-11-20T15:00:00",
    })
    cap_ji = r_ji["capture"]["capture_id"]
    assert r_ji["group"]["group_id"] == gid_main, "同内容记录应仍归主组"
    assert r_ji["group"]["representative_id"] == cap_ji, "定位更全的新记录应当选代表"
    counts = dedup.dedup_counts()
    assert counts == {"total_captures": 6, "unique_groups": 4, "duplicates": 2}, counts

    # 口径一致时新的复采目标指向新代表，计划重新待复采
    plans = (persist.read_json("reports", "recrawl_plans.json") or {}).get("plans", [])
    plan_main = next(p for p in plans if p["group_id"] == gid_main)
    assert plan_main["representative_id"] == cap_ji
    assert plan_main["locator"]["snapshot"] == "PAGE-9001"
    assert plan_main["status"] == "待复采"

    # 旧回执仍能核对当时使用的页面（乙），并指出代表已更换
    verify = dedup.verify_receipt(receipt["receipt_id"])
    assert verify is not None and verify["ok"], verify
    check_main = next(c for c in verify["checks"] if c["group_id"] == gid_main)
    assert check_main["receipt_representative"] == cap_yi
    assert check_main["still_member"] is True
    assert check_main["evidence_url"] == "https://shop.example.com/item/1001?from=entry_b"
    assert check_main["representative_changed"] is True
    assert check_main["current_representative"] == cap_ji
    # 旧回执仍指向当时的来源定位
    assert receipt["representative_locators"][gid_main].get("snapshot") is None

    # 再次发起重试：复采任务改用新代表的定位
    rec_svc.run_action(task_entry["id"], "发起重试")
    tasks = persist.read_jsonl("runs", "recrawl_tasks.jsonl")
    assert len(tasks) == 8
    latest_main = [t for t in tasks if t["group_id"] == gid_main][-1]
    assert latest_main["representative_id"] == cap_ji
    assert latest_main["locator"]["snapshot"] == "PAGE-9001"
    assert latest_main["locator"]["account"] == "crawler-acc-07"


def test_registry_reloads_from_persistence():
    """登记册可从持久化文件重建，组号与代表选举结果保持一致。"""
    captures = persist.read_jsonl("reports", "page_captures.jsonl")
    assert len(captures) == 6, f"采集页应持久化6份: {len(captures)}"

    class FreshService(PageDedupService):
        _captures = []
        _groups = []
        _plans = []
        _loaded = False

    svc = FreshService()
    counts = svc.dedup_counts()
    assert counts == {"total_captures": 6, "unique_groups": 4, "duplicates": 2}, counts
    groups = svc.duplicate_groups()
    main = next(g for g in groups if any(m["source_entry"] == "丙入口" for m in g["members"]))
    rep = next(c for c in captures if c["capture_id"] == main["representative_id"])
    assert rep["recrawl_locator"].get("snapshot") == "PAGE-9001"


if __name__ == "__main__":
    test_content_equivalence_rules()
    print("PASS: test_content_equivalence_rules")
    test_deduplicate_pages_field_order()
    print("PASS: test_deduplicate_pages_field_order")
    test_duplicate_group_run_receipt_recrawl_flow()
    print("PASS: test_duplicate_group_run_receipt_recrawl_flow")
    test_registry_reloads_from_persistence()
    print("PASS: test_registry_reloads_from_persistence")
    print("\nALL 4 TESTS PASSED")
