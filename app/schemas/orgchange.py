from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class OrgChangeCreateRequest(BaseModel):
    change_type: Literal["rename", "deactivate", "merge", "split"]
    effective_at: str = Field(min_length=1, max_length=40)
    summary: str = Field(default="", max_length=500)
    payload: dict = Field(default_factory=dict)


class OrgChangeItemResolveRequest(BaseModel):
    target_department_id: int
    resolution: str = Field(default="人工确认归属", max_length=200)
