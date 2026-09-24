from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.clock import FrozenClock, to_storage
from app.database import transaction
from app.services.orgchange import OrgChangeService, apply_due_plans


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def make_department(client, headers, name: str, manager: str = "主任", phone: str = "010-00000000") -> int:
    response = client.post(
        "/api/departments", headers=headers, json={"name": name, "manager": manager, "phone": phone}
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def make_user(client, admin, username: str, department_id: int | None = None) -> dict:
    response = client.post(
        "/api/users",
        headers=admin["headers"],
        json={
            "username": username,
            "password": "Clerk!23456",
            "display_name": username,
            "department_id": department_id,
            "role_codes": ["clerk"],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "Clerk!23456", "client_label": "tests"},
    )
    assert login.status_code == 200, login.text
    return {"id": body["id"], "token": login.json()["token"], "headers": {"Authorization": f"Bearer {login.json()['token']}"}}


def open_petition_in_department(client, department_id: int) -> int:
    created = client.post(
        "/petitions", json={"type": "意见建议", "target": "村道", "content": "待办事项", "contact": "13800000000"}
    )
    assert created.status_code == 201, created.text
    petition_id = created.json()["id"]
    assert client.post(f"/petitions/{petition_id}/receive").status_code == 200
    assert client.post(
        f"/petitions/{petition_id}/assign", json={"department_id": department_id, "deadline_days": 10}
    ).status_code == 200
    return petition_id


def open_affair_in_department(client, resident_id: int, department_id: int) -> int:
    created = client.post(
        "/affairs", json={"title": "待办事务", "category": "社保", "applicant_id": resident_id, "description": "x"}
    )
    assert created.status_code == 201, created.text
    affair_id = created.json()["id"]
    assert client.put(
        f"/affairs/{affair_id}/process", json={"status": "办理中", "department_id": department_id, "handler": "甲"}
    ).status_code == 200
    return affair_id


def create_resident(client) -> int:
    response = client.post(
        "/residents",
        json={"name": "李四", "id_card": "110101199102021234", "gender": "女", "birth_date": "1991-02-02",
              "address": "幸福路二号", "village": "幸福村"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_rename_keeps_history_displayed_under_org_at_the_time(client, admin):
    old_dept = make_department(client, admin["headers"], "民政办公室")
    resident_id = create_resident(client)
    petition_id = open_petition_in_department(client, old_dept)
    affair_id = open_affair_in_department(client, resident_id, old_dept)
    # 把部门存续起点、名称时间线起点与承办时刻回填到生效之前
    from app.database import get_connection

    three_days_ago = _iso(datetime.now(UTC) - timedelta(days=3))
    two_days_ago = _iso(datetime.now(UTC) - timedelta(days=2))
    conn = get_connection()
    conn.execute("UPDATE departments SET valid_from=? WHERE id=?", (three_days_ago, old_dept))
    conn.execute("UPDATE department_aliases SET valid_from=? WHERE department_id=?", (three_days_ago, old_dept))
    conn.execute("UPDATE petitions SET department_assigned_at=? WHERE id=?", (two_days_ago, petition_id))
    conn.execute("UPDATE affairs SET department_assigned_at=? WHERE id=?", (two_days_ago, affair_id))
    conn.commit()
    # 生效时刻取过去，模拟计划已到期
    effective = _iso(datetime.now(UTC) - timedelta(days=1))
    plan = client.post(
        "/api/org-changes/plans",
        headers=admin["headers"],
        json={
            "change_type": "rename",
            "source_department_ids": [old_dept],
            "rename_to": "民政和社会保障办公室",
            "effective_at": effective,
        },
    )
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    applied = client.post("/api/org-changes/apply-due", headers=admin["headers"])
    assert applied.status_code == 200, applied.text
    detail = client.get(f"/api/org-changes/plans/{plan_id}", headers=admin["headers"]).json()
    assert detail["status"] == "applied"

    # 部门当前名称已更新
    current = client.get("/api/departments", headers=admin["headers"]).json()
    names = [row["name"] for row in current["data"]]
    assert "民政和社会保障办公室" in names
    assert "民政办公室" not in names

    # 历史信访/事务仍按承办时刻展示旧部门名称
    petition = client.get(f"/petitions/{petition_id}").json()
    assert petition["department_name"] == "民政办公室"
    affair = client.get(f"/affairs/{affair_id}").json()
    assert affair["department_name"] == "民政办公室"

    # 沿革接口可查到名称时间线和继任关系类型
    timeline = client.get(f"/api/departments/{old_dept}/timeline", headers=admin["headers"]).json()
    alias_names = [item["name"] for item in timeline["aliases"]]
    assert alias_names == ["民政办公室", "民政和社会保障办公室"]

    # 按过去时刻还原组织视图应看到旧名，按现在看到新名
    asof_old = client.get(
        "/api/departments/timeline/as-of", headers=admin["headers"],
        params={"at": _iso(datetime.now(UTC) - timedelta(days=2))},
    ).json()
    assert any(row["id"] == old_dept and row["name"] == "民政办公室" for row in asof_old["data"])
    asof_now = client.get(
        "/api/departments/timeline/as-of", headers=admin["headers"],
        params={"at": _iso(datetime.now(UTC))},
    ).json()
    assert any(row["id"] == old_dept and row["name"] == "民政和社会保障办公室" for row in asof_now["data"])


def test_deactivate_single_successor_transfers_todos_and_staff_and_revokes_sessions(client, admin):
    old_dept = make_department(client, admin["headers"], "计划生育办公室")
    new_dept = make_department(client, admin["headers"], "卫生健康办公室")
    staff = make_user(client, admin, "transfer.staff", old_dept)
    petition_id = open_petition_in_department(client, old_dept)
    resident_id = create_resident(client)
    affair_id = open_affair_in_department(client, resident_id, old_dept)

    effective = _iso(datetime.now(UTC) - timedelta(minutes=5))
    plan = client.post(
        "/api/org-changes/plans",
        headers=admin["headers"],
        json={
            "change_type": "deactivate",
            "source_department_ids": [old_dept],
            "successors": [{"department_id": new_dept}],
            "effective_at": effective,
        },
    )
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    client.post("/api/org-changes/apply-due", headers=admin["headers"])
    detail = client.get(f"/api/org-changes/plans/{plan_id}", headers=admin["headers"]).json()
    assert detail["status"] == "applied"
    assert detail["open_conflicts"] == 0

    # 旧部门停用，待办自动迁给继任部门并留痕
    dept_row = client.get(f"/api/departments/{old_dept}/timeline", headers=admin["headers"]).json()["department"]
    assert dept_row["is_active"] == 0
    petition = client.get(f"/petitions/{petition_id}").json()
    assert petition["department_id"] == new_dept
    assert any(record["action"] == "组织调整移交" for record in petition["flow_records"])
    affair = client.get(f"/affairs/{affair_id}").json()
    assert affair["department_id"] == new_dept

    # 人员主部门与任职记录已切换；旧会话被撤销，权限数据范围随新部门重算
    me_old = client.get("/api/auth/me", headers=staff["headers"])
    assert me_old.status_code == 401
    user = client.get(f"/api/users/{staff['id']}", headers=admin["headers"]).json()
    assert user["department_id"] == new_dept
    relogin = client.post(
        "/api/auth/login",
        json={"username": "transfer.staff", "password": "Clerk!23456", "client_label": "tests"},
    ).json()
    assert relogin["user"]["department_id"] == new_dept

    # 继任关系可查
    timeline = client.get(f"/api/departments/{old_dept}/timeline", headers=admin["headers"]).json()
    assert timeline["successors"][0]["successor_id"] == new_dept
    reverse = client.get(f"/api/departments/{new_dept}/timeline", headers=admin["headers"]).json()
    assert reverse["predecessors"][0]["predecessor_id"] == old_dept


def test_split_with_manual_policy_blocks_completion_until_conflicts_resolved(client, admin):
    old_dept = make_department(client, admin["headers"], "综合办公室")
    dept_a = make_department(client, admin["headers"], "党建工作办公室")
    dept_b = make_department(client, admin["headers"], "政务服务办公室")
    staff = make_user(client, admin, "split.staff", old_dept)
    petition_id = open_petition_in_department(client, old_dept)

    effective = _iso(datetime.now(UTC) - timedelta(minutes=5))
    plan = client.post(
        "/api/org-changes/plans",
        headers=admin["headers"],
        json={
            "change_type": "split",
            "source_department_ids": [old_dept],
            "successors": [{"department_id": dept_a}, {"department_id": dept_b}],
            "transfer_policy": "manual",
            "staff_mappings": [{"user_id": staff["id"], "target": {"department_id": dept_a}}],
            "effective_at": effective,
        },
    )
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    client.post("/api/org-changes/apply-due", headers=admin["headers"])

    # 存在未解决归属冲突时计划不得标记为完成
    detail = client.get(f"/api/org-changes/plans/{plan_id}", headers=admin["headers"]).json()
    assert detail["status"] == "conflict"
    assert detail["open_conflicts"] >= 1
    conflicts = client.get("/api/org-changes/conflicts", headers=admin["headers"]).json()
    pending_petition = next(item for item in conflicts["data"] if item["resource_type"] == "petition")
    assert pending_petition["resource_id"] == petition_id
    assert set(pending_petition["candidate_department_ids"]) == {dept_a, dept_b}

    # 人员按明确规则迁到 A，旧会话撤销
    user = client.get(f"/api/users/{staff['id']}", headers=admin["headers"]).json()
    assert user["department_id"] == dept_a
    assert client.get("/api/auth/me", headers=staff["headers"]).status_code == 401

    # 待办人工确认迁到 B 后，计划才允许完成
    resolved = client.post(
        f"/api/org-changes/conflicts/{pending_petition['id']}/resolve",
        headers=admin["headers"],
        json={"department_id": dept_b},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["plan_status"] == "applied"
    detail = client.get(f"/api/org-changes/plans/{plan_id}", headers=admin["headers"]).json()
    assert detail["status"] == "applied"
    assert detail["open_conflicts"] == 0
    petition = client.get(f"/petitions/{petition_id}").json()
    assert petition["department_id"] == dept_b
    assert any(record["action"] == "组织调整归属确认" for record in petition["flow_records"])


def test_split_uncertain_staff_becomes_member_conflict(client, admin):
    old_dept = make_department(client, admin["headers"], "经济发展办公室")
    dept_a = make_department(client, admin["headers"], "招商办公室")
    dept_b = make_department(client, admin["headers"], "统计办公室")
    staff = make_user(client, admin, "lost.staff", old_dept)

    effective = _iso(datetime.now(UTC) - timedelta(minutes=5))
    plan = client.post(
        "/api/org-changes/plans",
        headers=admin["headers"],
        json={
            "change_type": "split",
            "source_department_ids": [old_dept],
            "successors": [{"department_id": dept_a}, {"department_id": dept_b}],
            "transfer_policy": "manual",
            "effective_at": effective,
        },
    )
    plan_id = plan.json()["id"]
    client.post("/api/org-changes/apply-due", headers=admin["headers"])
    # 未指定人员去向：主部门被清空并生成 member 类冲突，计划保持 conflict
    assert client.get("/api/auth/me", headers=staff["headers"]).status_code == 401
    conflicts = client.get("/api/org-changes/conflicts?resource_type=member", headers=admin["headers"]).json()
    member_conflict = next(item for item in conflicts["data"] if item["resource_id"] == staff["id"])
    result = client.post(
        f"/api/org-changes/conflicts/{member_conflict['id']}/resolve",
        headers=admin["headers"],
        json={"department_id": dept_b},
    )
    assert result.status_code == 200, result.text
    user = client.get(f"/api/users/{staff['id']}", headers=admin["headers"]).json()
    assert user["department_id"] == dept_b
    detail = client.get(f"/api/org-changes/plans/{plan_id}", headers=admin["headers"]).json()
    assert detail["status"] in {"applied", "conflict"}


def test_merge_two_departments_into_one(client, admin):
    dept_one = make_department(client, admin["headers"], "文化站")
    dept_two = make_department(client, admin["headers"], "广播站")
    merged = make_department(client, admin["headers"], "文化宣传服务中心")
    staff_one = make_user(client, admin, "merge.one", dept_one)
    staff_two = make_user(client, admin, "merge.two", dept_two)
    petition_one = open_petition_in_department(client, dept_one)
    petition_two = open_petition_in_department(client, dept_two)

    effective = _iso(datetime.now(UTC) - timedelta(minutes=5))
    plan = client.post(
        "/api/org-changes/plans",
        headers=admin["headers"],
        json={
            "change_type": "merge",
            "source_department_ids": [dept_one, dept_two],
            "successors": [{"department_id": merged}],
            "effective_at": effective,
        },
    )
    assert plan.status_code == 201, plan.text
    client.post("/api/org-changes/apply-due", headers=admin["headers"])
    assert plan.json()["status"] in {"planned", "applied"}
    detail = client.get(f"/api/org-changes/plans/{plan.json()['id']}", headers=admin["headers"]).json()
    assert detail["status"] == "applied"
    for petition_id in (petition_one, petition_two):
        assert client.get(f"/petitions/{petition_id}").json()["department_id"] == merged
    for staff in (staff_one, staff_two):
        assert client.get(f"/api/users/{staff['id']}", headers=admin["headers"]).json()["department_id"] == merged
        assert client.get("/api/auth/me", headers=staff["headers"]).status_code == 401


def test_plan_can_be_cancelled_before_effective_but_not_after(client, admin):
    dept = make_department(client, admin["headers"], "待撤销部门")
    future = client.post(
        "/api/org-changes/plans",
        headers=admin["headers"],
        json={
            "change_type": "rename",
            "source_department_ids": [dept],
            "rename_to": "未来名称",
            "effective_at": _iso(datetime.now(UTC) + timedelta(days=2)),
        },
    )
    future_id = future.json()["id"]
    cancelled = client.post(f"/api/org-changes/plans/{future_id}/cancel", headers=admin["headers"])
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "revoked"

    past = client.post(
        "/api/org-changes/plans",
        headers=admin["headers"],
        json={
            "change_type": "rename",
            "source_department_ids": [dept],
            "rename_to": "到期名称",
            "effective_at": _iso(datetime.now(UTC) - timedelta(minutes=1)),
        },
    )
    past_id = past.json()["id"]
    client.post("/api/org-changes/apply-due", headers=admin["headers"])
    denied = client.post(f"/api/org-changes/plans/{past_id}/cancel", headers=admin["headers"])
    assert denied.status_code == 409


def test_duplicate_pending_plan_on_same_department_is_rejected(client, admin):
    dept = make_department(client, admin["headers"], "有计划部门")
    payload = {
        "change_type": "rename",
        "source_department_ids": [dept],
        "rename_to": "新名称一号",
        "effective_at": _iso(datetime.now(UTC) + timedelta(days=3)),
    }
    assert client.post("/api/org-changes/plans", headers=admin["headers"], json=payload).status_code == 201
    payload["rename_to"] = "新名称二号"
    payload["effective_at"] = _iso(datetime.now(UTC) + timedelta(days=4))
    assert client.post("/api/org-changes/plans", headers=admin["headers"], json=payload).status_code == 409


def test_apply_is_idempotent_across_repeats_and_restart(client, admin):
    dept = make_department(client, admin["headers"], "重启验证部门")
    successor = make_department(client, admin["headers"], "重启继任部门")
    staff = make_user(client, admin, "restart.user", dept)
    petition_id = open_petition_in_department(client, dept)

    effective = _iso(datetime.now(UTC) - timedelta(minutes=5))
    plan = client.post(
        "/api/org-changes/plans",
        headers=admin["headers"],
        json={
            "change_type": "deactivate",
            "source_department_ids": [dept],
            "successors": [{"department_id": successor}],
            "effective_at": effective,
        },
    )
    plan_id = plan.json()["id"]
    # 模拟重启与重复补发
    client.post("/api/org-changes/apply-due", headers=admin["headers"])
    client.post("/api/org-changes/apply-due", headers=admin["headers"])
    apply_due_plans()
    detail = client.get(f"/api/org-changes/plans/{plan_id}", headers=admin["headers"]).json()
    assert detail["status"] == "applied"
    # 只产生一条继任关系、一次移交记录
    petition = client.get(f"/petitions/{petition_id}").json()
    assert petition["department_id"] == successor
    assert [r["action"] for r in petition["flow_records"]].count("组织调整移交") == 1
    user = client.get(f"/api/users/{staff['id']}", headers=admin["headers"]).json()
    assert user["department_id"] == successor


def test_rename_keeps_sessions_and_memberships_valid(client, admin):
    dept = make_department(client, admin["headers"], "规划办公室")
    staff = make_user(client, admin, "rename.keeps", dept)
    effective = _iso(datetime.now(UTC) - timedelta(minutes=5))
    plan = client.post(
        "/api/org-changes/plans",
        headers=admin["headers"],
        json={
            "change_type": "rename",
            "source_department_ids": [dept],
            "rename_to": "规划建设办公室",
            "effective_at": effective,
        },
    )
    client.post("/api/org-changes/apply-due", headers=admin["headers"])
    # 更名不改变部门实体：会话继续有效，主部门仍是同一部门 id
    assert client.get("/api/auth/me", headers=staff["headers"]).status_code == 200
    user = client.get(f"/api/users/{staff['id']}", headers=admin["headers"]).json()
    assert user["department_id"] == dept


def test_deactivate_with_explicit_todo_assignment(client, admin):
    old_dept = make_department(client, admin["headers"], "农机站")
    successor = make_department(client, admin["headers"], "农业服务中心")
    todo_target = make_department(client, admin["headers"], "应急管理办公室")
    petition_id = open_petition_in_department(client, old_dept)
    effective = _iso(datetime.now(UTC) - timedelta(minutes=5))
    plan = client.post(
        "/api/org-changes/plans",
        headers=admin["headers"],
        json={
            "change_type": "deactivate",
            "source_department_ids": [old_dept],
            "successors": [{"department_id": successor}],
            "todo_assignments": [{"from_department_id": old_dept, "to": {"department_id": todo_target}}],
            "effective_at": effective,
        },
    )
    plan_id = plan.json()["id"]
    client.post("/api/org-changes/apply-due", headers=admin["headers"])
    detail = client.get(f"/api/org-changes/plans/{plan_id}", headers=admin["headers"]).json()
    assert detail["status"] == "applied"
    # 待办按明确规则迁到应急办，而非继任的农业服务中心
    assert client.get(f"/petitions/{petition_id}").json()["department_id"] == todo_target
    # 继任关系仍指向农业服务中心
    timeline = client.get(f"/api/departments/{old_dept}/timeline", headers=admin["headers"]).json()
    assert timeline["successors"][0]["successor_id"] == successor


def test_split_creates_new_departments_via_keys(client, admin):
    old_dept = make_department(client, admin["headers"], "社会事务办公室")
    staff = make_user(client, admin, "keyed.staff", old_dept)
    petition_id = open_petition_in_department(client, old_dept)
    effective = _iso(datetime.now(UTC) - timedelta(minutes=5))
    plan = client.post(
        "/api/org-changes/plans",
        headers=admin["headers"],
        json={
            "change_type": "split",
            "source_department_ids": [old_dept],
            "new_departments": [
                {"key": "civil", "name": "民政事务股", "manager": "赵股", "phone": "010-11111111"},
                {"key": "insurance", "name": "社保事务股", "manager": "钱股", "phone": "010-22222222"},
            ],
            "successors": [{"key": "civil"}, {"key": "insurance"}],
            "transfer_policy": "manual",
            "staff_mappings": [{"user_id": staff["id"], "target": {"key": "civil"}}],
            "todo_assignments": [{"from_department_id": old_dept, "to": {"key": "insurance"}}],
            "effective_at": effective,
        },
    )
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    result = client.post("/api/org-changes/apply-due", headers=admin["headers"]).json()
    assert result["applied"] == 1
    detail = client.get(f"/api/org-changes/plans/{plan_id}", headers=admin["headers"]).json()
    # 显式待办去向 + 明确人员去向，无冲突，计划直接完成
    assert detail["status"] == "applied"
    assert detail["open_conflicts"] == 0
    civil = client.get("/api/departments", headers=admin["headers"]).json()
    names = {row["name"]: row for row in civil["data"]}
    assert "民政事务股" in names and "社保事务股" in names
    civil_id = names["民政事务股"]["id"]
    insurance_id = names["社保事务股"]["id"]
    assert client.get(f"/petitions/{petition_id}").json()["department_id"] == insurance_id
    assert client.get(f"/api/users/{staff['id']}", headers=admin["headers"]).json()["department_id"] == civil_id
    # 新设部门的存续起点等于生效时刻
    timeline = client.get(f"/api/departments/{civil_id}/timeline", headers=admin["headers"]).json()
    assert timeline["department"]["valid_from"] == detail["effective_at"]
    # 旧部门在沿革中指向两个继任
    old_timeline = client.get(f"/api/departments/{old_dept}/timeline", headers=admin["headers"]).json()
    assert {row["successor_id"] for row in old_timeline["successors"]} == {civil_id, insurance_id}


def test_service_layer_apply_with_frozen_clock():
    from app.database import close_connection, get_connection
    import os
    import tempfile

    tmp = tempfile.mkdtemp()
    os.environ["TOWNSHIP_DATABASE_PATH"] = os.path.join(tmp, "frozen.db")
    close_connection()
    from app.database import init_db

    init_db()
    clock = FrozenClock(datetime(2026, 9, 20, 0, 0, tzinfo=UTC))
    with transaction(immediate=True) as conn:
        now = to_storage(clock.now())
        cur = conn.execute(
            "INSERT INTO departments(name,manager,phone,is_active,valid_from,created_at,updated_at) VALUES('旧部门','主任','010-1',1,?,?,?)",
            (now, now, now),
        )
        old_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO department_aliases(department_id,name,valid_from,created_at) VALUES(?,?,?,?)",
            (old_id, "旧部门", now, now),
        )
        cur = conn.execute(
            "INSERT INTO departments(name,manager,phone,is_active,valid_from,created_at,updated_at) VALUES('新部门','主任','010-2',1,?,?,?)",
            (now, now, now),
        )
        new_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO department_aliases(department_id,name,valid_from,created_at) VALUES(?,?,?,?)",
            (new_id, "新部门", now, now),
        )
        cur = conn.execute(
            "INSERT INTO petitions(type,target,content,status,department_id,department_assigned_at,created_at,updated_at) "
            "VALUES('求助咨询','目标','内容','办理中',?,?,?,?)",
            (old_id, now, now, now),
        )
        petition_id = int(cur.lastrowid)

        from app.core.security import Principal

        principal = Principal(None, "admin", "管理员", None, frozenset({"orgchanges.write"}), 1)
        service = OrgChangeService(conn, clock)
        plan = service.create_plan(
            principal,
            {
                "change_type": "deactivate",
                "source_department_ids": [old_id],
                "successors": [{"department_id": new_id, "key": None}],
                "new_departments": [],
                "effective_at": "2026-09-24T00:00:00+00:00",
                "transfer_policy": "auto",
                "default_todo_target": None,
                "todo_assignments": [],
                "staff_mappings": [],
                "note": "",
                "rename_to": None,
            },
        )
        plan_id = plan["id"]

    # 生效时刻之前不会执行
    clock.current = datetime(2026, 9, 23, 23, 59, tzinfo=UTC)
    assert apply_due_plans(clock) == []
    with transaction(immediate=True) as conn:
        assert OrgChangeService(conn, clock).plans.require(plan_id)["status"] == "planned"
    # 到点后自动执行
    clock.current = datetime(2026, 9, 24, 0, 0, tzinfo=UTC)
    results = apply_due_plans(clock)
    assert len(results) == 1 and results[0]["status"] == "applied"
    with transaction(immediate=True) as conn:
        row = conn.execute("SELECT department_id FROM petitions WHERE id=?", (petition_id,)).fetchone()
        assert int(row[0]) == new_id
    close_connection()
