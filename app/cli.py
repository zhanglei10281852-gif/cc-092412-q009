from __future__ import annotations

import argparse
import json

from fastapi.testclient import TestClient

from app.database import database_path, get_connection, init_db
from app.main import app
from app.services.orgchange import apply_due_plans


def command_init() -> int:
    init_db()
    print(json.dumps({"database": str(database_path()), "status": "initialized"}, ensure_ascii=False))
    return 0


def command_check() -> int:
    init_db()
    connection = get_connection()
    result = {
        "database": str(database_path()),
        "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "tables": connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["integrity"] == "ok" and result["foreign_keys"] == 1 else 1


def command_smoke() -> int:
    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
    result = {"root": root.json(), "health": health.json(), "status_codes": [root.status_code, health.status_code]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status_codes"] == [200, 200] else 1


def command_apply_org() -> int:
    init_db()
    results = apply_due_plans()
    payload = {"applied": len(results), "results": [{"plan_id": item.get("plan_id") or item.get("id"), "status": item.get("status")} for item in results]}
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if all(item.get("status") != "failed" for item in results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="township-service", description="乡镇政务协同服务维护入口")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化 SQLite 数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行本地 API 冒烟检查")
    subparsers.add_parser("apply-org-changes", help="补发所有已到期的组织变更计划")
    args = parser.parse_args()
    return {
        "init-db": command_init,
        "check-db": command_check,
        "smoke": command_smoke,
        "apply-org-changes": command_apply_org,
    }[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
