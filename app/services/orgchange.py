from __future__ import annotations

import json
import secrets
import sqlite3

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.database import transaction
from app.repositories.business import DepartmentRepository
from app.repositories.identity import SessionRepository, UserRepository
from app.repositories.orgchange import OrgChangePlanRepository, OrgChangeTargetRepository
from app.services.audit import AuditContext, AuditService
from app.services.jobs import JobService

OPEN_AFFAIR_STATUSES = ("待受理", "办理中")
OPEN_PETITION_STATUSES = ("待签收", "待分派", "办理中", "待审核", "退回重办", "复查中")

JOB_TYPE = "orgchange.apply"


def _job_key(plan_id: int) -> str:
    return f"orgchange:apply:{plan_id}"


class OrgChangeService:
    """带生效日期的组织变更：更名、停用、拆分、合并与继任迁移。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.plans = OrgChangePlanRepository(connection)
        self.targets = OrgChangeTargetRepository(connection)
        self.departments = DepartmentRepository(connection)
        self.users = UserRepository(connection)
        self.sessions = SessionRepository(connection)
        self.jobs = JobService(connection, self.clock)
        self.audit = AuditService(connection, self.clock)

    # ------------------------------------------------------------------
    # 计划创建与撤销
    # ------------------------------------------------------------------

    def create_plan(self, principal: Principal, data: dict) -> dict:
        principal.require("orgchanges.write")
        try:
            effective = from_storage(data["effective_at"])
        except ValueError as exc:
            raise ValidationError("生效时间格式无效，应为 ISO 8601") from exc
        if effective is None:
            raise ValidationError("生效时间不能为空")
        change_type = data["change_type"]
        source_ids = [int(value) for value in data["source_department_ids"]]
        sources = [self.departments.require(department_id) for department_id in source_ids]
        for department in sources:
            if not department["is_active"]:
                raise ConflictError(f"部门已停用，不能再安排变更：{department['name']}")
        overlapping = self.plans.planned_involving(set(source_ids))
        if overlapping:
            codes = [plan["code"] for plan in overlapping]
            raise ConflictError("来源部门已存在未生效的变更计划", context={"plan_codes": codes})

        new_keys = {item["key"] for item in data.get("new_departments", [])}
        for item in data.get("new_departments", []):
            if self.departments.by_name(item["name"].strip()):
                raise ConflictError(f"部门名称已存在：{item['name']}")
        if change_type == "rename":
            new_name = str(data["rename_to"]).strip()
            duplicate = self.departments.by_name(new_name)
            if duplicate:
                raise ConflictError(f"部门名称已存在：{new_name}")

        def validate_ref(ref: dict, label: str) -> None:
            if ref.get("department_id") is not None:
                target_id = int(ref["department_id"])
                if target_id in source_ids:
                    raise ValidationError(f"{label}不能是来源部门自身")
                target = self.departments.get(target_id)
                if target is None or not target["is_active"]:
                    raise NotFoundError(f"{label}不存在或已停用")
            elif ref.get("key") not in new_keys:
                raise ValidationError(f"{label}引用了未定义的新设部门：{ref.get('key')}")

        for ref in data.get("successors", []):
            validate_ref(ref, "继任部门")
        if data.get("default_todo_target"):
            validate_ref(data["default_todo_target"], "待办默认迁入部门")
        for assignment in data.get("todo_assignments", []):
            if int(assignment["from_department_id"]) not in source_ids:
                raise ValidationError("待办迁移规则的来源部门必须属于本计划")
            validate_ref(assignment["to"], "待办迁入部门")
        for mapping in data.get("staff_mappings", []):
            user = self.users.get(int(mapping["user_id"]))
            if user is None:
                raise NotFoundError(f"用户不存在：{mapping['user_id']}")
            if user["department_id"] not in source_ids:
                raise ValidationError(f"用户 {user['username']} 的主部门不在本次变更来源中")
            validate_ref(mapping["target"], "人员去向部门")

        now = to_storage(self.clock.now())
        code = data.get("code") or f"OC-{self.clock.now():%Y%m%d}-{secrets.token_hex(3).upper()}"
        if self.plans.by_code(code):
            raise ConflictError(f"计划编号已存在：{code}")
        spec = {
            "rename_to": data.get("rename_to"),
            "successors": data.get("successors", []),
            "new_departments": data.get("new_departments", []),
            "transfer_policy": data.get("transfer_policy", "auto"),
            "default_todo_target": data.get("default_todo_target"),
            "todo_assignments": data.get("todo_assignments", []),
            "staff_mappings": data.get("staff_mappings", []),
        }
        existing_successor_ids = [
            int(ref["department_id"]) for ref in spec["successors"] if ref.get("department_id") is not None
        ]
        cursor = self.connection.execute(
            "INSERT INTO org_change_plans(code,change_type,source_department_ids,target_department_ids,new_names,"
            "transfer_policy,member_transfer,spec_json,effective_at,status,note,created_by,created_by_name,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,'planned',?,?,?,?,?)",
            (
                code,
                change_type,
                json.dumps(source_ids, ensure_ascii=False),
                json.dumps(existing_successor_ids, ensure_ascii=False),
                json.dumps([item["name"] for item in spec["new_departments"]], ensure_ascii=False),
                spec["transfer_policy"],
                "{}",
                json.dumps(spec, ensure_ascii=False, sort_keys=True),
                to_storage(effective),
                data.get("note", ""),
                principal.user_id,
                principal.display_name,
                now,
                now,
            ),
        )
        plan_id = int(cursor.lastrowid)
        delay = max(0, int((effective - self.clock.now()).total_seconds()))
        self.jobs.enqueue(JOB_TYPE, _job_key(plan_id), {"plan_id": plan_id}, delay_seconds=delay)
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="orgchange.plan.create",
            resource_type="org_change_plan",
            resource_id=plan_id,
            after={"code": code, "change_type": change_type, "effective_at": to_storage(effective), "sources": source_ids},
        )
        return self.plan_detail(plan_id)

    def cancel_plan(self, principal: Principal, plan_id: int) -> dict:
        principal.require("orgchanges.write")
        plan = self.plans.require(plan_id)
        if plan["status"] != "planned":
            raise ConflictError("只有未生效的计划可以撤销")
        effective = from_storage(plan["effective_at"])
        if effective is not None and effective <= self.clock.now():
            raise ConflictError("计划已到生效时间，无法撤销，请按生效流程处理")
        now = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE org_change_plans SET status='revoked',revoked_at=?,updated_at=? WHERE id=? AND status='planned'",
            (now, now, plan_id),
        )
        self.connection.execute(
            "UPDATE background_jobs SET status='cancelled',updated_at=? WHERE deduplication_key=? AND status IN ('pending','running')",
            (now, _job_key(plan_id)),
        )
        self._log(plan_id, "cancel", {"code": plan["code"]})
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="orgchange.plan.cancel",
            resource_type="org_change_plan",
            resource_id=plan_id,
            before={"status": "planned"},
            after={"status": "revoked"},
        )
        return self.plan_detail(plan_id)

    # ------------------------------------------------------------------
    # 生效执行（幂等）
    # ------------------------------------------------------------------

    def apply_plan(self, plan_id: int, *, actor_user_id: int | None, actor_name: str) -> dict:
        """在事务内应用计划；已生效或已撤销时直接返回现状，重复执行安全。"""
        plan = self.plans.get(plan_id)
        if plan is None:
            raise NotFoundError("组织变更计划不存在")
        if plan["status"] not in {"planned", "failed"}:
            return self.plan_detail(plan_id)
        spec = plan["spec"]
        change_type = plan["change_type"]
        source_ids = [int(value) for value in plan["source_department_ids"]]
        effective = plan["effective_at"]
        now = to_storage(self.clock.now())

        key_to_id = self._create_new_departments(spec, effective, now)

        def resolve_ref(ref: dict) -> int:
            if ref.get("department_id") is not None:
                return int(ref["department_id"])
            return key_to_id[ref["key"]]

        successor_ids = [resolve_ref(ref) for ref in spec.get("successors", [])]

        if change_type == "rename":
            self._apply_rename(source_ids[0], str(spec["rename_to"]).strip(), effective, now)
        else:
            placeholders = ",".join("?" for _ in source_ids)
            self.connection.execute(
                f"UPDATE departments SET is_active=0,deactivated_at=?,updated_at=? WHERE id IN ({placeholders})",
                (effective, now, *source_ids),
            )
            # 关闭旧部门在生效时刻仍开放的名称时间段，沿革止于停用
            for source_id in source_ids:
                self.connection.execute(
                    "UPDATE department_aliases SET valid_to=? WHERE department_id=? "
                    "AND (valid_to IS NULL OR julianday(valid_to)>julianday(?))",
                    (effective, source_id, effective),
                )
        for source_id in source_ids:
            for successor_id in successor_ids:
                self.connection.execute(
                    "INSERT OR IGNORE INTO department_successors(predecessor_id,successor_id,change_type,effective_at,plan_id,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (source_id, successor_id, change_type, effective, plan_id, now),
                )

        transferred: dict[str, int] = {}
        if change_type != "rename":
            transferred = self._relocate_staff(plan_id, source_ids, successor_ids, spec, resolve_ref, effective, now)
            self._relocate_todos(plan_id, source_ids, successor_ids, spec, resolve_ref, effective, now)

        open_conflicts = self.plans.open_conflict_count(plan_id)
        final_status = "applied" if open_conflicts == 0 else "conflict"
        self.connection.execute(
            "UPDATE org_change_plans SET status=?,applied_at=?,target_department_ids=?,member_transfer=?,updated_at=? WHERE id=?",
            (
                final_status,
                now,
                json.dumps(successor_ids, ensure_ascii=False),
                json.dumps(transferred, ensure_ascii=False, sort_keys=True),
                now,
                plan_id,
            ),
        )
        self.connection.execute(
            "UPDATE background_jobs SET status='completed',result_json=?,locked_at=NULL,locked_by=NULL,updated_at=? "
            "WHERE deduplication_key=? AND status IN ('pending','running')",
            (json.dumps({"plan_id": plan_id, "status": final_status}, ensure_ascii=False), now, _job_key(plan_id)),
        )
        self._log(plan_id, "apply", {"status": final_status, "open_conflicts": open_conflicts, "successors": successor_ids})
        self.audit.record(
            AuditContext(actor_user_id, actor_name),
            action="orgchange.plan.apply",
            resource_type="org_change_plan",
            resource_id=plan_id,
            after={"status": final_status, "successor_ids": successor_ids, "open_conflicts": open_conflicts},
        )
        return self.plan_detail(plan_id)

    def mark_failed(self, plan_id: int, message: str) -> None:
        now = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE org_change_plans SET status='failed',note=?,updated_at=? WHERE id=? AND status IN ('planned','failed')",
            (message[:500], now, plan_id),
        )
        self._log(plan_id, "failed", {"message": message[:500]})

    # ------------------------------------------------------------------
    # 归属冲突处理
    # ------------------------------------------------------------------

    def resolve_conflict(self, principal: Principal, target_id: int, department_id: int, remark: str = "") -> dict:
        principal.require("orgchanges.write")
        target = self.targets.get(target_id)
        if target is None:
            raise NotFoundError("归属确认项不存在")
        if target["status"] != "pending":
            raise ConflictError("该归属确认项已处理")
        plan = self.plans.require(target["plan_id"])
        if plan["status"] not in {"conflict", "applied"}:
            raise ConflictError("计划尚未生效，不能处理归属")
        department = self.departments.get(department_id)
        if department is None or not department["is_active"]:
            raise NotFoundError("迁入部门不存在或已停用")
        now = to_storage(self.clock.now())
        resource_type = target["resource_type"]
        resource_id = int(target["resource_id"])
        source_id = int(target["current_department_id"])
        if resource_type == "affair":
            self._resolve_affair(resource_id, source_id, department_id, now, principal)
        elif resource_type == "petition":
            self._resolve_petition(resource_id, source_id, department_id, now, principal)
        else:
            self._resolve_member(resource_id, source_id, department_id, plan["effective_at"], now)
        cursor = self.connection.execute(
            "UPDATE org_change_targets SET status='resolved',resolution_department_id=?,resolved_at=? WHERE id=? AND status='pending'",
            (department_id, now, target_id),
        )
        if cursor.rowcount != 1:
            raise ConflictError("该归属确认项已处理")
        remaining = self.plans.open_conflict_count(plan["id"])
        plan_status = plan["status"]
        if remaining == 0 and plan_status == "conflict":
            self.connection.execute(
                "UPDATE org_change_plans SET status='applied',updated_at=? WHERE id=? AND status='conflict'",
                (now, plan["id"]),
            )
            plan_status = "applied"
            self._log(plan["id"], "completed", {"resolved_target": target_id})
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="orgchange.conflict.resolve",
            resource_type="org_change_target",
            resource_id=target_id,
            after={"resource_type": resource_type, "resource_id": resource_id, "department_id": department_id, "remark": remark},
        )
        return {"target": self.targets.get(target_id), "plan_status": plan_status, "open_conflicts": remaining}

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def plan_detail(self, plan_id: int) -> dict:
        plan = self.plans.require(plan_id)
        plan["targets"] = self.targets.list_for_plan(plan_id)
        plan["open_conflicts"] = self.plans.open_conflict_count(plan_id)
        return plan

    # ------------------------------------------------------------------
    # 内部：结构变更
    # ------------------------------------------------------------------

    def _create_new_departments(self, spec: dict, effective: str, now: str) -> dict[str, int]:
        key_to_id: dict[str, int] = {}
        for item in spec.get("new_departments", []):
            name = item["name"].strip()
            if self.departments.by_name(name):
                raise ConflictError(f"部门名称已存在：{name}")
            cursor = self.connection.execute(
                "INSERT INTO departments(name,manager,phone,is_active,valid_from,created_at,updated_at) VALUES(?,?,?,1,?,?,?)",
                (name, item["manager"].strip(), item["phone"].strip(), effective, now, now),
            )
            new_id = int(cursor.lastrowid)
            self.connection.execute(
                "INSERT INTO department_aliases(department_id,name,valid_from,created_at) VALUES(?,?,?,?)",
                (new_id, name, effective, now),
            )
            key_to_id[item["key"]] = new_id
        return key_to_id

    def _apply_rename(self, department_id: int, new_name: str, effective: str, now: str) -> None:
        duplicate = self.departments.by_name(new_name)
        if duplicate and duplicate["id"] != department_id:
            raise ConflictError(f"部门名称已存在：{new_name}")
        self.connection.execute(
            "UPDATE department_aliases SET valid_to=? WHERE department_id=? AND valid_to IS NULL",
            (effective, department_id),
        )
        self.connection.execute(
            "INSERT INTO department_aliases(department_id,name,valid_from,created_at) VALUES(?,?,?,?)",
            (department_id, new_name, effective, now),
        )
        self.connection.execute(
            "UPDATE departments SET name=?,updated_at=? WHERE id=?", (new_name, now, department_id)
        )

    # ------------------------------------------------------------------
    # 内部：人员切换
    # ------------------------------------------------------------------

    def _relocate_staff(
        self,
        plan_id: int,
        source_ids: list[int],
        successor_ids: list[int],
        spec: dict,
        resolve_ref,
        effective: str,
        now: str,
    ) -> dict[str, int]:
        staff_map = {int(m["user_id"]): resolve_ref(m["target"]) for m in spec.get("staff_mappings", [])}
        placeholders = ",".join("?" for _ in source_ids)
        primary_users = self.connection.execute(
            f"SELECT id,department_id FROM users WHERE department_id IN ({placeholders})", tuple(source_ids)
        ).fetchall()
        transferred: dict[str, int] = {}
        handled: set[int] = set()
        auto_single = len(successor_ids) == 1 and spec.get("transfer_policy", "auto") == "auto"
        for row in primary_users:
            user_id = int(row["id"])
            handled.add(user_id)
            target_id = staff_map.get(user_id)
            if target_id is None and auto_single:
                target_id = successor_ids[0]
            if target_id is None:
                self._end_source_memberships(user_id, source_ids, effective)
                self.connection.execute(
                    "UPDATE users SET department_id=NULL,updated_at=? WHERE id=?", (now, user_id)
                )
                self._add_conflict(plan_id, "member", user_id, int(row["department_id"]), successor_ids, now)
                self.sessions.revoke_user_sessions(user_id, now, "org_change_transfer")
                continue
            self._transfer_user(user_id, source_ids, target_id, effective, now)
            transferred[str(user_id)] = target_id
        # 非主部门任职：生效时一并结束
        secondary = self.connection.execute(
            f"SELECT id,user_id FROM department_memberships WHERE department_id IN ({placeholders}) "
            "AND (ends_at IS NULL OR julianday(ends_at)>julianday(?))",
            (*source_ids, effective),
        ).fetchall()
        for row in secondary:
            if int(row["user_id"]) in handled:
                continue
            self.connection.execute(
                "UPDATE department_memberships SET ends_at=?,is_primary=0 WHERE id=? "
                "AND (ends_at IS NULL OR julianday(ends_at)>julianday(?))",
                (effective, int(row["id"]), effective),
            )
        return transferred

    def _transfer_user(self, user_id: int, source_ids: list[int], target_id: int, effective: str, now: str) -> None:
        self._end_source_memberships(user_id, source_ids, effective)
        self.connection.execute(
            "UPDATE department_memberships SET is_primary=0 WHERE user_id=? AND (ends_at IS NULL OR ends_at>?)",
            (user_id, now),
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO department_memberships(user_id,department_id,title,is_primary,starts_at,ends_at,created_at) "
            "VALUES(?,?,?,1,?,NULL,?)",
            (user_id, target_id, "", effective, now),
        )
        self.connection.execute(
            "UPDATE users SET department_id=?,updated_at=? WHERE id=?", (target_id, now, user_id)
        )
        self.sessions.revoke_user_sessions(user_id, now, "org_change_transfer")

    def _end_source_memberships(self, user_id: int, source_ids: list[int], effective: str) -> None:
        placeholders = ",".join("?" for _ in source_ids)
        self.connection.execute(
            f"UPDATE department_memberships SET ends_at=?,is_primary=0 WHERE user_id=? AND department_id IN ({placeholders}) "
            "AND (ends_at IS NULL OR julianday(ends_at)>julianday(?))",
            (effective, user_id, *source_ids, effective),
        )

    # ------------------------------------------------------------------
    # 内部：待办迁移
    # ------------------------------------------------------------------

    def _relocate_todos(
        self,
        plan_id: int,
        source_ids: list[int],
        successor_ids: list[int],
        spec: dict,
        resolve_ref,
        effective: str,
        now: str,
    ) -> None:
        todo_map = {
            int(item["from_department_id"]): resolve_ref(item["to"]) for item in spec.get("todo_assignments", [])
        }
        default_target = resolve_ref(spec["default_todo_target"]) if spec.get("default_todo_target") else None
        auto = spec.get("transfer_policy", "auto") == "auto"
        for source_id in source_ids:
            # 明确规则优先：显式去向与默认去向始终生效；无规则时 auto+唯一继任才自动迁移
            target_id = todo_map.get(source_id) or default_target
            if target_id is None and auto and len(successor_ids) == 1:
                target_id = successor_ids[0]
            if target_id is not None:
                self._move_open_affairs(source_id, target_id, now)
                self._move_open_petitions(source_id, target_id, now)
            else:
                self._flag_open_todos(plan_id, source_id, successor_ids, now)

    def _move_open_affairs(self, source_id: int, target_id: int, now: str) -> None:
        placeholders = ",".join("?" for _ in OPEN_AFFAIR_STATUSES)
        rows = self.connection.execute(
            f"SELECT id FROM affairs WHERE department_id=? AND status IN ({placeholders})",
            (source_id, *OPEN_AFFAIR_STATUSES),
        ).fetchall()
        for row in rows:
            self.connection.execute(
                "UPDATE affairs SET department_id=?,department_assigned_at=?,updated_at=? WHERE id=?",
                (target_id, now, now, int(row["id"])),
            )
            self.connection.execute(
                "INSERT INTO affair_flow_records(affair_id,action,operator,remark,created_at) VALUES(?,?,?,?,?)",
                (int(row["id"]), "组织调整移交", "system", f"由部门 {source_id} 移交至部门 {target_id}", now),
            )

    def _move_open_petitions(self, source_id: int, target_id: int, now: str) -> None:
        placeholders = ",".join("?" for _ in OPEN_PETITION_STATUSES)
        rows = self.connection.execute(
            f"SELECT id FROM petitions WHERE department_id=? AND status IN ({placeholders})",
            (source_id, *OPEN_PETITION_STATUSES),
        ).fetchall()
        for row in rows:
            self.connection.execute(
                "UPDATE petitions SET department_id=?,department_assigned_at=?,updated_at=? WHERE id=?",
                (target_id, now, now, int(row["id"])),
            )
            self.connection.execute(
                "INSERT INTO petition_flow_records(petition_id,action,operator,remark,created_at) VALUES(?,?,?,?,?)",
                (int(row["id"]), "组织调整移交", "system", f"由部门 {source_id} 移交至部门 {target_id}", now),
            )

    def _flag_open_todos(self, plan_id: int, source_id: int, candidate_ids: list[int], now: str) -> None:
        affair_placeholders = ",".join("?" for _ in OPEN_AFFAIR_STATUSES)
        affairs = self.connection.execute(
            f"SELECT id FROM affairs WHERE department_id=? AND status IN ({affair_placeholders})",
            (source_id, *OPEN_AFFAIR_STATUSES),
        ).fetchall()
        for row in affairs:
            self._add_conflict(plan_id, "affair", int(row["id"]), source_id, candidate_ids, now)
        petition_placeholders = ",".join("?" for _ in OPEN_PETITION_STATUSES)
        petitions = self.connection.execute(
            f"SELECT id FROM petitions WHERE department_id=? AND status IN ({petition_placeholders})",
            (source_id, *OPEN_PETITION_STATUSES),
        ).fetchall()
        for row in petitions:
            self._add_conflict(plan_id, "petition", int(row["id"]), source_id, candidate_ids, now)

    def _add_conflict(
        self, plan_id: int, resource_type: str, resource_id: int, source_id: int, candidate_ids: list[int], now: str
    ) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO org_change_targets(plan_id,resource_type,resource_id,current_department_id,"
            "candidate_department_ids,status,created_at) VALUES(?,?,?,?,?,'pending',?)",
            (plan_id, resource_type, resource_id, source_id, json.dumps(candidate_ids, ensure_ascii=False), now),
        )

    # ------------------------------------------------------------------
    # 内部：冲突落库
    # ------------------------------------------------------------------

    def _resolve_affair(self, affair_id: int, source_id: int, department_id: int, now: str, principal: Principal) -> None:
        row = self.connection.execute("SELECT department_id,status FROM affairs WHERE id=?", (affair_id,)).fetchone()
        if row is None:
            raise NotFoundError("事务不存在")
        if int(row["department_id"] or 0) == department_id:
            return  # 已在目标部门，幂等确认
        if int(row["department_id"] or 0) != source_id or row["status"] not in OPEN_AFFAIR_STATUSES:
            raise ConflictError("该事务归属已变化，请刷新待确认列表")
        self.connection.execute(
            "UPDATE affairs SET department_id=?,department_assigned_at=?,updated_at=? WHERE id=?",
            (department_id, now, now, affair_id),
        )
        self.connection.execute(
            "INSERT INTO affair_flow_records(affair_id,action,operator,remark,created_at) VALUES(?,?,?,?,?)",
            (affair_id, "组织调整归属确认", principal.display_name, f"确认迁入部门 {department_id}", now),
        )

    def _resolve_petition(self, petition_id: int, source_id: int, department_id: int, now: str, principal: Principal) -> None:
        row = self.connection.execute("SELECT department_id,status FROM petitions WHERE id=?", (petition_id,)).fetchone()
        if row is None:
            raise NotFoundError("信访件不存在")
        if int(row["department_id"] or 0) == department_id:
            return  # 已在目标部门，幂等确认
        if int(row["department_id"] or 0) != source_id or row["status"] not in OPEN_PETITION_STATUSES:
            raise ConflictError("该信访件归属已变化，请刷新待确认列表")
        self.connection.execute(
            "UPDATE petitions SET department_id=?,department_assigned_at=?,updated_at=? WHERE id=?",
            (department_id, now, now, petition_id),
        )
        self.connection.execute(
            "INSERT INTO petition_flow_records(petition_id,action,operator,remark,created_at) VALUES(?,?,?,?,?)",
            (petition_id, "组织调整归属确认", principal.display_name, f"确认迁入部门 {department_id}", now),
        )

    def _resolve_member(self, user_id: int, source_id: int, department_id: int, effective: str, now: str) -> None:
        user = self.users.get(user_id)
        if user is None:
            raise NotFoundError("用户不存在")
        if user["department_id"] is not None:
            # 计划外已被人工安置：目标一致则幂等确认，否则提示先刷新
            if int(user["department_id"]) == department_id:
                return
            raise ConflictError("该用户已分配主部门，请刷新待确认列表")
        self._transfer_user(user_id, [source_id], department_id, effective, now)

    def _log(self, plan_id: int, action: str, detail: dict) -> None:
        self.connection.execute(
            "INSERT INTO org_change_log(plan_id,action,detail_json,created_at) VALUES(?,?,?,?)",
            (plan_id, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), to_storage(self.clock.now())),
        )


def apply_due_plans(clock: Clock | None = None) -> list[dict]:
    """应用所有到期的计划：每个计划独立事务，失败标记为 failed，重复执行幂等。"""
    actual_clock = clock or SystemClock()
    moment = to_storage(actual_clock.now())
    with transaction(immediate=True) as connection:
        due_ids = [plan["id"] for plan in OrgChangePlanRepository(connection).due_plans(moment)]
    results: list[dict] = []
    for plan_id in due_ids:
        try:
            with transaction(immediate=True) as connection:
                service = OrgChangeService(connection, actual_clock)
                results.append(service.apply_plan(plan_id, actor_user_id=None, actor_name="system"))
        except Exception as exc:  # noqa: BLE001 - 单个计划失败不阻塞其余计划
            with transaction(immediate=True) as connection:
                OrgChangeService(connection, actual_clock).mark_failed(plan_id, str(exc))
            results.append({"plan_id": plan_id, "status": "failed", "error": str(exc)})
    return results
