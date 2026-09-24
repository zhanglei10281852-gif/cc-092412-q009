from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.errors import NotFoundError
from app.repositories.base import row_dict, rows_dict

OPEN_AFFAIR_STATUSES = ("待受理", "办理中")
OPEN_PETITION_STATUSES = ("待签收", "待分派", "办理中", "待审核", "退回重办", "复查中")

# 取指定时刻生效的部门快照：该时刻之前最近一次快照，没有快照时回退到部门当前行
SNAPSHOT_AT_SQL = (
    "SELECT * FROM department_snapshots WHERE department_id=? AND effective_at<=? "
    "ORDER BY effective_at DESC,id DESC LIMIT 1"
)

SNAPSHOT_EARLIEST_SQL = "SELECT * FROM department_snapshots WHERE department_id=? ORDER BY effective_at ASC,id ASC LIMIT 1"


def historical_department_name_sql(department_ref: str, moment_ref: str) -> str:
    """生成"记录发生时刻的部门名称"SQL 片段：优先当时快照，早于所有快照时取最早快照，无快照回退当前名称。"""
    return (
        "COALESCE("
        f"(SELECT s.name FROM department_snapshots s WHERE s.department_id={department_ref} "
        f"AND s.effective_at<={moment_ref} ORDER BY s.effective_at DESC,s.id DESC LIMIT 1),"
        f"(SELECT s.name FROM department_snapshots s WHERE s.department_id={department_ref} "
        "ORDER BY s.effective_at ASC,s.id ASC LIMIT 1),"
        f"(SELECT d0.name FROM departments d0 WHERE d0.id={department_ref}))"
    )


def decode_plan(row: dict[str, Any]) -> dict[str, Any]:
    row["payload"] = json.loads(row.pop("payload_json"))
    return row


class OrgChangeRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def plan(self, plan_id: int) -> dict[str, Any] | None:
        row = row_dict(self.connection.execute("SELECT * FROM org_changes WHERE id=?", (plan_id,)).fetchone())
        return decode_plan(row) if row else None

    def require_plan(self, plan_id: int) -> dict[str, Any]:
        plan = self.plan(plan_id)
        if plan is None:
            raise NotFoundError("组织变更计划不存在")
        return plan

    def list_plans(self, *, status: str | None, change_type: str | None, limit: int, offset: int) -> list[dict]:
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("status=?")
            params.append(status)
        if change_type:
            conditions.append("change_type=?")
            params.append(change_type)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        params.extend([limit, offset])
        rows = rows_dict(self.connection.execute(
            "SELECT * FROM org_changes" + where + " ORDER BY id DESC LIMIT ? OFFSET ?", tuple(params)
        ).fetchall())
        return [decode_plan(row) for row in rows]

    def count_plans(self, *, status: str | None, change_type: str | None) -> int:
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("status=?")
            params.append(status)
        if change_type:
            conditions.append("change_type=?")
            params.append(change_type)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        return int(self.connection.execute("SELECT COUNT(*) FROM org_changes" + where, tuple(params)).fetchone()[0])

    def due_plans(self, moment: str) -> list[dict]:
        rows = rows_dict(self.connection.execute(
            "SELECT * FROM org_changes WHERE status='planned' AND effective_at<=? ORDER BY effective_at,id",
            (moment,),
        ).fetchall())
        return [decode_plan(row) for row in rows]

    def planned_plans_touching(self, department_ids: list[int], *, exclude_plan_id: int | None = None) -> list[dict]:
        """找出仍以这些部门为来源部门的待生效计划，用于防止变更计划交叠。"""
        if not department_ids:
            return []
        rows = rows_dict(self.connection.execute(
            "SELECT * FROM org_changes WHERE status='planned' ORDER BY id"
        ).fetchall())
        found: list[dict] = []
        wanted = set(department_ids)
        for row in rows:
            if exclude_plan_id is not None and row["id"] == exclude_plan_id:
                continue
            plan = decode_plan(row)
            if wanted & set(plan_source_departments(plan)):
                found.append(plan)
        return found

    def items(self, change_id: int, *, status: str | None = None) -> list[dict]:
        conditions = ["i.change_id=?"]
        params: list[Any] = [change_id]
        if status:
            conditions.append("i.status=?")
            params.append(status)
        return rows_dict(self.connection.execute(
            "SELECT i.*,s.name AS source_department_name,t.name AS target_department_name "
            "FROM org_change_items i "
            "JOIN departments s ON s.id=i.source_department_id "
            "LEFT JOIN departments t ON t.id=i.target_department_id "
            "WHERE " + " AND ".join(conditions) + " ORDER BY i.id",
            tuple(params),
        ).fetchall())

    def item(self, item_id: int) -> dict[str, Any] | None:
        return row_dict(self.connection.execute("SELECT * FROM org_change_items WHERE id=?", (item_id,)).fetchone())

    def item_for(self, change_id: int, item_type: str, item_id: int) -> dict[str, Any] | None:
        return row_dict(self.connection.execute(
            "SELECT * FROM org_change_items WHERE change_id=? AND item_type=? AND item_id=?",
            (change_id, item_type, item_id),
        ).fetchone())

    def count_items(self, change_id: int, statuses: tuple[str, ...]) -> int:
        placeholders = ",".join("?" for _ in statuses)
        return int(self.connection.execute(
            f"SELECT COUNT(*) FROM org_change_items WHERE change_id=? AND status IN ({placeholders})",
            (change_id, *statuses),
        ).fetchone()[0])

    def successions(self, change_id: int) -> list[dict]:
        return rows_dict(self.connection.execute(
            "SELECT s.*,p.name AS predecessor_name,t.name AS successor_name FROM department_successions s "
            "JOIN departments p ON p.id=s.predecessor_id JOIN departments t ON t.id=s.successor_id "
            "WHERE s.change_id=? ORDER BY s.id",
            (change_id,),
        ).fetchall())

    def successions_for_department(self, department_id: int) -> list[dict]:
        return rows_dict(self.connection.execute(
            "SELECT s.*,p.name AS predecessor_name,t.name AS successor_name FROM department_successions s "
            "JOIN departments p ON p.id=s.predecessor_id JOIN departments t ON t.id=s.successor_id "
            "WHERE s.predecessor_id=? OR s.successor_id=? ORDER BY s.effective_at,s.id",
            (department_id, department_id),
        ).fetchall())

    def snapshot_at(self, department_id: int, moment: str) -> dict[str, Any] | None:
        row = row_dict(self.connection.execute(SNAPSHOT_AT_SQL, (department_id, moment)).fetchone())
        if row is None:
            row = row_dict(self.connection.execute(SNAPSHOT_EARLIEST_SQL, (department_id,)).fetchone())
        return row

    def snapshots(self, department_id: int) -> list[dict]:
        return rows_dict(self.connection.execute(
            "SELECT * FROM department_snapshots WHERE department_id=? ORDER BY effective_at,id",
            (department_id,),
        ).fetchall())

    def open_affairs(self, department_id: int) -> list[dict]:
        placeholders = ",".join("?" for _ in OPEN_AFFAIR_STATUSES)
        return rows_dict(self.connection.execute(
            f"SELECT id,category,status FROM affairs WHERE department_id=? AND status IN ({placeholders}) ORDER BY id",
            (department_id, *OPEN_AFFAIR_STATUSES),
        ).fetchall())

    def open_petitions(self, department_id: int) -> list[dict]:
        placeholders = ",".join("?" for _ in OPEN_PETITION_STATUSES)
        return rows_dict(self.connection.execute(
            f"SELECT id,type,status FROM petitions WHERE department_id=? AND status IN ({placeholders}) ORDER BY id",
            (department_id, *OPEN_PETITION_STATUSES),
        ).fetchall())

    def active_memberships_at(self, department_id: int, moment: str) -> list[dict]:
        return rows_dict(self.connection.execute(
            "SELECT * FROM department_memberships WHERE department_id=? AND starts_at<=? "
            "AND (ends_at IS NULL OR ends_at>?) ORDER BY id",
            (department_id, moment, moment),
        ).fetchall())


def plan_source_departments(plan: dict[str, Any]) -> list[int]:
    """从计划负载中解析来源部门 id 列表。"""
    payload = plan.get("payload") or {}
    change_type = plan.get("change_type")
    if change_type in {"rename", "deactivate"}:
        return [int(payload["department_id"])] if payload.get("department_id") is not None else []
    if change_type == "merge":
        return [int(value) for value in payload.get("source_department_ids", [])]
    if change_type == "split":
        return [int(payload["source_department_id"])] if payload.get("source_department_id") is not None else []
    return []
