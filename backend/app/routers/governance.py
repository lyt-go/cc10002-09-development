"""治理计划接口：先演练后执行的分区治理计划，覆盖生成、演练、执行、续做与证据登记。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.governance import GovernanceService

router = APIRouter(prefix="/api/governance", tags=["治理计划"])

service = GovernanceService()

LIST_FIELDS = ["计划编号", "数据表名", "分区粒度", "预建天数", "保留天数", "冷热阈值", "归档路径", "生效日期", "冲突原因"]
STATUSES = ["草稿", "已演练", "执行中", "已中断", "已完成", "部分失败"]


@router.get("", response_model=PageResult[dict])
def list_plans(
    keyword: str | None = Query(default=None, description="按计划编号检索"),
    table: str | None = Query(default=None, description="按数据表名检索"),
    status: str | None = Query(default=None, description="草稿、已演练、执行中、已中断、已完成、部分失败"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """分页列出治理计划；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_plans(keyword=keyword, table=table, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/generate", response_model=ActionResult)
def generate_plans(payload: EntryPayload) -> ActionResult:
    """由已启用的分区方案生成或刷新治理计划；冲突按生效日期更晚且已启用的方案裁决并保留原因。"""
    table = str(payload.values.get("数据表名") or "").strip() or None
    result = service.generate_plans(table)
    generated = result["generated"]
    conflicts = result["conflicts"]
    errors = result["errors"]
    if not generated and not errors:
        return ActionResult(ok=False, message="没有已启用的分区方案可生成治理计划")
    parts = [f"已生成/刷新 {len(generated)} 条治理计划"]
    if conflicts:
        parts.append(f"保留 {len(conflicts)} 条冲突原因")
    if errors:
        parts.append(f"{len(errors)} 张表被跳过：{'；'.join(errors)}")
    return ActionResult(
        ok=True,
        message="，".join(parts),
        entry={"plans": generated, "conflicts": conflicts, "errors": errors},
    )


@router.get("/runs")
def list_runs(
    plan_code: str | None = Query(default=None, description="按计划编号过滤"),
    table: str | None = Query(default=None, description="按数据表名过滤"),
) -> dict[str, Any]:
    """治理作业列表：演练与正式执行的历史都留在这里。"""
    items = service.list_runs(plan_code=plan_code, table=table)
    return {"items": items, "total": len(items)}


@router.get("/ledger")
def list_ledger(
    table: str | None = Query(default=None, description="按数据表名过滤"),
    kind: str | None = Query(default=None, description="按类型过滤：新建、归档、删除"),
) -> dict[str, Any]:
    """归档证据与分区状态台账：删除前核验的就是这里持久化的证据。"""
    items = service.list_ledger(table=table, kind=kind)
    return {"items": items, "total": len(items)}


@router.get("/impact")
def governance_impact(
    table: str | None = Query(default=None, description="按数据表名过滤"),
) -> dict[str, Any]:
    """治理结论与成本账单的联动视图，供成本页跨页引用。"""
    items = service.impact(table)
    return {"items": items, "total": len(items)}


@router.post("/evidence", response_model=ActionResult)
def register_evidence(payload: EntryPayload) -> ActionResult:
    """登记外部系统的归档完成证据；证据落盘持久化后才允许对应分区进入删除。"""
    entry, missing = service.register_evidence(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"证据登记失败：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"归档完成证据 {entry['证据编号']} 已持久化", entry=entry)


@router.get("/export")
def export_plans() -> dict[str, Any]:
    """导出治理计划清单：返回当前全量计划。"""
    items, total = service.list_plans(page=1, size=10000)
    return {"module": "governance", "total": total, "items": items}


@router.get("/{entry_id}")
def get_plan(entry_id: int) -> dict[str, Any]:
    """治理计划详情：含最近演练范围、最近执行结果、台账统计与成本影响。"""
    detail = service.plan_detail(entry_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"治理计划 {entry_id} 不存在或已归档")
    return detail


@router.post("/{entry_id}/dryrun", response_model=ActionResult)
def dryrun_plan(entry_id: int) -> ActionResult:
    """演练：只给出将新建、将归档、将删除与仍保留的范围，不改动任何数据。"""
    plan, message, scope = service.dryrun(entry_id)
    if plan is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={"plan": plan, "演练范围": scope})


@router.post("/{entry_id}/execute", response_model=ActionResult)
def execute_plan(entry_id: int, payload: EntryPayload) -> ActionResult:
    """正式执行：逐分区记录结果；interrupt_after 用于演示中断，续做从失败分区接着跑。"""
    interrupt_after = payload.values.get("interrupt_after")
    try:
        interrupt_after = int(interrupt_after) if interrupt_after not in (None, "") else None
    except (TypeError, ValueError):
        return ActionResult(ok=False, message="interrupt_after 必须是正整数")
    if interrupt_after is not None and interrupt_after <= 0:
        interrupt_after = None
    plan, message, run = service.execute(entry_id, interrupt_after=interrupt_after)
    if plan is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={"plan": plan, "run": run})


@router.post("/{entry_id}/resume", response_model=ActionResult)
def resume_plan(entry_id: int) -> ActionResult:
    """续做：从失败或被阻断的分区接着执行，已经成功的分区不重复迁移或删除。"""
    plan, message, run = service.resume(entry_id)
    if plan is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={"plan": plan, "run": run})
