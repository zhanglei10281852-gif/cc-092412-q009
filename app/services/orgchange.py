from __future__ import annotations

import json
import sqlite3

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.repositories.business import DepartmentRepository, PetitionRepository
from app.repositories.identity import SessionRepository, UserRepository
from app.repositories.orgchange import OrgChangeRepository, plan_source_departments
from app.services.audit import AuditContext, AuditService

AFFAIR_CATEGORIES = {"户籍", "社保", "医保", "低保", "建房", "计生", "其他"}
PETITION_TYPES = {"投诉举报", "意见建议", "求助咨询", "信息公开申请"}
MEMBER_POLICIES = {"auto", "keep", "manual"}
CHANGE_TYPES = {"rename", "deactivate", "merge", "split"}


class OrgChangeService:
    """带生效日期的组织变更：更名、停用、合并、拆分以及待办与任职的迁移。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repo = OrgChangeRepository(connection)
        self.departments = DepartmentRepository(connection)
        self.users = UserRepository(connection)
        self.sessions = SessionRepository(connection)
        self.petitions = PetitionRepository(connection)
        self.audit = AuditService(connection, self.clock)

    # ------------------------------------------------------------------ 查询

    def list_plans(self, principal: Principal, *, status: str | None, change_type: str | None, limit: int, offset: int) -> tuple[int, list[dict]]:
        principal.require("departments.read")
        total = self.repo.count_plans(status=status, change_type=change_type)
        return total, self.repo.list_plans(status=status, change_type=change_type, limit=limit, offset=offset)

    def detail(self, plan_id: int) -> dict:
        plan = self.repo.require_plan(plan_id)
        plan["items"] = self.repo.items(plan_id)
        plan["successions"] = self.repo.successions(plan_id)
        return plan

    def get_plan(self, principal: Principal, plan_id: int) -> dict:
        principal.require("departments.read")
        return self.detail(plan_id)

    def department_timeline(self, principal: Principal, department_id: int) -> dict:
        principal.require("departments.read")
        department = self.departments.require(department_id)
        return {
            "department": department,
            "snapshots": self.repo.snapshots(department_id),
            "successions": self.repo.successions_for_department(department_id),
        }

    # ------------------------------------------------------------------ 计划生命周期

    def create_plan(self, principal: Principal, data: dict) -> dict:
        principal.require("departments.write")
        change_type = data.get("change_type")
        if change_type not in CHANGE_TYPES:
            raise ValidationError("不支持的变更类型")
        effective = from_storage(data.get("effective_at"))
        if effective is None:
            raise ValidationError("生效时间无效")
        effective_at = to_storage(effective)
        payload = self._validate_payload(change_type, data.get("payload") or {})
        sources = plan_source_departments({"change_type": change_type, "payload": payload})
        for department_id in sources:
            department = self.departments.require(department_id)
            created_at = from_storage(department["created_at"])
            if created_at is not None and effective < created_at:
                raise ValidationError("生效时间不能早于来源部门的创建时间")
        overlapping = self.repo.planned_plans_touching(sources)
        if overlapping:
            raise ConflictError(f"来源部门已存在待生效的变更计划：#{overlapping[0]['id']}")
        now = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "INSERT INTO org_changes(change_type,effective_at,status,payload_json,summary,created_by,created_by_name,created_at,updated_at) "
            "VALUES(?,?, 'planned', ?,?,?,?,?,?)",
            (
                change_type,
                effective_at,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                (data.get("summary") or "").strip(),
                principal.user_id,
                principal.display_name,
                now,
                now,
            ),
        )
        plan_id = int(cursor.lastrowid)
        plan = self.detail(plan_id)
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="org_change.create",
            resource_type="org_change",
            resource_id=plan_id,
            after=plan,
        )
        return plan

    def revoke(self, principal: Principal, plan_id: int) -> dict:
        principal.require("departments.write")
        plan = self.repo.require_plan(plan_id)
        if plan["status"] != "planned":
            raise ConflictError("仅待生效的计划可以撤销")
        effective = from_storage(plan["effective_at"])
        if effective is not None and effective <= self.clock.now():
            raise ConflictError("已过生效时间，计划不能撤销")
        now = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE org_changes SET status='revoked',revoked_at=?,updated_at=? WHERE id=?",
            (now, now, plan_id),
        )
        after = self.detail(plan_id)
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="org_change.revoke",
            resource_type="org_change",
            resource_id=plan_id,
            before={"status": plan["status"]},
            after={"status": after["status"]},
        )
        return after

    def apply(self, principal: Principal, plan_id: int) -> dict:
        principal.require("departments.write")
        plan = self.repo.require_plan(plan_id)
        if plan["status"] == "applied":
            return {"plan": self.detail(plan_id), "result": {"already_applied": True}}
        if plan["status"] == "revoked":
            raise ConflictError("计划已撤销，不能执行")
        effective = from_storage(plan["effective_at"])
        if plan["status"] == "planned" and effective is not None and effective > self.clock.now():
            raise ConflictError("未到生效时间，不能执行计划")
        stats = self._execute(plan, AuditContext(principal.user_id, principal.display_name))
        return {"plan": self.detail(plan_id), "result": stats}

    def apply_due(self, *, actor_name: str = "system.scheduler") -> list[dict]:
        """执行所有已到生效时间的计划；每个计划在独立保存点中执行，单个失败不影响其余。"""
        now = to_storage(self.clock.now())
        results: list[dict] = []
        for plan in self.repo.due_plans(now):
            savepoint = f"org_change_{plan['id']}"
            self.connection.execute(f"SAVEPOINT {savepoint}")
            try:
                stats = self._execute(plan, AuditContext(None, actor_name))
            except Exception as exc:  # noqa: BLE001 - 单计划失败必须可回滚且不影响其他计划
                self.connection.execute(f"ROLLBACK TO {savepoint}")
                self.connection.execute(f"RELEASE {savepoint}")
                results.append({"plan_id": plan["id"], "status": "error", "error": str(exc)})
            else:
                self.connection.execute(f"RELEASE {savepoint}")
                results.append({"plan_id": plan["id"], "status": stats["status"], "conflict_count": stats["conflict_count"]})
        return results

    def resolve_item(self, principal: Principal, plan_id: int, item_id: int, target_department_id: int, resolution: str) -> dict:
        principal.require("departments.write")
        plan = self.repo.require_plan(plan_id)
        if plan["status"] != "conflict":
            raise ConflictError("仅存在归属冲突的计划需要人工确认")
        item = self.repo.item(item_id)
        if item is None or item["change_id"] != plan_id:
            raise NotFoundError("变更条目不存在")
        if item["status"] != "manual":
            raise ConflictError("该条目已处理，无需人工确认")
        target = self.departments.get(target_department_id)
        if target is None or not target["is_active"]:
            raise NotFoundError("目标部门不存在或已停用")
        now = to_storage(self.clock.now())
        table = "affairs" if item["item_type"] == "affair" else "petitions"
        self.connection.execute(
            f"UPDATE {table} SET department_id=?,updated_at=? WHERE id=?",
            (target_department_id, now, item["item_id"]),
        )
        if item["item_type"] == "petition":
            self.petitions.append_flow(
                item["item_id"],
                "组织调整迁移",
                principal.display_name,
                f"组织变更#{plan_id}：人工确认归属 {target['name']}",
                now,
            )
        self.connection.execute(
            "UPDATE org_change_items SET status='resolved',target_department_id=?,resolution=?,resolved_by=?,resolved_at=?,updated_at=? WHERE id=?",
            (target_department_id, resolution.strip() or "人工确认归属", principal.display_name, now, now, item_id),
        )
        remaining = self.repo.count_items(plan_id, ("pending", "manual"))
        if remaining == 0:
            self.connection.execute(
                "UPDATE org_changes SET status='applied',conflict_count=0,applied_at=?,updated_at=? WHERE id=?",
                (now, now, plan_id),
            )
        else:
            self.connection.execute(
                "UPDATE org_changes SET conflict_count=?,updated_at=? WHERE id=?",
                (remaining, now, plan_id),
            )
        after = self.detail(plan_id)
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="org_change.resolve_item",
            resource_type="org_change_item",
            resource_id=item_id,
            before={"status": item["status"], "target_department_id": item["target_department_id"]},
            after={"status": "resolved", "target_department_id": target_department_id},
            metadata={"plan_id": plan_id, "remaining_conflicts": remaining},
        )
        return after

    # ------------------------------------------------------------------ 执行

    def _execute(self, plan: dict, context: AuditContext) -> dict:
        """在调用方的事务内执行计划；所有步骤幂等，可安全重复执行。"""
        now = to_storage(self.clock.now())
        payload = plan["payload"]
        change_type = plan["change_type"]
        stats: dict = {
            "migrated_items": 0,
            "manual_items": 0,
            "transferred_memberships": 0,
            "revoked_sessions": 0,
            "new_department_ids": [],
        }
        source_ids: list[int] = []
        default_target: int | None = None
        if change_type == "rename":
            self._apply_rename(plan, payload, now)
        elif change_type == "deactivate":
            source_ids, default_target = self._apply_deactivate(plan, payload, now)
        elif change_type == "merge":
            source_ids, default_target = self._apply_merge(plan, payload, now)
        elif change_type == "split":
            source_ids, stats["new_department_ids"] = self._apply_split(plan, payload, now)
        if source_ids:
            self._migrate_items(plan, payload, source_ids, default_target, stats["new_department_ids"], context, now, stats)
            self._transfer_memberships(plan, payload, source_ids, default_target, stats["new_department_ids"], now, stats)
        manual = self.repo.count_items(plan["id"], ("pending", "manual"))
        status = "conflict" if manual else "applied"
        self.connection.execute(
            "UPDATE org_changes SET status=?,conflict_count=?,applied_at=?,updated_at=? WHERE id=?",
            (status, manual, now if status == "applied" else None, now, plan["id"]),
        )
        stats["status"] = status
        stats["conflict_count"] = manual
        self.audit.record(
            context,
            action="org_change.apply",
            resource_type="org_change",
            resource_id=plan["id"],
            before={"status": plan["status"]},
            after={"status": status, "conflict_count": manual},
            metadata={key: value for key, value in stats.items() if key != "status"},
        )
        return stats

    def _apply_rename(self, plan: dict, payload: dict, now: str) -> None:
        department = self.departments.require(payload["department_id"])
        self._ensure_baseline_snapshot(department, plan)
        new_name = payload["new_name"]
        duplicate = self.departments.by_name(new_name)
        if duplicate and duplicate["id"] != department["id"]:
            raise ConflictError("目标名称已被其他部门使用")
        self.connection.execute(
            "UPDATE departments SET name=?,manager=?,phone=?,updated_at=? WHERE id=?",
            (
                new_name,
                payload.get("manager") or department["manager"],
                payload.get("phone") or department["phone"],
                now,
                department["id"],
            ),
        )
        self._record_state_snapshot(department["id"], plan, now)
        self._record_succession(plan, department["id"], department["id"], "rename", now)

    def _apply_deactivate(self, plan: dict, payload: dict, now: str) -> tuple[list[int], int | None]:
        department = self.departments.require(payload["department_id"])
        self._ensure_baseline_snapshot(department, plan)
        self.connection.execute("UPDATE departments SET is_active=0,updated_at=? WHERE id=?", (now, department["id"]))
        self._record_state_snapshot(department["id"], plan, now)
        successor = payload.get("successor_department_id")
        if successor is not None:
            self._record_succession(plan, department["id"], successor, "deactivate", now)
        return [department["id"]], successor

    def _apply_merge(self, plan: dict, payload: dict, now: str) -> tuple[list[int], int | None]:
        target = int(payload["target_department_id"])
        source_ids: list[int] = []
        for source_id in payload["source_department_ids"]:
            department = self.departments.require(int(source_id))
            self._ensure_baseline_snapshot(department, plan)
            self.connection.execute("UPDATE departments SET is_active=0,updated_at=? WHERE id=?", (now, department["id"]))
            self._record_state_snapshot(department["id"], plan, now)
            self._record_succession(plan, department["id"], target, "merge", now)
            source_ids.append(department["id"])
        return source_ids, target

    def _apply_split(self, plan: dict, payload: dict, now: str) -> tuple[list[int], list[int]]:
        source = self.departments.require(payload["source_department_id"])
        self._ensure_baseline_snapshot(source, plan)
        self.connection.execute("UPDATE departments SET is_active=0,updated_at=? WHERE id=?", (now, source["id"]))
        self._record_state_snapshot(source["id"], plan, now)
        existing = {
            row["successor_name"]: row["successor_id"]
            for row in self.repo.successions(plan["id"])
            if row["relation"] == "split"
        }
        new_ids: list[int] = []
        for spec in payload["new_departments"]:
            if spec["name"] in existing:
                new_ids.append(existing[spec["name"]])
                continue
            try:
                cursor = self.connection.execute(
                    "INSERT INTO departments(name,manager,phone,is_active,created_at,updated_at) VALUES(?,?,?,1,?,?)",
                    (spec["name"], spec["manager"], spec["phone"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"新部门名称已被占用：{spec['name']}") from exc
            department_id = int(cursor.lastrowid)
            self.connection.execute(
                "INSERT INTO department_snapshots(department_id,name,manager,phone,is_active,effective_at,change_id,created_at) VALUES(?,?,?,?,1,?,?,?)",
                (department_id, spec["name"], spec["manager"], spec["phone"], plan["effective_at"], plan["id"], now),
            )
            self._record_succession(plan, source["id"], department_id, "split", now)
            new_ids.append(department_id)
        return [source["id"]], new_ids

    def _migrate_items(
        self,
        plan: dict,
        payload: dict,
        source_ids: list[int],
        default_target: int | None,
        new_ids: list[int],
        context: AuditContext,
        now: str,
        stats: dict,
    ) -> None:
        rules = payload.get("item_rules") or []
        for source_id in source_ids:
            source = self.departments.require(source_id)
            for affair in self.repo.open_affairs(source_id):
                if self.repo.item_for(plan["id"], "affair", affair["id"]):
                    continue
                routed = self._route_rule(rules, "affair_category", affair["category"], new_ids)
                target = routed if routed is not None else default_target
                self._migrate_item(plan, "affair", affair["id"], source, target, context, now, stats)
            for petition in self.repo.open_petitions(source_id):
                if self.repo.item_for(plan["id"], "petition", petition["id"]):
                    continue
                routed = self._route_rule(rules, "petition_type", petition["type"], new_ids)
                target = routed if routed is not None else default_target
                self._migrate_item(plan, "petition", petition["id"], source, target, context, now, stats)

    def _migrate_item(
        self,
        plan: dict,
        item_type: str,
        item_id: int,
        source: dict,
        target_id: int | None,
        context: AuditContext,
        now: str,
        stats: dict,
    ) -> None:
        if target_id is not None:
            table = "affairs" if item_type == "affair" else "petitions"
            self.connection.execute(
                f"UPDATE {table} SET department_id=?,updated_at=? WHERE id=?",
                (target_id, now, item_id),
            )
            target = self.departments.require(target_id)
            if item_type == "petition":
                self.petitions.append_flow(
                    item_id,
                    "组织调整迁移",
                    context.actor_name,
                    f"组织变更#{plan['id']}：{source['name']} → {target['name']}",
                    now,
                )
            status = "migrated"
            stats["migrated_items"] += 1
        else:
            status = "manual"
            stats["manual_items"] += 1
        self.connection.execute(
            "INSERT INTO org_change_items(change_id,item_type,item_id,source_department_id,target_department_id,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (plan["id"], item_type, item_id, source["id"], target_id, status, now, now),
        )

    def _transfer_memberships(
        self,
        plan: dict,
        payload: dict,
        source_ids: list[int],
        default_target: int | None,
        new_ids: list[int],
        now: str,
        stats: dict,
    ) -> None:
        policy = payload.get("member_policy") or "manual"
        assignments = {int(key): int(value) for key, value in (payload.get("member_assignments") or {}).items()}
        effective = plan["effective_at"]
        affected_users: set[int] = set()
        for source_id in source_ids:
            for membership in self.repo.active_memberships_at(source_id, effective):
                target: int | None = None
                explicit = assignments.get(membership["user_id"])
                if explicit is not None:
                    target = new_ids[explicit] if plan["change_type"] == "split" and new_ids else explicit
                elif policy == "auto":
                    target = default_target
                if target is None:
                    continue
                self.connection.execute(
                    "UPDATE department_memberships SET ends_at=?,is_primary=0 WHERE id=?",
                    (effective, membership["id"]),
                )
                self.connection.execute(
                    "INSERT OR IGNORE INTO department_memberships(user_id,department_id,title,is_primary,starts_at,ends_at,created_at) "
                    "VALUES(?,?,?,?,?,NULL,?)",
                    (membership["user_id"], target, membership["title"], membership["is_primary"], effective, now),
                )
                if membership["is_primary"]:
                    self.connection.execute(
                        "UPDATE users SET department_id=?,updated_at=? WHERE id=? AND department_id=?",
                        (target, now, membership["user_id"], source_id),
                    )
                affected_users.add(membership["user_id"])
                stats["transferred_memberships"] += 1
        for user_id in affected_users:
            stats["revoked_sessions"] += self.sessions.revoke_user_sessions(user_id, now, "org_change")

    # ------------------------------------------------------------------ 快照与继任

    def _ensure_baseline_snapshot(self, department: dict, plan: dict) -> None:
        """若部门在生效点之前没有任何快照，按变更前属性在部门创建时间补一条基线。"""
        earlier = self.connection.execute(
            "SELECT 1 FROM department_snapshots WHERE department_id=? AND effective_at<? LIMIT 1",
            (department["id"], plan["effective_at"]),
        ).fetchone()
        if earlier:
            return
        self.connection.execute(
            "INSERT OR IGNORE INTO department_snapshots(department_id,name,manager,phone,is_active,effective_at,change_id,created_at) "
            "VALUES(?,?,?,?,?,?,NULL,?)",
            (
                department["id"],
                department["name"],
                department["manager"],
                department["phone"],
                department["is_active"],
                department["created_at"],
                to_storage(self.clock.now()),
            ),
        )

    def _record_state_snapshot(self, department_id: int, plan: dict, now: str) -> None:
        department = self.departments.require(department_id)
        self.connection.execute(
            "INSERT INTO department_snapshots(department_id,name,manager,phone,is_active,effective_at,change_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(department_id,effective_at) DO UPDATE SET name=excluded.name,manager=excluded.manager,"
            "phone=excluded.phone,is_active=excluded.is_active,change_id=excluded.change_id",
            (
                department_id,
                department["name"],
                department["manager"],
                department["phone"],
                department["is_active"],
                plan["effective_at"],
                plan["id"],
                now,
            ),
        )

    def _record_succession(self, plan: dict, predecessor_id: int, successor_id: int, relation: str, now: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO department_successions(change_id,predecessor_id,successor_id,relation,effective_at,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (plan["id"], predecessor_id, successor_id, relation, plan["effective_at"], now),
        )

    @staticmethod
    def _route_rule(rules: list[dict], kind: str, value: str, new_ids: list[int]) -> int | None:
        """按明确规则匹配目标部门。拆分计划里规则存新部门序号，其余存真实部门 id。"""
        for rule in rules:
            if rule["kind"] == kind and rule["value"] == value:
                target = int(rule["target"])
                if new_ids:
                    return new_ids[target] if 0 <= target < len(new_ids) else None
                return target
        return None

    # ------------------------------------------------------------------ 负载校验

    def _validate_payload(self, change_type: str, payload: dict) -> dict:
        if change_type == "rename":
            return self._validate_rename(payload)
        if change_type == "deactivate":
            return self._validate_deactivate(payload)
        if change_type == "merge":
            return self._validate_merge(payload)
        return self._validate_split(payload)

    def _require_active_department(self, raw_id: object, label: str) -> dict:
        try:
            department_id = int(raw_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValidationError(f"{label}无效") from None
        department = self.departments.get(department_id)
        if department is None or not department["is_active"]:
            raise NotFoundError(f"{label}不存在或已停用")
        return department

    def _validate_rename(self, payload: dict) -> dict:
        department = self._require_active_department(payload.get("department_id"), "部门")
        new_name = str(payload.get("new_name") or "").strip()
        if not new_name:
            raise ValidationError("新名称不能为空")
        if new_name == department["name"]:
            raise ValidationError("新名称与当前名称相同")
        duplicate = self.departments.by_name(new_name)
        if duplicate and duplicate["id"] != department["id"]:
            raise ConflictError("部门名称已存在")
        normalized: dict = {"department_id": department["id"], "new_name": new_name}
        for field in ("manager", "phone"):
            value = str(payload.get(field) or "").strip()
            if value:
                normalized[field] = value
        return normalized

    def _validate_deactivate(self, payload: dict) -> dict:
        department = self._require_active_department(payload.get("department_id"), "部门")
        normalized: dict = {"department_id": department["id"]}
        successor = payload.get("successor_department_id")
        if successor is not None:
            target = self._require_active_department(successor, "继任部门")
            if target["id"] == department["id"]:
                raise ValidationError("继任部门不能是部门自身")
            normalized["successor_department_id"] = target["id"]
        normalized.update(self._validate_members(payload, default_policy="auto", allowed_targets={normalized.get("successor_department_id")}))
        return normalized

    def _validate_merge(self, payload: dict) -> dict:
        raw_sources = payload.get("source_department_ids")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise ValidationError("来源部门列表不能为空")
        source_ids: list[int] = []
        for raw in raw_sources:
            department = self._require_active_department(raw, "来源部门")
            if department["id"] in source_ids:
                raise ValidationError("来源部门不能重复")
            source_ids.append(department["id"])
        target = self._require_active_department(payload.get("target_department_id"), "目标部门")
        if target["id"] in source_ids:
            raise ValidationError("目标部门不能同时是来源部门")
        normalized: dict = {"source_department_ids": source_ids, "target_department_id": target["id"]}
        normalized.update(self._validate_members(payload, default_policy="auto", allowed_targets={target["id"]}))
        normalized["item_rules"] = self._validate_item_rules(payload.get("item_rules"), allowed_targets={target["id"]})
        return normalized

    def _validate_split(self, payload: dict) -> dict:
        source = self._require_active_department(payload.get("source_department_id"), "来源部门")
        raw_new = payload.get("new_departments")
        if not isinstance(raw_new, list) or not raw_new:
            raise ValidationError("拆分计划必须至少包含一个新部门")
        new_departments: list[dict] = []
        seen_names: set[str] = set()
        for index, spec in enumerate(raw_new):
            if not isinstance(spec, dict):
                raise ValidationError(f"新部门 #{index + 1} 格式无效")
            name = str(spec.get("name") or "").strip()
            manager = str(spec.get("manager") or "").strip()
            phone = str(spec.get("phone") or "").strip()
            if not name or not manager or not phone:
                raise ValidationError(f"新部门 #{index + 1} 的名称、负责人和电话不能为空")
            if name in seen_names:
                raise ValidationError(f"新部门名称重复：{name}")
            if self.departments.by_name(name):
                raise ConflictError(f"新部门名称已存在：{name}")
            seen_names.add(name)
            new_departments.append({"name": name, "manager": manager, "phone": phone})
        normalized: dict = {"source_department_id": source["id"], "new_departments": new_departments}
        normalized.update(
            self._validate_members(payload, default_policy="manual", allowed_targets=set(range(len(new_departments))))
        )
        if normalized["member_policy"] == "auto":
            raise ValidationError("拆分没有唯一的默认继任部门，成员请使用 member_assignments 逐一指派")
        normalized["item_rules"] = self._validate_item_rules(
            payload.get("item_rules"), allowed_targets=set(range(len(new_departments)))
        )
        return normalized

    def _validate_members(self, payload: dict, *, default_policy: str, allowed_targets: set[int | None]) -> dict:
        policy = payload.get("member_policy", default_policy)
        if policy not in MEMBER_POLICIES:
            raise ValidationError("成员处理策略必须是 auto、keep 或 manual")
        normalized: dict = {"member_policy": policy}
        raw_assignments = payload.get("member_assignments") or {}
        if not isinstance(raw_assignments, dict):
            raise ValidationError("成员指派格式无效")
        assignments: dict[str, int] = {}
        for raw_user, raw_target in raw_assignments.items():
            user = self.users.get(self._require_int(raw_user, "成员指派的用户"))
            if user is None:
                raise NotFoundError(f"成员指派的用户不存在：{raw_user}")
            target = self._require_int(raw_target, "成员指派的目标")
            if target not in allowed_targets:
                raise ValidationError(f"用户 {user['username']} 的指派目标不在本计划允许的范围内")
            assignments[str(user["id"])] = target
        if assignments:
            normalized["member_assignments"] = assignments
        return normalized

    def _validate_item_rules(self, raw_rules: object, *, allowed_targets: set[int]) -> list[dict]:
        if raw_rules is None:
            return []
        if not isinstance(raw_rules, list):
            raise ValidationError("待办迁移规则格式无效")
        rules: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for index, raw_rule in enumerate(raw_rules):
            if not isinstance(raw_rule, dict):
                raise ValidationError(f"待办迁移规则 #{index + 1} 格式无效")
            kind = raw_rule.get("kind")
            value = str(raw_rule.get("value") or "").strip()
            if kind == "affair_category":
                if value not in AFFAIR_CATEGORIES:
                    raise ValidationError(f"待办迁移规则 #{index + 1} 的事务分类无效")
            elif kind == "petition_type":
                if value not in PETITION_TYPES:
                    raise ValidationError(f"待办迁移规则 #{index + 1} 的信访类型无效")
            else:
                raise ValidationError(f"待办迁移规则 #{index + 1} 的匹配维度无效")
            target = self._require_int(raw_rule.get("target"), f"待办迁移规则 #{index + 1} 的目标")
            if target not in allowed_targets:
                raise ValidationError(f"待办迁移规则 #{index + 1} 的目标不在本计划允许的范围内")
            if (kind, value) in seen:
                raise ValidationError(f"待办迁移规则重复：{value}")
            seen.add((kind, value))
            rules.append({"kind": kind, "value": value, "target": target})
        return rules

    @staticmethod
    def _require_int(raw: object, label: str) -> int:
        try:
            return int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValidationError(f"{label}无效") from None
