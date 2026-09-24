from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class NewDepartmentSpec(BaseModel):
    """计划中随生效一并设立的新部门。"""

    key: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=100)
    manager: str = Field(min_length=1, max_length=50)
    phone: str = Field(min_length=3, max_length=30)


class DepartmentRef(BaseModel):
    """引用既有部门或计划内新设部门（key 与 department_id 二选一）。"""

    department_id: int | None = None
    key: str | None = Field(default=None, min_length=1, max_length=40)

    @model_validator(mode="after")
    def exactly_one(self):
        if (self.department_id is None) == (self.key is None):
            raise ValueError("department_id 与 key 必须且只能提供一个")
        return self


class StaffMapping(BaseModel):
    user_id: int
    target: DepartmentRef


class TodoAssignment(BaseModel):
    """把某来源部门的未结待办在生效时迁往指定部门。"""

    from_department_id: int
    to: DepartmentRef


class OrgChangePlanCreate(BaseModel):
    change_type: Literal["rename", "deactivate", "split", "merge"]
    effective_at: str
    source_department_ids: list[int] = Field(min_length=1, max_length=20)
    new_departments: list[NewDepartmentSpec] = Field(default_factory=list, max_length=20)
    rename_to: str | None = Field(default=None, min_length=1, max_length=100)
    successors: list[DepartmentRef] = Field(default_factory=list, max_length=20)
    transfer_policy: Literal["auto", "manual"] = "auto"
    default_todo_target: DepartmentRef | None = None
    todo_assignments: list[TodoAssignment] = Field(default_factory=list, max_length=50)
    staff_mappings: list[StaffMapping] = Field(default_factory=list, max_length=500)
    note: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_by_type(self):
        sources = dict.fromkeys(self.source_department_ids)
        if len(sources) != len(self.source_department_ids):
            raise ValueError("来源部门不能重复")
        new_keys = [item.key for item in self.new_departments]
        if len(set(new_keys)) != len(new_keys):
            raise ValueError("新设部门 key 不能重复")
        if self.change_type == "rename":
            if len(self.source_department_ids) != 1:
                raise ValueError("更名只能针对一个来源部门")
            if not self.rename_to or not self.rename_to.strip():
                raise ValueError("更名计划必须提供 rename_to")
            if self.successors or self.new_departments:
                raise ValueError("更名计划不需要继任部门")
        elif self.change_type == "deactivate":
            if len(self.source_department_ids) != 1:
                raise ValueError("停用只能针对一个来源部门")
            if self.rename_to:
                raise ValueError("停用计划不支持 rename_to")
            if len(self.successors) > 1:
                raise ValueError("停用最多指定一个继任部门")
        elif self.change_type == "split":
            if len(self.source_department_ids) != 1:
                raise ValueError("拆分只能针对一个来源部门")
            if len(self.successors) < 2:
                raise ValueError("拆分至少需要两个目标部门")
            if self.rename_to:
                raise ValueError("拆分计划不支持 rename_to")
        elif self.change_type == "merge":
            if len(self.source_department_ids) < 2:
                raise ValueError("合并至少需要两个来源部门")
            if len(self.successors) != 1:
                raise ValueError("合并必须且只能指定一个目标部门")
            if self.rename_to:
                raise ValueError("合并计划不支持 rename_to")
        return self


class OrgChangeConflictResolve(BaseModel):
    department_id: int
    remark: str = Field(default="", max_length=500)
