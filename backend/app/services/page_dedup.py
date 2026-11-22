"""页面去重领域服务：采集页内容等价判定与页面重复组管理。

内容等价口径：字段（键）位置不影响判定；类型差异、嵌套列表顺序、
缺字段与字段明确留空的差异仍然计入判定。

每个重复组只有一个正式代表页（按复采定位完整度选举，先到先得），
代表页之外的捕获来源仍按原地址留在组内取证。运行历史确认时按重复组
出实际去重计数并出具运行回执；代表页的采集定位同步到复采计划，供
error_recover 发起重试时逐组触发复采。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app.store import store
from app.services.base import BaseModuleService
from app import persistence as persist


def canonicalize_content(value: Any) -> Any:
    """递归规范化内容：字典按键名排序（字段位置无关），列表保持原有顺序。"""
    if isinstance(value, dict):
        return {
            str(key): canonicalize_content(val)
            for key, val in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [canonicalize_content(item) for item in value]
    return value


def canonical_content_text(content: Any) -> str:
    """内容的规范文本：键序无关；类型、列表顺序、缺字段与空值的差异都体现在文本里。"""
    return json.dumps(
        canonicalize_content(content),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def registry_fingerprint(content: Any) -> str:
    """重复组登记用的内容指纹（规范文本取 sha256）。"""
    return hashlib.sha256(canonical_content_text(content).encode("utf-8")).hexdigest()[:16]


def locator_score(locator: Any) -> int:
    """复采定位完整度：递归统计非空定位字段个数。"""
    if isinstance(locator, dict):
        return sum(locator_score(value) for value in locator.values())
    if isinstance(locator, (list, tuple)):
        return sum(locator_score(item) for item in locator)
    return 1 if locator not in (None, "") else 0


class PageDedupService(BaseModuleService):
    MODULE = "page_dedup"
    ENTITY = "去重规则"
    OPERATION_TERM = "去重"
    LIST_FIELDS = ["规则编号", "去重维度", "特征字段", "相似算法", "指纹长度", "时间窗口", "冲突策略", "规则版本"]
    REQUIRED_FIELDS = ["规则编号", "去重维度", "特征字段"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    # 采集页登记册 / 页面重复组 / 复采计划为类级状态：
    # 服务可被多次实例化（如跨模块钩子内），口径与数据保持一致。
    _captures: list[dict[str, Any]] = []
    _groups: list[dict[str, Any]] = []
    _plans: list[dict[str, Any]] = []
    _loaded: bool = False

    # ------------------------------------------------------------------
    # 内容等价判定
    # ------------------------------------------------------------------
    def compute_fingerprint(self, content: str, rule_id: int) -> str:
        """根据去重规则计算内容串的指纹（原始串口径，保留给外部调用）。"""
        rule = store.find(self.MODULE, rule_id)
        algorithm = rule.get("相似算法", "md5") if rule else "md5"
        if algorithm == "sha256":
            return hashlib.sha256(content.encode()).hexdigest()[:32]
        return hashlib.md5(content.encode()).hexdigest()[:16]

    def content_fingerprint(self, content: Any, rule_id: int | None = None) -> str:
        """内容等价指纹：先规范化（键序无关）再按规则算法取指纹。"""
        text = canonical_content_text(content)
        algorithm = "md5"
        if rule_id is not None:
            rule = store.find(self.MODULE, rule_id)
            algorithm = (rule or {}).get("相似算法", "md5") or "md5"
        if algorithm == "sha256":
            return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
        return hashlib.md5(text.encode("utf-8")).hexdigest()[:16]

    def content_equivalent(self, left: Any, right: Any) -> bool:
        """判定两份内容是否等价：字段位置无关；类型、嵌套列表顺序、
        缺字段与空值的差异仍会使两边不等价。"""
        return canonical_content_text(left) == canonical_content_text(right)

    def deduplicate_pages(self, pages: list[dict[str, Any]], rule_id: int) -> dict[str, Any]:
        """对页面列表执行去重：仅字段换序的页面判为同一唯一页。"""
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        duplicates: list[dict[str, Any]] = []
        for page in pages:
            content = page.get("content") if isinstance(page, dict) and "content" in page else page
            fp = self.content_fingerprint(content, rule_id)
            if fp in seen:
                duplicates.append({**page, "fingerprint": fp})
            else:
                seen.add(fp)
                unique.append({**page, "fingerprint": fp})
        result = {
            "rule_id": rule_id,
            "total": len(pages),
            "unique": len(unique),
            "duplicates": len(duplicates),
            "timestamp": persist.now_iso(),
        }
        persist.append_jsonl("reports", "dedup_report.jsonl", result)
        return result

    # ------------------------------------------------------------------
    # 采集页登记与页面重复组
    # ------------------------------------------------------------------
    @classmethod
    def reset_registry(cls) -> None:
        """清空登记册/重复组/复采计划（测试与重灌场景使用）。"""
        cls._captures = []
        cls._groups = []
        cls._plans = []
        cls._loaded = True

    @classmethod
    def _ensure_loaded(cls) -> None:
        """首次访问时从持久化文件重建登记册与重复组。"""
        if cls._loaded:
            return
        cls._loaded = True
        for record in persist.read_jsonl("reports", "page_captures.jsonl"):
            cls._add_capture(record)
        saved = persist.read_json("reports", "recrawl_plans.json") or {}
        cls._plans = list(saved.get("plans", []))
        cls._sync_plans()

    @classmethod
    def _find_group(cls, fingerprint: str) -> dict[str, Any] | None:
        return next((g for g in cls._groups if g["fingerprint"] == fingerprint), None)

    @classmethod
    def _representative_capture(cls, group: dict[str, Any]) -> dict[str, Any] | None:
        rep_id = group.get("representative_id")
        return next((c for c in cls._captures if c.get("capture_id") == rep_id), None)

    @classmethod
    def _elect_representative(cls, group: dict[str, Any]) -> None:
        """选举正式代表：复采定位更全者优先；完整度相同时先到的保留。"""
        best: dict[str, Any] | None = None
        for member in group["members"]:
            if best is None or member["locator_score"] > best["locator_score"]:
                best = member
        group["representative_id"] = best["capture_id"] if best else None

    @classmethod
    def _add_capture(cls, page: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        capture = dict(page)
        if not capture.get("capture_id"):
            capture["capture_id"] = f"CAP-{len(cls._captures) + 1:04d}"
        capture.setdefault("captured_at", persist.now_iso())
        fingerprint = registry_fingerprint(capture.get("content", {}))
        capture["fingerprint"] = fingerprint
        cls._captures.append(capture)

        group = cls._find_group(fingerprint)
        if group is None:
            group = {
                "group_id": f"DG-{len(cls._groups) + 1:04d}",
                "fingerprint": fingerprint,
                "representative_id": None,
                "members": [],
                "created_at": persist.now_iso(),
            }
            cls._groups.append(group)
        group["members"].append({
            "capture_id": capture["capture_id"],
            "source_entry": capture.get("source_entry", ""),
            "url": capture.get("url", ""),
            "evidence_url": capture.get("url", ""),  # 原地址取证，跟组保留
            "captured_at": capture.get("captured_at", ""),
            "locator_score": locator_score(capture.get("recrawl_locator")),
        })
        group["updated_at"] = persist.now_iso()
        cls._elect_representative(group)
        return capture, group

    @classmethod
    def _sync_plans(cls) -> None:
        """把各组代表页的采集定位同步到复采计划；代表更换后计划重新待复采。"""
        for group in cls._groups:
            rep = cls._representative_capture(group) or {}
            locator = rep.get("recrawl_locator") or {}
            plan = next((p for p in cls._plans if p.get("group_id") == group["group_id"]), None)
            if plan is None:
                plan = {
                    "plan_id": f"RC-{len(cls._plans) + 1:04d}",
                    "group_id": group["group_id"],
                    "status": "待复采",
                    "created_at": persist.now_iso(),
                }
                cls._plans.append(plan)
            if plan.get("representative_id") != group.get("representative_id"):
                plan["representative_id"] = group.get("representative_id")
                plan["locator"] = locator
                plan["status"] = "待复采"
            plan["fingerprint"] = group["fingerprint"]
            plan["updated_at"] = persist.now_iso()
        cls._save()

    @classmethod
    def _save(cls) -> None:
        persist.write_json("reports", "page_dedup_groups.json", {
            "updated_at": persist.now_iso(),
            "groups": cls._groups,
        })
        persist.write_json("reports", "recrawl_plans.json", {
            "updated_at": persist.now_iso(),
            "plans": cls._plans,
        })

    def register_capture(self, page: dict[str, Any]) -> dict[str, Any]:
        """登记一份采集页：按内容等价口径归入页面重复组，并选举代表页。"""
        self._ensure_loaded()
        capture, group = self._add_capture(page)
        persist.append_jsonl("reports", "page_captures.jsonl", capture)
        self._sync_plans()
        return {
            "capture": capture,
            "group": group,
            "representative_id": group.get("representative_id"),
            "counts": self.dedup_counts(),
        }

    def dedup_counts(self) -> dict[str, int]:
        """实际去重计数：唯一页=重复组数，重复=采集页数-组数。"""
        self._ensure_loaded()
        total = len(self._captures)
        groups = len(self._groups)
        return {"total_captures": total, "unique_groups": groups, "duplicates": total - groups}

    def duplicate_groups(self) -> list[dict[str, Any]]:
        """当前全部页面重复组（每组一个正式代表，成员含原地址取证）。"""
        self._ensure_loaded()
        return self._groups

    def recrawl_plans(self) -> list[dict[str, Any]]:
        """当前复采计划：每组一个，定位取代表页采集定位。"""
        self._ensure_loaded()
        self._sync_plans()
        return self._plans

    def sync_recrawl_plans(self) -> list[dict[str, Any]]:
        """强制同步复采计划（代表页定位变更后调用）。"""
        self._ensure_loaded()
        self._sync_plans()
        return self._plans

    # ------------------------------------------------------------------
    # 运行回执与逐组复采
    # ------------------------------------------------------------------
    def issue_receipt(self, run_id: Any = None, run_code: str = "") -> dict[str, Any]:
        """出具运行回执：记录确认时刻的实际去重计数与各组代表页，供事后核对。"""
        self._ensure_loaded()
        self._sync_plans()
        counts = self.dedup_counts()
        existing = persist.read_jsonl("reports", "run_receipts.jsonl")
        receipt = {
            "receipt_id": f"RCPT-{len(existing) + 1:04d}",
            "run_id": run_id,
            "run_code": run_code,
            "confirmed_at": persist.now_iso(),
            **counts,
            "representatives": {g["group_id"]: g.get("representative_id") for g in self._groups},
            "representative_locators": {
                g["group_id"]: (self._representative_capture(g) or {}).get("recrawl_locator") or {}
                for g in self._groups
            },
        }
        persist.append_jsonl("reports", "run_receipts.jsonl", receipt)
        return receipt

    def verify_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        """核对已出具的运行回执：当时使用的代表页仍在组内、可按原地址取证。"""
        self._ensure_loaded()
        receipts = persist.read_jsonl("reports", "run_receipts.jsonl")
        receipt = next((r for r in receipts if r.get("receipt_id") == receipt_id), None)
        if receipt is None:
            return None
        checks: list[dict[str, Any]] = []
        for group_id, capture_id in (receipt.get("representatives") or {}).items():
            group = next((g for g in self._groups if g["group_id"] == group_id), None)
            members = (group or {}).get("members", [])
            member = next((m for m in members if m.get("capture_id") == capture_id), None)
            checks.append({
                "group_id": group_id,
                "receipt_representative": capture_id,
                "still_member": member is not None,
                "evidence_url": (member or {}).get("evidence_url", ""),
                "current_representative": (group or {}).get("representative_id"),
                "representative_changed": (group or {}).get("representative_id") != capture_id,
            })
        return {
            "receipt_id": receipt_id,
            "run_id": receipt.get("run_id"),
            "ok": all(c["still_member"] for c in checks),
            "checks": checks,
        }

    def trigger_recrawl(
        self,
        group_ids: list[str] | None = None,
        *,
        trigger: str = "manual",
        recovery_task: str = "",
    ) -> list[dict[str, Any]]:
        """按页面重复组逐组触发复采：复采定位取代表页当前的采集定位。"""
        self._ensure_loaded()
        self._sync_plans()
        existing = persist.read_jsonl("runs", "recrawl_tasks.jsonl")
        tasks: list[dict[str, Any]] = []
        for plan in self._plans:
            if group_ids and plan.get("group_id") not in group_ids:
                continue
            tasks.append({
                "task_id": f"RT-{len(existing) + len(tasks) + 1:04d}",
                "plan_id": plan.get("plan_id"),
                "group_id": plan.get("group_id"),
                "representative_id": plan.get("representative_id"),
                "locator": plan.get("locator") or {},
                "trigger": trigger,
                "recovery_task": recovery_task,
                "status": "已触发",
                "timestamp": persist.now_iso(),
            })
        for task in tasks:
            persist.append_jsonl("runs", "recrawl_tasks.jsonl", task)
        if tasks:
            touched = {t["plan_id"] for t in tasks}
            for plan in self._plans:
                if plan.get("plan_id") in touched:
                    plan["status"] = "复采中"
                    plan["updated_at"] = persist.now_iso()
            self._save()
        return tasks

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        # 去重报告消费
        reports = persist.read_jsonl("reports", "dedup_report.jsonl")
        base["dedup_reports"] = len(reports)
        # 关联的导出配置
        channels = store.rows("export_channel")
        base["export_channels"] = len(channels)
        # 页面重复组与复采计划
        self._ensure_loaded()
        base["captures"] = len(self._captures)
        base["duplicate_groups"] = len(self._groups)
        base["recrawl_plans"] = len(self._plans)
        base["run_receipts"] = len(persist.read_jsonl("reports", "run_receipts.jsonl"))
        return base
