from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.repositories.base import Repository, row_dict, rows_dict


def _decode_plan(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    row["source_department_ids"] = json.loads(row["source_department_ids"])
    row["target_department_ids"] = json.loads(row["target_department_ids"])
    row["new_names"] = json.loads(row["new_names"])
    row["member_transfer"] = json.loads(row["member_transfer"])
    row["spec"] = json.loads(row.pop("spec_json") or "{}")
    return row


def _decode_target(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    row["candidate_department_ids"] = json.loads(row["candidate_department_ids"])
    return row


class OrgChangePlanRepository(Repository):
    table = "org_change_plans"
    entity_name = "组织变更计划"

    def get(self, entity_id: int) -> dict[str, Any] | None:
        return _decode_plan(super().get(entity_id))

    def by_code(self, code: str) -> dict[str, Any] | None:
        return _decode_plan(row_dict(self.connection.execute(
            "SELECT * FROM org_change_plans WHERE code=?", (code,)
        ).fetchone()))

    def list(self, *, status: str | None, limit: int, offset: int) -> list[dict]:
        where = " WHERE status=?" if status else ""
        params: tuple = (status, limit, offset) if status else (limit, offset)
        rows = rows_dict(self.connection.execute(
            "SELECT * FROM org_change_plans" + where + " ORDER BY effective_at, id LIMIT ? OFFSET ?", params
        ).fetchall())
        return [_decode_plan(row) for row in rows]

    def due_plans(self, moment: str) -> list[dict]:
        rows = rows_dict(self.connection.execute(
            "SELECT * FROM org_change_plans WHERE status='planned' AND julianday(effective_at)<=julianday(?) "
            "ORDER BY effective_at, id", (moment,)
        ).fetchall())
        return [_decode_plan(row) for row in rows]

    def planned_involving(self, department_ids: set[int]) -> list[dict]:
        """返回仍处于 planned 状态且源部门与给定集合有交集的计划，用于冲突校验。"""
        rows = rows_dict(self.connection.execute(
            "SELECT * FROM org_change_plans WHERE status='planned'"
        ).fetchall())
        result = []
        for row in rows:
            plan = _decode_plan(row)
            if set(plan["source_department_ids"]) & department_ids:
                result.append(plan)
        return result

    def open_conflict_count(self, plan_id: int) -> int:
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM org_change_targets WHERE plan_id=? AND status='pending'", (plan_id,)
        ).fetchone()[0])


class OrgChangeTargetRepository(Repository):
    table = "org_change_targets"
    entity_name = "组织变更待确认项"

    def get(self, entity_id: int) -> dict[str, Any] | None:
        return _decode_target(super().get(entity_id))

    def list_for_plan(self, plan_id: int, *, status: str | None = None) -> list[dict]:
        where = " WHERE plan_id=?" + (" AND status=?" if status else "")
        params: tuple = (plan_id, status) if status else (plan_id,)
        rows = rows_dict(self.connection.execute(
            "SELECT * FROM org_change_targets" + where + " ORDER BY id", params
        ).fetchall())
        return [_decode_target(row) for row in rows]

    def list_pending(self, *, resource_type: str | None, department_id: int | None, limit: int, offset: int) -> list[dict]:
        conditions = ["t.status='pending'"]
        params: list[Any] = []
        if resource_type:
            conditions.append("t.resource_type=?")
            params.append(resource_type)
        if department_id is not None:
            conditions.append("t.current_department_id=?")
            params.append(department_id)
        params.extend([limit, offset])
        rows = rows_dict(self.connection.execute(
            "SELECT t.*,p.code AS plan_code,p.change_type,p.effective_at FROM org_change_targets t "
            "JOIN org_change_plans p ON p.id=t.plan_id WHERE " + " AND ".join(conditions) +
            " ORDER BY t.id LIMIT ? OFFSET ?", tuple(params)
        ).fetchall())
        return [_decode_target(row) for row in rows]


class DepartmentTimelineRepository:
    """部门名称时间线与继任关系的只读查询。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def name_at(self, department_id: int, moment: str) -> str | None:
        row = self.connection.execute(
            "SELECT name FROM department_aliases WHERE department_id=? AND julianday(valid_from)<=julianday(?) "
            "AND (valid_to IS NULL OR julianday(valid_to)>julianday(?)) "
            "ORDER BY julianday(valid_from) DESC, id DESC LIMIT 1",
            (department_id, moment, moment),
        ).fetchone()
        return str(row[0]) if row else None

    def aliases(self, department_id: int) -> list[dict]:
        return rows_dict(self.connection.execute(
            "SELECT * FROM department_aliases WHERE department_id=? ORDER BY julianday(valid_from), id",
            (department_id,),
        ).fetchall())

    def successors_of(self, department_id: int) -> list[dict]:
        return rows_dict(self.connection.execute(
            "SELECT s.*,d.name AS successor_name FROM department_successors s "
            "JOIN departments d ON d.id=s.successor_id WHERE s.predecessor_id=? "
            "ORDER BY julianday(s.effective_at), s.id",
            (department_id,),
        ).fetchall())

    def predecessors_of(self, department_id: int) -> list[dict]:
        return rows_dict(self.connection.execute(
            "SELECT s.*,d.name AS predecessor_name FROM department_successors s "
            "JOIN departments d ON d.id=s.predecessor_id WHERE s.successor_id=? "
            "ORDER BY julianday(s.effective_at), s.id",
            (department_id,),
        ).fetchall())

    def departments_as_of(self, moment: str) -> list[dict]:
        """按指定时刻还原组织视图：当时已存在且未停用的部门，名称取当时生效名。"""
        return rows_dict(self.connection.execute(
            "SELECT d.id, d.manager, d.phone, d.valid_from, d.deactivated_at, "
            "(SELECT a.name FROM department_aliases a WHERE a.department_id=d.id "
            " AND julianday(a.valid_from)<=julianday(?) "
            " AND (a.valid_to IS NULL OR julianday(a.valid_to)>julianday(?)) "
            " ORDER BY julianday(a.valid_from) DESC, a.id DESC LIMIT 1) AS name "
            "FROM departments d "
            "WHERE julianday(d.valid_from)<=julianday(?) "
            "AND (d.deactivated_at IS NULL OR julianday(d.deactivated_at)>julianday(?)) "
            "ORDER BY name",
            (moment, moment, moment, moment),
        ).fetchall())


def department_name_at_sql(department_column: str, moment_column: str) -> str:
    """生成按业务时刻解析部门当时名称的 SQL 片段（无记录时回退当前名）。"""
    return (
        f"COALESCE((SELECT a.name FROM department_aliases a WHERE a.department_id={department_column} "
        f"AND julianday(a.valid_from)<=julianday({moment_column}) "
        f"AND (a.valid_to IS NULL OR julianday(a.valid_to)>julianday({moment_column})) "
        f"ORDER BY julianday(a.valid_from) DESC, a.id DESC LIMIT 1), "
        f"(SELECT d2.name FROM departments d2 WHERE d2.id={department_column}))"
    )
