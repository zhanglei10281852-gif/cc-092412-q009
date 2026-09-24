# 乡镇政务协同服务

这是一个面向乡镇综合服务中心的模块化后端，集中管理居民档案、政务事务、信访流转、公告、部门、用户、角色、权限、会话、审计和可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 居民档案：登记、查询、更新和关联事务。
- 事务办理：受理、分派、退回、办结和部门责任查询。
- 信访流转：签收、分派、办理、审核、复查、催办和流转记录。
- 公告与部门：公告置顶、分类检索、部门信息及关联业务查看。
- 组织变更：带生效日期的更名、停用、拆分、合并与继任关系；历史按当时组织展示，待办按规则迁移或人工确认归属。
- 身份与权限：用户、角色、细粒度权限、会话令牌、账号停用和会话撤销。
- 审计记录：关键身份操作留痕，并对口令和令牌等敏感字段做过滤。
- 后台任务：使用 SQLite 保存待执行任务，支持去重、租约、重试和完成回执。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

配置项均以 `TOWNSHIP_` 开头。可以复制 `.env.example` 后按需设置，默认数据库位于 `./data/township.db`。

## 初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

首次部署可创建唯一的初始管理员：

```bash
curl -sS -X POST http://127.0.0.1:8432/api/auth/bootstrap   -H 'Content-Type: application/json'   -d '{"username":"admin","password":"Admin!23456","client_label":"initial-setup"}'
```

之后通过 `/api/auth/login` 获取会话令牌，并在管理接口请求头中使用 `Authorization: Bearer <token>`。

## 测试

```bash
python -m pytest
```

测试覆盖身份初始化、登录、用户与角色维护、权限计算、账号停用后的会话撤销、审计脱敏、居民事务、信访状态流转、公告排序、后台任务去重与领取，以及数据库时间格式。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内启动应用并检查服务根路径与健康接口，适合部署前快速确认路由和数据库初始化是否正常。

## 目录结构

```text
app/
  api/             用户、角色、审计、认证和系统接口
  repositories/    SQLite 查询与持久化读取
  routers/         居民、事务、公告、部门和信访业务接口
  schemas/         管理接口输入模型
  services/        身份、审计、后台任务和组织变更领域服务
  cli.py           初始化、检查、冒烟和组织变更补发入口
  database.py      SQLite 连接、事务、表结构与基础权限
tests/             核心、管理接口、原有业务和组织变更回归测试
tools/             本地维护脚本
```

## 组织变更

组织调整通过"变更计划"管理，避免直接改名造成历史归属失真：

- 计划带 `effective_at` 生效时间，类型支持 `rename`（更名）、`deactivate`（停用）、`split`（拆分）、`merge`（合并），生效前可撤销。
- 部门名称按 `department_aliases` 时间线管理，继任关系记入 `department_successors`；事务与信访保存承办时刻，历史查询按当时组织展示，`GET /api/departments/timeline/as-of?at=...` 可还原任意时刻组织视图。
- 当前待办的迁移规则：唯一继任或计划中显式指定去向时自动迁移；拆分等存在多个可能去向时按 `manual` 策略生成归属确认项，由人工确认。存在未解决归属冲突时计划保持 `conflict`，不会标记为完成。
- 生效时同步结束旧任职、建立继任部门任职、切换用户主部门，并撤销相关会话，使登录后的数据范围与权限按新部门重新计算。
- 计划生效具有幂等性：重复补发、服务重启（启动时自动扫描到期计划）或通过 `python -m app.cli apply-org-changes` 手动补发都不会重复迁移；单个计划失败标记为 `failed`，可在修复后重试，不影响其他计划。

## 数据一致性

需要跨多张表更新的管理操作在即时事务中执行，失败会整体回滚。会话令牌只保存摘要；用户停用或组织调整转岗会撤销仍有效的会话。审计事件保存操作者、动作、资源、结果和前后状态，但不会保存明文密码或令牌。
