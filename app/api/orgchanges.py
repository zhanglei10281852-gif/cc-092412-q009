from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.clock import from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.pagination import Page
from app.core.security import Principal
from app.database import get_connection, transaction
from app.repositories.orgchange import DepartmentTimelineRepository, OrgChangePlanRepository, OrgChangeTargetRepository
from app.schemas.orgchange import OrgChangeConflictResolve, OrgChangePlanCreate
from app.services.orgchange import OrgChangeService, apply_due_plans

router = APIRouter(prefix="/api/org-changes", tags=["组织变更"])


@router.post("/plans", status_code=201)
def create_plan(data: OrgChangePlanCreate, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return OrgChangeService(connection).create_plan(principal, data.model_dump())


@router.get("/plans")
def list_plans(
    status: str | None = Query(default=None, pattern="^(planned|applied|revoked|conflict|failed)$"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("orgchanges.read")
    pagination = Page(page, size)
    repository = OrgChangePlanRepository(get_connection())
    rows = repository.list(status=status, limit=size, offset=pagination.offset)
    where = "status=?" if status else ""
    total = repository.count(where, (status,) if status else ())
    return {"page": page, "size": size, "total": total, "data": rows}


@router.get("/plans/{plan_id}")
def get_plan(plan_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("orgchanges.read")
    return OrgChangeService(get_connection()).plan_detail(plan_id)


@router.post("/plans/{plan_id}/cancel")
def cancel_plan(plan_id: int, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return OrgChangeService(connection).cancel_plan(principal, plan_id)


@router.post("/plans/{plan_id}/apply")
def apply_plan(plan_id: int, principal: Principal = Depends(current_principal)) -> dict:
    """立即执行一个已到期的计划（用于手动补发或失败后重试；重复调用幂等）。"""
    principal.require("orgchanges.write")
    with transaction(immediate=True) as connection:
        service = OrgChangeService(connection)
        plan = service.plans.require(plan_id)
        if plan["status"] not in {"planned", "failed"}:
            raise ConflictError("只有未生效或执行失败的计划可以执行")
        effective = from_storage(plan["effective_at"])
        if effective is not None and effective > service.clock.now():
            raise ConflictError("计划未到生效时间，不能提前执行")
        return service.apply_plan(plan_id, actor_user_id=principal.user_id, actor_name=principal.display_name)


@router.post("/apply-due")
def apply_due(principal: Principal = Depends(current_principal)) -> dict:
    principal.require("orgchanges.write")
    results = apply_due_plans()
    return {"applied": len(results), "results": results}


@router.get("/conflicts")
def list_conflicts(
    resource_type: str | None = Query(default=None, pattern="^(affair|petition|member)$"),
    department_id: int | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("orgchanges.read")
    pagination = Page(page, size)
    connection = get_connection()
    conditions = ["t.status='pending'"]
    params: list = []
    if resource_type:
        conditions.append("t.resource_type=?")
        params.append(resource_type)
    if department_id is not None:
        conditions.append("t.current_department_id=?")
        params.append(department_id)
    where = " AND ".join(conditions)
    total = int(connection.execute(
        "SELECT COUNT(*) FROM org_change_targets t WHERE " + where, tuple(params)
    ).fetchone()[0])
    repository = OrgChangeTargetRepository(connection)
    rows = repository.list_pending(
        resource_type=resource_type, department_id=department_id, limit=size, offset=pagination.offset
    )
    return {"page": page, "size": size, "total": total, "data": rows}


@router.post("/conflicts/{target_id}/resolve")
def resolve_conflict(
    target_id: int, data: OrgChangeConflictResolve, principal: Principal = Depends(current_principal)
) -> dict:
    with transaction(immediate=True) as connection:
        return OrgChangeService(connection).resolve_conflict(
            principal, target_id, data.department_id, data.remark
        )


timeline_router = APIRouter(prefix="/api/departments", tags=["组织沿革"])


@timeline_router.get("/timeline/as-of")
def departments_as_of(
    at: str = Query(..., description="ISO 8601 时刻，按当时组织还原部门视图"),
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("departments.read")
    try:
        moment = from_storage(at)
    except ValueError as exc:
        raise ValidationError("at 必须是有效的 ISO 8601 时间") from exc
    if moment is None:
        raise ValidationError("at 必须是有效的 ISO 8601 时间")
    rows = DepartmentTimelineRepository(get_connection()).departments_as_of(to_storage(moment))
    return {"at": to_storage(moment), "data": rows}


@timeline_router.get("/{department_id}/timeline")
def department_timeline(department_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("departments.read")
    connection = get_connection()
    repository = DepartmentTimelineRepository(connection)
    department = connection.execute("SELECT * FROM departments WHERE id=?", (department_id,)).fetchone()
    if department is None:
        raise NotFoundError("部门不存在")
    return {
        "department": dict(department),
        "aliases": repository.aliases(department_id),
        "successors": repository.successors_of(department_id),
        "predecessors": repository.predecessors_of(department_id),
    }
