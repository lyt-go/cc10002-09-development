"""治理计划业务规则：先演练后执行、断点续做、归档证据校验与成本联动都收在这里。

整体约定：
- 治理计划由「已启用」的分区方案生成；同一张表有多个启用方案或方案与生命周期
  策略冲突时，按生效日期更晚且已启用的分区方案为准，冲突原因保留在计划上。
- 演练只计算将新建、将归档（迁移）、将删除与仍保留的范围，不改动任何数据。
- 正式执行逐个分区记录结果；同一计划同时只允许一个作业生效；中断后可续做，
  已经成功的分区不重复迁移或删除。
- 归档完成证据写入台账并落盘持久化；删除前必须能查到该分区的已持久化证据，
  任何证据缺失都不得进入删除。
- 每个执行结论同步写变更审计，并保留演练范围与实际执行结果的差异；归档、删除
  成功的分区会按估算口径联动成本账单。
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.store import store

MODULE = "governance"
RUN_MODULE = "_governance_run"      # 治理作业（内部表，不进运营概览）
LEDGER_MODULE = "_governance_ledger"  # 归档证据与分区状态台账（内部表，落盘持久化）

REQUIRED_FIELDS = ["计划编号", "数据表名", "分区粒度"]
PLAN_STATUSES = ["草稿", "已演练", "执行中", "已中断", "已完成", "部分失败"]
SUPPORTED_GRANULARITY = {"按天"}

DEFAULT_OVERDUE_PARTITIONS = 7  # 演示环境模拟「已超期但仍残留在库」的分区数
PARTITION_GB = 10  # 演示环境单分区存储估算口径（GB）

LEDGER_PATH = Path(
    os.environ.get(
        "GOVERNANCE_LEDGER_PATH",
        Path(__file__).resolve().parents[2] / "data" / "governance_ledger.json",
    )
)

_AUDIT_PREFIX = "AUDI"
_RUN_PREFIX = "RUN"
_EVIDENCE_PREFIX = "EV"
_PLAN_PREFIX = "PLAN"


def _to_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _next_code(rows: list[dict[str, Any]], field: str, prefix: str) -> str:
    """按前缀扫描已有编号，生成下一个连续编号。"""
    highest = 0
    for row in rows:
        match = re.fullmatch(rf"{prefix}-(\d+)", str(row.get(field) or ""))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{prefix}-{highest + 1:04d}"


def _partition_label(day: date) -> str:
    return f"dt={day.isoformat()}"


def _partition_day(label: str) -> date | None:
    text = str(label or "")
    if text.startswith("dt="):
        try:
            return date.fromisoformat(text[3:])
        except ValueError:
            return None
    return None


def _load_persisted_ledger() -> None:
    """启动时把落盘的归档证据台账读回内存，与种子数据按证据编号去重。"""
    rows = store.rows(LEDGER_MODULE)
    if not LEDGER_PATH.exists():
        return
    try:
        persisted = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    known = {str(row.get("证据编号")) for row in rows}
    for row in persisted:
        if isinstance(row, dict) and str(row.get("证据编号")) not in known:
            rows.append(row)
            known.add(str(row.get("证据编号")))


_load_persisted_ledger()


class GovernanceService:
    def __init__(self) -> None:
        # 执行/续做的互斥锁：配合「执行中」状态保证同一计划只有一个作业生效
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 计划查询
    # ------------------------------------------------------------------
    def list_plans(
        self,
        *,
        keyword: str | None = None,
        table: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("计划编号", ""))]
        if table:
            rows = [row for row in rows if table in str(row.get("数据表名", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_plan(self, plan_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, plan_id)

    def plan_detail(self, plan_id: int) -> dict[str, Any] | None:
        plan = store.find(MODULE, plan_id)
        if plan is None:
            return None
        runs = self._runs_of(plan)
        latest_dryrun = self._find_run(str(plan.get("最近演练编号") or ""))
        execute_runs = [run for run in runs if run.get("作业类型") == "正式执行"]
        latest_execute = execute_runs[-1] if execute_runs else None
        return {
            "plan": plan,
            "最近演练": latest_dryrun,
            "最近作业": latest_execute,
            "作业列表": runs,
            "台账统计": self._ledger_stats(str(plan.get("数据表名") or "")),
            "成本影响": self._impact_for_table(str(plan.get("数据表名") or "")),
        }

    # ------------------------------------------------------------------
    # 计划生成：分区方案 + 生命周期策略冲突裁决
    # ------------------------------------------------------------------
    def generate_plans(self, table: str | None = None) -> dict[str, Any]:
        schemes = [row for row in store.rows("partition") if row.get("status") == "已启用"]
        if table:
            schemes = [row for row in schemes if str(row.get("数据表名")) == table]
        by_table: dict[str, list[dict[str, Any]]] = {}
        for scheme in schemes:
            by_table.setdefault(str(scheme.get("数据表名") or "").strip(), []).append(scheme)

        generated: list[dict[str, Any]] = []
        conflicts: list[dict[str, str]] = []
        errors: list[str] = []
        for table_name in sorted(name for name in by_table if name):
            group = sorted(
                by_table[table_name],
                key=lambda row: str(row.get("生效日期") or ""),
                reverse=True,
            )
            winner = group[0]
            reasons: list[str] = []
            if len(group) > 1:
                others = "、".join(
                    f"{item.get('方案编号')}（{item.get('生效日期') or '无生效日期'}）"
                    for item in group[1:]
                )
                reasons.append(
                    f"数据表 {table_name} 存在多个已启用分区方案，按生效日期更晚的 "
                    f"{winner.get('方案编号')}（{winner.get('生效日期')}）为准，{others} 不执行"
                )
            if str(winner.get("分区粒度")) not in SUPPORTED_GRANULARITY:
                errors.append(
                    f"{table_name}：分区粒度「{winner.get('分区粒度')}」暂不支持，治理计划当前仅支持按天"
                )
                continue
            param_error = self._validate_params(winner)
            if param_error:
                errors.append(f"{table_name}：{param_error}")
                continue

            policy = self._enabled_policy(table_name)
            if policy is not None:
                diffs = []
                if str(policy.get("保留天数")) != str(winner.get("保留天数")):
                    diffs.append(f"保留天数 策略{policy.get('保留天数')}/方案{winner.get('保留天数')}")
                if str(policy.get("冷数据阈值")) != str(winner.get("冷热阈值")):
                    diffs.append(f"冷数据阈值 策略{policy.get('冷数据阈值')}/方案{winner.get('冷热阈值')}")
                if str(policy.get("归档路径")) != str(winner.get("归档路径")):
                    diffs.append(f"归档路径 策略{policy.get('归档路径')}/方案{winner.get('归档路径')}")
                if diffs:
                    reasons.append(
                        f"生命周期策略 {policy.get('策略编号')} 与分区方案 {winner.get('方案编号')} "
                        f"冲突（{'、'.join(diffs)}），按生效日期更晚且已启用的分区方案为准"
                    )

            plan = self._upsert_plan(
                table_name=table_name,
                scheme=winner,
                policy=policy,
                conflict="；".join(reasons) if reasons else "无",
            )
            generated.append(plan)
            if reasons:
                conflicts.append({"数据表名": table_name, "冲突原因": plan["冲突原因"]})
        return {"generated": generated, "conflicts": conflicts, "errors": errors}

    def _enabled_policy(self, table_name: str) -> dict[str, Any] | None:
        policies = [
            row for row in store.rows("lifecycle")
            if row.get("status") == "已启用" and str(row.get("数据表名")) == table_name
        ]
        if not policies:
            return None
        policies.sort(key=lambda row: str(row.get("生效日期") or ""), reverse=True)
        return policies[0]

    def _upsert_plan(
        self,
        *,
        table_name: str,
        scheme: dict[str, Any],
        policy: dict[str, Any] | None,
        conflict: str,
    ) -> dict[str, Any]:
        rows = store.rows(MODULE)
        plan = next((row for row in rows if str(row.get("数据表名")) == table_name), None)
        values = {
            "数据表名": table_name,
            "分区粒度": str(scheme.get("分区粒度")),
            "预建天数": str(scheme.get("预建天数")),
            "保留天数": str(scheme.get("保留天数")),
            "冷热阈值": str(scheme.get("冷热阈值")),
            "归档路径": str(scheme.get("归档路径")),
            "生效日期": str(scheme.get("生效日期") or ""),
            "来源方案编号": str(scheme.get("方案编号") or ""),
            "来源策略编号": str(policy.get("策略编号")) if policy else "",
            "冲突原因": conflict,
        }
        if plan is None:
            plan = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            plan["计划编号"] = _next_code(rows, "计划编号", _PLAN_PREFIX)
            plan["版本"] = "1"
            plan.update(values)
            plan["status"] = PLAN_STATUSES[0]
            plan["pending"] = True
            plan["abnormal"] = False
            plan["最近演练编号"] = ""
            plan["最近作业编号"] = ""
            rows.append(plan)
        else:
            plan.update(values)
            plan["版本"] = str((_to_int(plan.get("版本"), 1) or 1) + 1)
            plan["status"] = PLAN_STATUSES[0]
            plan["pending"] = True
            plan["abnormal"] = False
            # 方案刷新后旧演练失效，必须重新演练才能执行
            plan["最近演练编号"] = ""
            plan["最近作业编号"] = ""
        return plan

    def _validate_params(self, row: dict[str, Any]) -> str | None:
        precreate = _to_int(row.get("预建天数"))
        retention = _to_int(row.get("保留天数"))
        cold = _to_int(row.get("冷热阈值"))
        if precreate is None or retention is None or cold is None:
            return "预建天数、保留天数、冷热阈值必须是整数"
        if precreate < 0 or cold < precreate or retention < cold:
            return "参数需满足 保留天数 ≥ 冷热阈值 ≥ 预建天数 ≥ 0"
        if not str(row.get("归档路径") or "").strip():
            return "归档路径不能为空"
        return None

    # ------------------------------------------------------------------
    # 演练：只计算范围，不改动数据
    # ------------------------------------------------------------------
    def dryrun(self, plan_id: int) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
        plan = store.find(MODULE, plan_id)
        if plan is None:
            return None, f"治理计划 {plan_id} 不存在或已归档", {}
        param_error = self._validate_params(plan)
        if param_error:
            return None, f"计划参数不完整：{param_error}", {}
        scope = self._compute_scope(plan)
        run = self._new_run(plan, "演练")
        run["status"] = "已完成"
        run["结束时间"] = self._now()
        run["演练范围"] = scope
        run["说明"] = (
            f"演练完成：将新建 {scope['新建']['数量']} 个、可归档 {scope['归档']['数量']} 个、"
            f"将删除 {scope['删除']['数量']} 个分区，仍保留 {scope['保留']['数量']} 个热分区；未改动任何数据"
        )
        plan["status"] = "已演练"
        plan["pending"] = True
        plan["最近演练编号"] = run["作业编号"]
        self._audit(
            str(plan.get("数据表名")),
            "治理演练",
            "",
            run["说明"],
            f"计划 {plan.get('计划编号')} 第{plan.get('版本')}版演练",
        )
        return plan, run["说明"], scope

    def _compute_scope(self, plan: dict[str, Any]) -> dict[str, Any]:
        """按分区粒度、预建天数、保留天数、冷热阈值推算四类分区范围。

        口径（以按天分区为例）：锚点 = 今天 + 预建天数。
        - 将新建：(今天, 锚点]，共 预建天数 个；
        - 仍保留（热）：(锚点 - 冷热阈值, 今天]，共 冷热阈值 - 预建天数 个；
        - 可归档（冷）：(锚点 - 保留天数, 锚点 - 冷热阈值]，共 保留天数 - 冷热阈值 个；
        - 将删除：已存在且不晚于 锚点 - 保留天数 的超期分区。
        """
        today = date.today()
        precreate = _to_int(plan.get("预建天数"), 0) or 0
        retention = _to_int(plan.get("保留天数"), 0) or 0
        cold = _to_int(plan.get("冷热阈值"), 0) or 0
        overdue = _to_int(plan.get("模拟超期分区数"), DEFAULT_OVERDUE_PARTITIONS)
        if overdue is None or overdue < 0:
            overdue = DEFAULT_OVERDUE_PARTITIONS

        anchor = today + timedelta(days=precreate)
        keep_start = anchor - timedelta(days=cold - 1)
        archive_start = anchor - timedelta(days=retention - 1)
        delete_end = anchor - timedelta(days=retention)
        inventory_start = delete_end - timedelta(days=overdue - 1)

        table_name = str(plan.get("数据表名") or "")
        state = self._partition_state(table_name)

        existing: set[date] = set()
        day = inventory_start
        while day <= today:
            existing.add(day)
            day += timedelta(days=1)
        for label, kind in state.items():
            day = _partition_day(label)
            if day is None:
                continue
            if kind == "删除":
                existing.discard(day)
            else:
                existing.add(day)

        created = {label for label, kind in state.items() if kind == "新建"}
        archived = {label for label, kind in state.items() if kind == "归档"}

        create_days = [
            today + timedelta(days=offset)
            for offset in range(1, precreate + 1)
            if _partition_label(today + timedelta(days=offset)) not in created
        ]
        keep_days = sorted(day for day in existing if keep_start <= day <= today)
        archive_days = sorted(
            day for day in existing
            if archive_start <= day < keep_start and _partition_label(day) not in archived
        )
        delete_days = sorted(day for day in existing if day <= delete_end)

        return {
            "基准日期": today.isoformat(),
            "锚点日期": anchor.isoformat(),
            "参数": {"预建天数": precreate, "保留天数": retention, "冷热阈值": cold},
            "新建": {
                "起始": create_days[0].isoformat() if create_days else "—",
                "截止": create_days[-1].isoformat() if create_days else "—",
                "数量": len(create_days),
                "分区": [_partition_label(day) for day in create_days],
            },
            "保留": {
                "起始": keep_days[0].isoformat() if keep_days else "—",
                "截止": keep_days[-1].isoformat() if keep_days else "—",
                "数量": len(keep_days),
                "分区": [_partition_label(day) for day in keep_days],
            },
            "归档": {
                "起始": archive_days[0].isoformat() if archive_days else "—",
                "截止": archive_days[-1].isoformat() if archive_days else "—",
                "数量": len(archive_days),
                "分区": [_partition_label(day) for day in archive_days],
            },
            "删除": {
                "起始": delete_days[0].isoformat() if delete_days else "—",
                "截止": delete_days[-1].isoformat() if delete_days else "—",
                "数量": len(delete_days),
                "分区": [
                    {
                        "分区": _partition_label(day),
                        "归档证据": "已就绪" if _partition_label(day) in archived else "缺失",
                    }
                    for day in delete_days
                ],
            },
        }

    # ------------------------------------------------------------------
    # 正式执行与续做
    # ------------------------------------------------------------------
    def execute(
        self,
        plan_id: int,
        *,
        interrupt_after: int | None = None,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        plan = store.find(MODULE, plan_id)
        if plan is None:
            return None, f"治理计划 {plan_id} 不存在或已归档", None
        if not str(plan.get("最近演练编号") or "").strip():
            return None, "请先演练再正式执行：当前版本还没有演练记录", None
        param_error = self._validate_params(plan)
        if param_error:
            return None, f"计划参数不完整：{param_error}", None
        with self._lock:
            active = self._active_run(plan)
            if active is not None:
                return None, f"作业 {active.get('作业编号')} 正在执行中，同一计划只允许一个作业生效", None
            run = self._new_run(plan, "正式执行")
            plan["status"] = "执行中"
            plan["最近作业编号"] = run["作业编号"]

        scope = self._compute_scope(plan)
        tasks = self._build_tasks(scope)
        run["任务列表"] = tasks
        processed = 0
        interrupted = False
        for task in tasks:
            if interrupt_after is not None and processed >= interrupt_after:
                interrupted = True
                break
            self._apply_task(plan, run, task)
            processed += 1
        message = self._finalize_run(plan, run, interrupted=interrupted)
        return plan, message, run

    def resume(self, plan_id: int) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        plan = store.find(MODULE, plan_id)
        if plan is None:
            return None, f"治理计划 {plan_id} 不存在或已归档", None
        with self._lock:
            active = self._active_run(plan)
            if active is not None:
                return None, f"作业 {active.get('作业编号')} 正在执行中，同一计划只允许一个作业生效", None
            candidates = [
                run for run in self._runs_of(plan)
                if run.get("作业类型") == "正式执行" and run.get("status") in ("已中断", "部分失败")
            ]
            if not candidates:
                return None, "没有可续做的作业：最近一次执行已完成或尚未执行", None
            run = candidates[-1]
            todo = [
                task for task in run.get("任务列表", [])
                if task.get("结果") in ("待执行", "失败", "阻断")
            ]
            if not todo:
                return None, f"作业 {run.get('作业编号')} 没有待续做的分区", None
            run["status"] = "执行中"
            plan["status"] = "执行中"

        # 只续做待执行/失败/阻断的分区；已经成功的分区不重复迁移或删除
        for task in todo:
            self._apply_task(plan, run, task)
        message = self._finalize_run(plan, run, interrupted=False)
        return plan, f"已从失败分区续做：{message}", run

    def _active_run(self, plan: dict[str, Any]) -> dict[str, Any] | None:
        for run in self._runs_of(plan):
            if run.get("status") == "执行中":
                return run
        return None

    def _build_tasks(self, scope: dict[str, Any]) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        for label in scope["新建"]["分区"]:
            tasks.append({"分区": label, "操作": "新建", "结果": "待执行", "说明": ""})
        for label in scope["归档"]["分区"]:
            tasks.append({"分区": label, "操作": "归档", "结果": "待执行", "说明": ""})
        for item in scope["删除"]["分区"]:
            tasks.append({"分区": item["分区"], "操作": "删除", "结果": "待执行", "说明": ""})
        return tasks

    def _apply_task(self, plan: dict[str, Any], run: dict[str, Any], task: dict[str, Any]) -> None:
        table_name = str(plan.get("数据表名") or "")
        label = str(task.get("分区") or "")
        op = str(task.get("操作") or "")
        state = self._partition_state(table_name)
        # 幂等：台账里已经成功的分区不重复迁移或删除
        if op == "新建" and state.get(label) == "新建":
            task["结果"] = "成功"
            task["说明"] = "台账已记录新建完成，跳过重复执行"
            return
        if op == "归档" and state.get(label) == "归档":
            task["结果"] = "成功"
            task["说明"] = "已存在归档完成证据，跳过重复迁移"
            return
        if op == "删除" and state.get(label) == "删除":
            task["结果"] = "成功"
            task["说明"] = "台账已记录删除完成，跳过重复删除"
            return
        if op == "删除" and state.get(label) != "归档":
            task["结果"] = "阻断"
            task["说明"] = "缺少已持久化的归档完成证据，未进入删除"
            self._audit(
                table_name, "治理阻断", label, "未删除",
                f"作业 {run.get('作业编号')}：分区 {label} 缺少归档完成证据",
            )
            return
        try:
            entry = self._append_ledger(plan, run, op, label)
        except OSError as exc:
            task["结果"] = "失败"
            task["说明"] = f"台账持久化失败：{exc}"
            return
        task["结果"] = "成功"
        if op == "归档":
            task["说明"] = f"已迁移到 {entry.get('归档路径')}，证据 {entry.get('证据编号')} 已持久化"
        elif op == "删除":
            task["说明"] = "归档证据核验通过，分区已删除"
        else:
            task["说明"] = "分区已预建"
        self._audit(
            table_name,
            f"治理{op}",
            label,
            str(task.get("说明")),
            f"作业 {run.get('作业编号')} 执行",
        )

    def _finalize_run(
        self,
        plan: dict[str, Any],
        run: dict[str, Any],
        *,
        interrupted: bool,
    ) -> str:
        tasks = run.get("任务列表", [])
        succeeded = [task for task in tasks if task.get("结果") == "成功"]
        blocked = [task for task in tasks if task.get("结果") in ("阻断", "失败")]
        waiting = [task for task in tasks if task.get("结果") == "待执行"]
        counts = {
            op: sum(1 for task in succeeded if task.get("操作") == op)
            for op in ("新建", "归档", "删除")
        }

        if interrupted or waiting:
            run["status"] = "已中断"
            plan["status"] = "已中断"
        elif blocked:
            run["status"] = "部分失败"
            plan["status"] = "部分失败"
        else:
            run["status"] = "已完成"
            plan["status"] = "已完成"
        plan["pending"] = plan["status"] not in ("已完成",)
        plan["abnormal"] = plan["status"] == "部分失败"
        run["结束时间"] = self._now()
        run["结果统计"] = {
            "新建成功": counts["新建"],
            "归档成功": counts["归档"],
            "删除成功": counts["删除"],
            "阻断或失败": len(blocked),
            "待执行": len(waiting),
        }

        # 演练与实际执行差异：以最近一次演练范围为基准
        run["演练差异"] = self._diff_with_dryrun(plan, succeeded)

        # 成本联动：归档/删除成功的分区按估算口径释放存储，只入账新增部分
        released = (counts["归档"] + counts["删除"]) * PARTITION_GB
        billed = _to_int(run.get("成本已入账GB"), 0) or 0
        delta = released - billed
        bill_message = ""
        if delta > 0:
            bill_message = self._apply_cost_impact(plan, run, delta)
            run["成本已入账GB"] = released
        run["成本影响"] = {
            "释放存储GB": released,
            "估算口径": f"单分区 {PARTITION_GB}GB",
            "联动账单": bill_message or "无变化",
        }

        run["说明"] = (
            f"作业{run.get('status')}：新建 {counts['新建']}、归档 {counts['归档']}、"
            f"删除 {counts['删除']}、阻断/失败 {len(blocked)}、待执行 {len(waiting)}"
        )
        self._audit(
            str(plan.get("数据表名")),
            "治理执行",
            f"计划 {plan.get('计划编号')} 第{plan.get('版本')}版",
            run["说明"],
            f"作业 {run.get('作业编号')} 执行结论",
        )
        return str(run["说明"])

    def _diff_with_dryrun(
        self,
        plan: dict[str, Any],
        succeeded: list[dict[str, Any]],
    ) -> dict[str, Any]:
        dryrun_run = self._find_run(str(plan.get("最近演练编号") or ""))
        planned: set[str] = set()
        if dryrun_run and dryrun_run.get("演练范围"):
            scope = dryrun_run["演练范围"]
            planned |= {f"新建:{label}" for label in scope["新建"]["分区"]}
            planned |= {f"归档:{label}" for label in scope["归档"]["分区"]}
            planned |= {f"删除:{item['分区']}" for item in scope["删除"]["分区"]}
        actual = {f"{task.get('操作')}:{task.get('分区')}" for task in succeeded}
        return {
            "计划外执行": sorted(actual - planned),
            "未执行": sorted(planned - actual),
        }

    # ------------------------------------------------------------------
    # 归档证据台账（落盘持久化）
    # ------------------------------------------------------------------
    def register_evidence(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        table_name = str(values.get("数据表名") or "").strip()
        label = str(values.get("分区") or "").strip()
        path = str(values.get("归档路径") or "").strip()
        missing = [
            name for name, text in (("数据表名", table_name), ("分区", label), ("归档路径", path))
            if not text
        ]
        if missing:
            return None, missing
        if _partition_day(label) is None:
            return None, ["分区（格式应为 dt=YYYY-MM-DD）"]
        state = self._partition_state(table_name)
        if state.get(label) == "归档":
            return None, [f"分区 {label} 已存在归档完成证据，无需重复登记"]
        rows = store.rows(LEDGER_MODULE)
        entry = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "证据编号": _next_code(rows, "证据编号", _EVIDENCE_PREFIX),
            "数据表名": table_name,
            "分区": label,
            "类型": "归档",
            "来源": "外部登记",
            "作业编号": "",
            "归档路径": path,
            "完成时间": self._now(),
            "操作人": str(values.get("操作人") or "归档系统"),
        }
        rows.append(entry)
        try:
            self._persist_ledger()
        except OSError as exc:
            rows.remove(entry)
            return None, [f"证据持久化失败：{exc}"]
        self._audit(
            table_name, "归档证据登记", label,
            f"证据 {entry['证据编号']} 已持久化，路径 {path}",
            "外部归档完成证据登记",
        )
        return entry, []

    def list_ledger(
        self,
        *,
        table: str | None = None,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = store.rows(LEDGER_MODULE)
        if table:
            rows = [row for row in rows if table in str(row.get("数据表名", ""))]
        if kind:
            rows = [row for row in rows if row.get("类型") == kind]
        return list(rows)

    def _append_ledger(
        self,
        plan: dict[str, Any],
        run: dict[str, Any],
        kind: str,
        label: str,
    ) -> dict[str, Any]:
        rows = store.rows(LEDGER_MODULE)
        table_name = str(plan.get("数据表名") or "")
        entry = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "证据编号": _next_code(rows, "证据编号", _EVIDENCE_PREFIX),
            "数据表名": table_name,
            "分区": label,
            "类型": kind,
            "来源": "治理作业",
            "作业编号": str(run.get("作业编号") or ""),
            "归档路径": f"{plan.get('归档路径')}/{label}" if kind == "归档" else "",
            "完成时间": self._now(),
            "操作人": "治理作业",
        }
        rows.append(entry)
        self._persist_ledger()
        return entry

    def _persist_ledger(self) -> None:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        LEDGER_PATH.write_text(
            json.dumps(store.rows(LEDGER_MODULE), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _partition_state(self, table_name: str) -> dict[str, str]:
        """台账里每个分区的最新状态：新建 → 归档 → 删除。"""
        state: dict[str, str] = {}
        for row in store.rows(LEDGER_MODULE):
            if str(row.get("数据表名")) == table_name:
                state[str(row.get("分区"))] = str(row.get("类型"))
        return state

    def _ledger_stats(self, table_name: str) -> dict[str, int]:
        stats = {"新建": 0, "归档": 0, "删除": 0}
        for kind in self._partition_state(table_name).values():
            if kind in stats:
                stats[kind] += 1
        return stats

    # ------------------------------------------------------------------
    # 成本联动
    # ------------------------------------------------------------------
    def _apply_cost_impact(self, plan: dict[str, Any], run: dict[str, Any], released_gb: int) -> str:
        table_name = str(plan.get("数据表名") or "")
        bills = [row for row in store.rows("cost") if str(row.get("数据表名")) == table_name]
        if not bills:
            return "无匹配账单，未联动"
        bills.sort(key=lambda row: str(row.get("统计月份") or ""), reverse=True)
        bill = bills[0]
        before = _to_int(bill.get("存储大小GB"), 0) or 0
        after = max(before - released_gb, 0)
        bill["存储大小GB"] = str(after)
        bill["治理联动"] = (
            f"作业 {run.get('作业编号')} 释放 {released_gb}GB"
            f"（归档/删除分区按单分区 {PARTITION_GB}GB 估算）"
        )
        self._audit(
            table_name,
            "成本联动",
            f"存储 {before}GB",
            f"存储 {after}GB",
            f"作业 {run.get('作业编号')} 释放 {released_gb}GB，账单 {bill.get('账单编号')}",
        )
        return str(bill.get("账单编号") or "")

    def impact(self, table: str | None = None) -> list[dict[str, Any]]:
        tables: set[str] = set()
        if table:
            tables.add(table)
        else:
            for row in store.rows(MODULE):
                tables.add(str(row.get("数据表名") or ""))
            for row in store.rows(LEDGER_MODULE):
                tables.add(str(row.get("数据表名") or ""))
            tables.discard("")
        return [self._impact_for_table(name) for name in sorted(tables)]

    def _impact_for_table(self, table_name: str) -> dict[str, Any]:
        runs = [
            run for run in store.rows(RUN_MODULE)
            if str(run.get("数据表名")) == table_name and run.get("作业类型") == "正式执行"
        ]
        latest = runs[-1] if runs else None
        released = sum(
            _to_int((run.get("成本影响") or {}).get("释放存储GB"), 0) or 0 for run in runs
        )
        bills = [row for row in store.rows("cost") if str(row.get("数据表名")) == table_name]
        bills.sort(key=lambda row: str(row.get("统计月份") or ""), reverse=True)
        bill = bills[0] if bills else None
        audits = [
            row for row in store.rows("auditlog")
            if str(row.get("操作表名")) == table_name
            and str(row.get("变更类型", "")).startswith(("治理", "成本联动", "归档证据"))
        ]
        return {
            "数据表名": table_name,
            "最近作业编号": (latest or {}).get("作业编号") or "—",
            "最近作业状态": (latest or {}).get("status") or "—",
            "最近结论": (latest or {}).get("说明") or "尚未执行",
            "累计释放存储GB": released,
            "台账统计": self._ledger_stats(table_name),
            "账单编号": (bill or {}).get("账单编号") or "—",
            "当前存储GB": (bill or {}).get("存储大小GB") or "—",
            "治理联动": (bill or {}).get("治理联动") or "—",
            "最近审计": [
                f"{row.get('记录编号')} {row.get('变更类型')}" for row in audits[-5:]
            ],
        }

    # ------------------------------------------------------------------
    # 作业与审计
    # ------------------------------------------------------------------
    def list_runs(
        self,
        *,
        plan_code: str | None = None,
        table: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = store.rows(RUN_MODULE)
        if plan_code:
            rows = [row for row in rows if str(row.get("计划编号")) == plan_code]
        if table:
            rows = [row for row in rows if table in str(row.get("数据表名", ""))]
        return list(rows)

    def _runs_of(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            run for run in store.rows(RUN_MODULE)
            if str(run.get("计划编号")) == str(plan.get("计划编号"))
        ]

    def _find_run(self, run_code: str) -> dict[str, Any] | None:
        if not run_code:
            return None
        for run in store.rows(RUN_MODULE):
            if str(run.get("作业编号")) == run_code:
                return run
        return None

    def _new_run(self, plan: dict[str, Any], kind: str) -> dict[str, Any]:
        rows = store.rows(RUN_MODULE)
        run = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "作业编号": _next_code(rows, "作业编号", _RUN_PREFIX),
            "计划编号": str(plan.get("计划编号") or ""),
            "计划版本": str(plan.get("版本") or ""),
            "数据表名": str(plan.get("数据表名") or ""),
            "作业类型": kind,
            "status": "执行中",
            "开始时间": self._now(),
            "结束时间": "",
            "演练范围": None,
            "任务列表": [],
            "结果统计": {},
            "演练差异": {},
            "成本影响": {},
            "成本已入账GB": "0",
            "说明": "",
        }
        rows.append(run)
        return run

    def _audit(
        self,
        table_name: str,
        change_type: str,
        before: str,
        after: str,
        reason: str,
    ) -> None:
        rows = store.rows("auditlog")
        rows.append({
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "记录编号": _next_code(rows, "记录编号", _AUDIT_PREFIX),
            "操作表名": table_name,
            "变更类型": change_type,
            "变更前值": before,
            "变更后值": after,
            "操作人": "治理作业",
            "操作时间": self._now(),
            "变更原因": reason,
            "status": "已记录",
            "pending": False,
            "abnormal": False,
        })

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
