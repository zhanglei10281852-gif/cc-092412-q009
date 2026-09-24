from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.pagination import Page, page_result
from app.core.security import Principal
from app.database import get_connection, transaction
from app.schemas.orgchange import OrgChangeCreateRequest, OrgChangeItemResolveRequest
from app.services.orgchange import OrgChangeService

router = APIRouter(prefix="/api/org-changes", tags=["组织变更"])


@router.get("")
def list_plans(
    status: str | None = None,
    change_type: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(current_principal),
) -> dict:
    pagination = Page(page, size)
    total, rows = OrgChangeService(get_connection()).list_plans(
        principal, status=status, change_type=change_type, limit=size, offset=pagination.offset
    )
    return page_result(total=total, page=pagination, rows=rows)


@router.post("", status_code=201)
def create_plan(data: OrgChangeCreateRequest, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return OrgChangeService(connection).create_plan(principal, data.model_dump())


@router.post("/apply-due")
def apply_due_plans(principal: Principal = Depends(current_principal)) -> dict:
    principal.require("departments.write")
    with transaction(immediate=True) as connection:
        results = OrgChangeService(connection).apply_due(actor_name=principal.display_name)
    return {"results": results}


@router.get("/{plan_id}")
def get_plan(plan_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return OrgChangeService(get_connection()).get_plan(principal, plan_id)


@router.post("/{plan_id}/apply")
def apply_plan(plan_id: int, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return OrgChangeService(connection).apply(principal, plan_id)


@router.post("/{plan_id}/revoke")
def revoke_plan(plan_id: int, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return OrgChangeService(connection).revoke(principal, plan_id)


@router.post("/{plan_id}/items/{item_id}/resolve")
def resolve_item(
    plan_id: int,
    item_id: int,
    data: OrgChangeItemResolveRequest,
    principal: Principal = Depends(current_principal),
) -> dict:
    with transaction(immediate=True) as connection:
        return OrgChangeService(connection).resolve_item(
            principal, plan_id, item_id, data.target_department_id, data.resolution
        )
