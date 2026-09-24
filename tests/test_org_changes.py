from __future__ import annotations

from datetime import timedelta

from app.core.clock import to_storage, utc_now
from app.database import get_connection


def iso(moment) -> str:
    return to_storage(moment)


def create_department(client, admin, name: str, manager: str = "李主任", phone: str = "010-12345678") -> int:
    response = client.post("/api/departments", headers=admin["headers"], json={"name": name, "manager": manager, "phone": phone})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def backdate_department(department_id: int, created_at: str) -> None:
    connection = get_connection()
    connection.execute("UPDATE departments SET created_at=? WHERE id=?", (created_at, department_id))
    connection.execute("UPDATE department_snapshots SET effective_at=? WHERE department_id=?", (created_at, department_id))


def backdate(table: str, row_id: int, created_at: str) -> None:
    get_connection().execute(f"UPDATE {table} SET created_at=? WHERE id=?", (created_at, row_id))


def create_resident(client) -> int:
    response = client.post(
        "/residents",
        json={"name": "张三", "id_card": "110101199001011234", "gender": "男", "birth_date": "1990-01-01", "address": "幸福路一号", "village": "幸福村"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def create_affair(client, resident_id: int, department_id: int, *, category: str = "社保", title: str = "测试事务") -> int:
    response = client.post("/affairs", json={"title": title, "category": category, "applicant_id": resident_id})
    assert response.status_code == 201, response.text
    affair_id = response.json()["id"]
    processing = client.put(f"/affairs/{affair_id}/process", json={"status": "办理中", "department_id": department_id, "handler": "王经办"})
    assert processing.status_code == 200, processing.text
    return affair_id


def create_petition(client, department_id: int, *, petition_type: str = "意见建议") -> int:
    response = client.post("/petitions", json={"type": petition_type, "target": "村道照明", "content": "建议增设照明"})
    assert response.status_code == 201, response.text
    petition_id = response.json()["id"]
    assert client.post(f"/petitions/{petition_id}/receive").status_code == 200
    assigned = client.post(f"/petitions/{petition_id}/assign", json={"department_id": department_id, "deadline_days": 5})
    assert assigned.status_code == 200, assigned.text
    return petition_id


def department_map(client, admin) -> dict:
    rows = client.get("/api/departments", headers=admin["headers"], params={"active_only": False}).json()["data"]
    return {row["id"]: row for row in rows}


def test_rename_plan_applies_and_history_uses_snapshot(client, admin):
    now = utc_now()
    department_id = create_department(client, admin, "民政服务办")
    backdate_department(department_id, iso(now - timedelta(days=200)))
    resident_id = create_resident(client)
    old_affair = create_affair(client, resident_id, department_id, category="低保", title="低保申请")
    backdate("affairs", old_affair, iso(now - timedelta(days=120)))

    plan = client.post(
        "/api/org-changes",
        headers=admin["headers"],
        json={
            "change_type": "rename",
            "effective_at": iso(now - timedelta(days=30)),
            "summary": "民政服务办更名为便民服务中心",
            "payload": {"department_id": department_id, "new_name": "便民服务中心"},
        },
    )
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    assert plan.json()["status"] == "planned"

    applied = client.post(f"/api/org-changes/{plan_id}/apply", headers=admin["headers"])
    assert applied.status_code == 200, applied.text
    assert applied.json()["plan"]["status"] == "applied"

    departments = department_map(client, admin)
    assert departments[department_id]["name"] == "便民服务中心"

    old_detail = client.get(f"/affairs/{old_affair}").json()
    assert old_detail["department_name"] == "便民服务中心"
    assert old_detail["department_name_at"] == "民政服务办"
    new_affair = create_affair(client, resident_id, department_id, category="低保", title="低保证明")
    new_detail = client.get(f"/affairs/{new_affair}").json()
    assert new_detail["department_name_at"] == "便民服务中心"

    before = client.get("/api/departments", headers=admin["headers"], params={"as_of": iso(now - timedelta(days=60))}).json()["data"]
    assert before[0]["name"] == "民政服务办"
    after = client.get("/api/departments", headers=admin["headers"], params={"as_of": iso(now - timedelta(days=10))}).json()["data"]
    assert after[0]["name"] == "便民服务中心"

    timeline = client.get(f"/api/departments/{department_id}/timeline", headers=admin["headers"]).json()
    assert len(timeline["snapshots"]) >= 2
    assert timeline["successions"][0]["relation"] == "rename"
    assert timeline["successions"][0]["predecessor_id"] == department_id


def test_revoke_only_before_effective(client, admin):
    now = utc_now()
    department_id = create_department(client, admin, "临时协调办")
    plan = client.post(
        "/api/org-changes",
        headers=admin["headers"],
        json={"change_type": "deactivate", "effective_at": iso(now + timedelta(days=7)), "payload": {"department_id": department_id}},
    )
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    revoked = client.post(f"/api/org-changes/{plan_id}/revoke", headers=admin["headers"])
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["revoked_at"] is not None
    assert client.post(f"/api/org-changes/{plan_id}/apply", headers=admin["headers"]).status_code == 409
    assert client.post(f"/api/org-changes/{plan_id}/revoke", headers=admin["headers"]).status_code == 409

    second_id = create_department(client, admin, "老旧办公室")
    backdate_department(second_id, iso(now - timedelta(days=100)))
    past_plan = client.post(
        "/api/org-changes",
        headers=admin["headers"],
        json={"change_type": "deactivate", "effective_at": iso(now - timedelta(days=1)), "payload": {"department_id": second_id}},
    )
    assert past_plan.status_code == 201, past_plan.text
    assert client.post(f"/api/org-changes/{past_plan.json()['id']}/revoke", headers=admin["headers"]).status_code == 409


def test_merge_transfers_work_members_and_sessions(client, admin):
    now = utc_now()
    dept_a = create_department(client, admin, "农业服务站")
    dept_b = create_department(client, admin, "水利服务站")
    dept_c = create_department(client, admin, "农业农村办公室")
    for department_id in (dept_a, dept_b, dept_c):
        backdate_department(department_id, iso(now - timedelta(days=200)))

    user = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": "farm.clerk", "password": "Clerk!23456", "display_name": "农口经办", "role_codes": ["clerk"]},
    )
    assert user.status_code == 201, user.text
    user_id = user.json()["id"]
    membership = client.post(
        f"/api/departments/users/{user_id}/memberships",
        headers=admin["headers"],
        json={"department_id": dept_a, "is_primary": True, "starts_at": iso(now - timedelta(days=150)), "title": "经办员"},
    )
    assert membership.status_code == 201, membership.text
    login = client.post("/api/auth/login", json={"username": "farm.clerk", "password": "Clerk!23456", "client_label": "merge-test"})
    assert login.status_code == 200
    token = login.json()["token"]
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200

    resident_id = create_resident(client)
    affair_a = create_affair(client, resident_id, dept_a, category="社保")
    petition_b = create_petition(client, dept_b)

    plan = client.post(
        "/api/org-changes",
        headers=admin["headers"],
        json={
            "change_type": "merge",
            "effective_at": iso(now - timedelta(days=30)),
            "summary": "农业服务站与水利服务站合并",
            "payload": {"source_department_ids": [dept_a, dept_b], "target_department_id": dept_c, "member_policy": "auto"},
        },
    )
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    applied = client.post(f"/api/org-changes/{plan_id}/apply", headers=admin["headers"])
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["plan"]["status"] == "applied"
    assert body["result"]["migrated_items"] == 2
    assert body["result"]["transferred_memberships"] == 1
    assert body["result"]["revoked_sessions"] == 1

    departments = department_map(client, admin)
    assert departments[dept_a]["is_active"] == 0
    assert departments[dept_b]["is_active"] == 0
    assert departments[dept_c]["is_active"] == 1
    assert client.get(f"/affairs/{affair_a}").json()["department_id"] == dept_c
    petition_detail = client.get(f"/petitions/{petition_b}").json()
    assert petition_detail["department_id"] == dept_c
    assert any(record["action"] == "组织调整迁移" for record in petition_detail["flow_records"])

    old_membership = get_connection().execute(
        "SELECT * FROM department_memberships WHERE department_id=? AND user_id=?", (dept_a, user_id)
    ).fetchone()
    assert old_membership["ends_at"] is not None
    members = client.get(f"/api/departments/{dept_c}/members", headers=admin["headers"]).json()
    assert [row["user_id"] for row in members] == [user_id]
    assert members[0]["is_primary"] == 1

    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    fresh_login = client.post("/api/auth/login", json={"username": "farm.clerk", "password": "Clerk!23456", "client_label": "after-merge"})
    assert fresh_login.status_code == 200
    fresh = client.get("/api/auth/me", headers={"Authorization": f"Bearer {fresh_login.json()['token']}"}).json()
    assert fresh["department_id"] == dept_c

    successions = client.get(f"/api/departments/{dept_a}/timeline", headers=admin["headers"]).json()["successions"]
    assert {(row["relation"], row["successor_id"]) for row in successions} == {("merge", dept_c)}

    # 已停用部门不能再承办新业务
    new_petition = client.post("/petitions", json={"type": "求助咨询", "target": "灌溉水渠", "content": "请求协助"})
    petition_id = new_petition.json()["id"]
    client.post(f"/petitions/{petition_id}/receive")
    assert client.post(f"/petitions/{petition_id}/assign", json={"department_id": dept_a, "deadline_days": 3}).status_code == 404


def test_split_routes_items_and_conflicts_block_completion(client, admin):
    now = utc_now()
    source = create_department(client, admin, "社会事务办")
    backdate_department(source, iso(now - timedelta(days=200)))
    resident_id = create_resident(client)
    dibao = create_affair(client, resident_id, source, category="低保", title="低保办理")
    shebao = create_affair(client, resident_id, source, category="社保", title="社保登记")
    petition = create_petition(client, source, petition_type="投诉举报")

    plan = client.post(
        "/api/org-changes",
        headers=admin["headers"],
        json={
            "change_type": "split",
            "effective_at": iso(now - timedelta(days=30)),
            "payload": {
                "source_department_id": source,
                "new_departments": [
                    {"name": "民政救助股", "manager": "王股长", "phone": "010-11111111"},
                    {"name": "社会保障股", "manager": "刘股长", "phone": "010-22222222"},
                ],
                "item_rules": [
                    {"kind": "affair_category", "value": "低保", "target": 0},
                    {"kind": "petition_type", "value": "投诉举报", "target": 1},
                ],
            },
        },
    )
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    applied = client.post(f"/api/org-changes/{plan_id}/apply", headers=admin["headers"])
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["plan"]["status"] == "conflict"
    assert body["plan"]["conflict_count"] == 1
    assert body["plan"]["applied_at"] is None

    departments = department_map(client, admin)
    by_name = {row["name"]: department_id for department_id, row in departments.items()}
    minzheng = by_name["民政救助股"]
    shebao_dept = by_name["社会保障股"]
    assert departments[source]["is_active"] == 0
    assert departments[minzheng]["is_active"] == 1
    assert client.get(f"/affairs/{dibao}").json()["department_id"] == minzheng
    assert client.get(f"/petitions/{petition}").json()["department_id"] == shebao_dept
    assert client.get(f"/affairs/{shebao}").json()["department_id"] == source

    again = client.post(f"/api/org-changes/{plan_id}/apply", headers=admin["headers"])
    assert again.status_code == 200
    assert again.json()["plan"]["status"] == "conflict"
    assert again.json()["result"]["migrated_items"] == 0

    detail = client.get(f"/api/org-changes/{plan_id}", headers=admin["headers"]).json()
    manual_items = [item for item in detail["items"] if item["status"] == "manual"]
    assert len(manual_items) == 1
    assert manual_items[0]["item_type"] == "affair"
    resolved = client.post(
        f"/api/org-changes/{plan_id}/items/{manual_items[0]['id']}/resolve",
        headers=admin["headers"],
        json={"target_department_id": shebao_dept},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "applied"
    assert resolved.json()["applied_at"] is not None
    assert client.get(f"/affairs/{shebao}").json()["department_id"] == shebao_dept

    timeline = client.get(f"/api/departments/{source}/timeline", headers=admin["headers"]).json()
    split_successors = {row["successor_id"] for row in timeline["successions"] if row["relation"] == "split"}
    assert split_successors == {minzheng, shebao_dept}


def test_deactivate_without_successor_requires_manual_confirmation(client, admin):
    now = utc_now()
    department_id = create_department(client, admin, "临时征拆办")
    fallback = create_department(client, admin, "综合协调办")
    backdate_department(department_id, iso(now - timedelta(days=200)))
    petition = create_petition(client, department_id)

    plan = client.post(
        "/api/org-changes",
        headers=admin["headers"],
        json={"change_type": "deactivate", "effective_at": iso(now - timedelta(days=10)), "payload": {"department_id": department_id}},
    )
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    applied = client.post(f"/api/org-changes/{plan_id}/apply", headers=admin["headers"])
    assert applied.status_code == 200, applied.text
    assert applied.json()["plan"]["status"] == "conflict"
    assert client.get(f"/petitions/{petition}").json()["department_id"] == department_id

    item = applied.json()["plan"]["items"][0]
    assert item["status"] == "manual"
    assert item["target_department_id"] is None
    inactive_target = client.post(
        f"/api/org-changes/{plan_id}/items/{item['id']}/resolve",
        headers=admin["headers"],
        json={"target_department_id": department_id},
    )
    assert inactive_target.status_code == 404
    resolved = client.post(
        f"/api/org-changes/{plan_id}/items/{item['id']}/resolve",
        headers=admin["headers"],
        json={"target_department_id": fallback},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "applied"
    detail = client.get(f"/petitions/{petition}").json()
    assert detail["department_id"] == fallback
    assert any(record["action"] == "组织调整迁移" for record in detail["flow_records"])


def test_apply_due_is_idempotent_across_runs(client, admin):
    now = utc_now()
    department_id = create_department(client, admin, "计划生育办")
    backdate_department(department_id, iso(now - timedelta(days=200)))
    plan = client.post(
        "/api/org-changes",
        headers=admin["headers"],
        json={"change_type": "rename", "effective_at": iso(now - timedelta(days=5)), "payload": {"department_id": department_id, "new_name": "卫生健康办"}},
    )
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    first = client.post("/api/org-changes/apply-due", headers=admin["headers"])
    assert first.status_code == 200, first.text
    assert first.json()["results"] == [{"plan_id": plan_id, "status": "applied", "conflict_count": 0}]
    second = client.post("/api/org-changes/apply-due", headers=admin["headers"])
    assert second.json()["results"] == []
    again = client.post(f"/api/org-changes/{plan_id}/apply", headers=admin["headers"])
    assert again.status_code == 200
    assert again.json()["result"] == {"already_applied": True}
    departments = department_map(client, admin)
    assert departments[department_id]["name"] == "卫生健康办"


def test_service_restart_applies_due_plans(client, admin):
    from fastapi.testclient import TestClient

    from app.database import close_connection
    from app.main import app

    now = utc_now()
    department_id = create_department(client, admin, "重启生效办")
    backdate_department(department_id, iso(now - timedelta(days=200)))
    plan = client.post(
        "/api/org-changes",
        headers=admin["headers"],
        json={"change_type": "rename", "effective_at": iso(now - timedelta(days=2)), "payload": {"department_id": department_id, "new_name": "重启后生效"}},
    )
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    assert client.get(f"/api/org-changes/{plan_id}", headers=admin["headers"]).json()["status"] == "planned"

    close_connection()
    with TestClient(app) as restarted:
        detail = restarted.get(f"/api/org-changes/{plan_id}", headers=admin["headers"]).json()
        assert detail["status"] == "applied"
        departments = restarted.get("/api/departments", headers=admin["headers"]).json()["data"]
        assert departments[0]["name"] == "重启后生效"


def test_future_plan_cannot_be_applied_and_is_not_due(client, admin):
    now = utc_now()
    department_id = create_department(client, admin, "未来改革办")
    plan = client.post(
        "/api/org-changes",
        headers=admin["headers"],
        json={"change_type": "rename", "effective_at": iso(now + timedelta(days=30)), "payload": {"department_id": department_id, "new_name": "未来服务办"}},
    )
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    assert client.post(f"/api/org-changes/{plan_id}/apply", headers=admin["headers"]).status_code == 409
    assert client.post("/api/org-changes/apply-due", headers=admin["headers"]).json()["results"] == []
    assert client.get(f"/api/org-changes/{plan_id}", headers=admin["headers"]).json()["status"] == "planned"


def test_overlapping_plans_are_rejected(client, admin):
    now = utc_now()
    department_id = create_department(client, admin, "规划重叠办")
    first = client.post(
        "/api/org-changes",
        headers=admin["headers"],
        json={"change_type": "rename", "effective_at": iso(now + timedelta(days=30)), "payload": {"department_id": department_id, "new_name": "改名甲"}},
    )
    assert first.status_code == 201, first.text
    second = client.post(
        "/api/org-changes",
        headers=admin["headers"],
        json={"change_type": "deactivate", "effective_at": iso(now + timedelta(days=60)), "payload": {"department_id": department_id}},
    )
    assert second.status_code == 409


def test_plan_list_filters_by_status_and_type(client, admin):
    now = utc_now()
    department_id = create_department(client, admin, "列表过滤办")
    planned = client.post(
        "/api/org-changes",
        headers=admin["headers"],
        json={"change_type": "rename", "effective_at": iso(now + timedelta(days=30)), "payload": {"department_id": department_id, "new_name": "列表过滤新名"}},
    )
    assert planned.status_code == 201, planned.text
    revoked = client.post(f"/api/org-changes/{planned.json()['id']}/revoke", headers=admin["headers"])
    assert revoked.status_code == 200

    second = client.post(
        "/api/org-changes",
        headers=admin["headers"],
        json={"change_type": "deactivate", "effective_at": iso(now + timedelta(days=60)), "payload": {"department_id": department_id}},
    )
    assert second.status_code == 201, second.text

    all_plans = client.get("/api/org-changes", headers=admin["headers"]).json()
    assert all_plans["total"] == 2
    revoked_only = client.get("/api/org-changes", headers=admin["headers"], params={"status": "revoked"}).json()
    assert revoked_only["total"] == 1
    assert revoked_only["data"][0]["change_type"] == "rename"
    deactivate_only = client.get("/api/org-changes", headers=admin["headers"], params={"change_type": "deactivate"}).json()
    assert deactivate_only["total"] == 1
    assert deactivate_only["data"][0]["status"] == "planned"


def test_plan_validation_and_permissions(client, admin):
    now = utc_now()
    department_id = create_department(client, admin, "校验办")
    create_department(client, admin, "名称占用办")
    user = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": "no.perm", "password": "Clerk!23456", "display_name": "无权用户", "role_codes": []},
    )
    assert user.status_code == 201, user.text
    login = client.post("/api/auth/login", json={"username": "no.perm", "password": "Clerk!23456", "client_label": "t"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    denied = client.post(
        "/api/org-changes",
        headers=headers,
        json={"change_type": "rename", "effective_at": iso(now + timedelta(days=1)), "payload": {"department_id": department_id, "new_name": "无权改名"}},
    )
    assert denied.status_code == 403

    conflict = client.post(
        "/api/org-changes",
        headers=admin["headers"],
        json={"change_type": "rename", "effective_at": iso(now + timedelta(days=1)), "payload": {"department_id": department_id, "new_name": "名称占用办"}},
    )
    assert conflict.status_code == 409

    too_early = client.post(
        "/api/org-changes",
        headers=admin["headers"],
        json={"change_type": "rename", "effective_at": "2020-01-01T00:00:00+00:00", "payload": {"department_id": department_id, "new_name": "过早改名"}},
    )
    assert too_early.status_code == 422

    out_of_range = client.post(
        "/api/org-changes",
        headers=admin["headers"],
        json={
            "change_type": "split",
            "effective_at": iso(now + timedelta(days=1)),
            "payload": {
                "source_department_id": department_id,
                "new_departments": [{"name": "唯一新部门", "manager": "甲", "phone": "010-11111111"}],
                "item_rules": [{"kind": "affair_category", "value": "低保", "target": 3}],
            },
        },
    )
    assert out_of_range.status_code == 422

    bad_rule_kind = client.post(
        "/api/org-changes",
        headers=admin["headers"],
        json={
            "change_type": "split",
            "effective_at": iso(now + timedelta(days=1)),
            "payload": {
                "source_department_id": department_id,
                "new_departments": [{"name": "另一个新部门", "manager": "乙", "phone": "010-22222222"}],
                "item_rules": [{"kind": "unknown", "value": "低保", "target": 0}],
            },
        },
    )
    assert bad_rule_kind.status_code == 422
