"""Esquema de acciones que devuelve el LLM y su resultado al aplicarlas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

ActionType = Literal["post", "reply", "repost", "quote", "like", "follow", "unfollow", "do_nothing"]
TEXT_ACTIONS = ("post", "reply", "quote")
POST_TARGET_ACTIONS = ("reply", "repost", "quote", "like")
HANDLE_TARGET_ACTIONS = ("follow", "unfollow")


class Action(BaseModel):
    type: ActionType
    text: str | None = Field(None, description="texto para post, reply y quote")
    target_post_id: str | None = Field(None, description="id del post para reply, repost, quote y like")
    target_handle: str | None = Field(None, description="handle (sin @) para follow y unfollow")

    @field_validator("target_handle")
    @classmethod
    def _strip_at(cls, v: str | None) -> str | None:
        return v.strip().lstrip("@").lower() if v else v

    @field_validator("target_post_id")
    @classmethod
    def _strip_id(cls, v: str | None) -> str | None:
        return v.strip().strip("[]") if v else v


class AgentDecision(BaseModel):
    agent_id: str
    reasoning: str = Field(description="justificación interna breve (no se publica)")
    actions: list[Action] = Field(
        min_length=1,
        description="1–4 acciones concretas que hace en esta ronda; usa do_nothing si no hace nada",
    )
    new_stance: float = Field(ge=-1, le=1, description="postura tras esta ronda")


class BatchDecisions(BaseModel):
    decisions: list[AgentDecision]


class ActionOutcome(BaseModel):
    round: int
    agent_id: str
    type: ActionType
    target_post_id: str | None = None
    target_handle: str | None = None
    post_id: str | None = None  # post creado (post, reply, repost, quote)
    valid: bool = True
    reason: str | None = None

    def row(self) -> dict[str, object]:
        d = self.model_dump()
        d["valid"] = int(self.valid)
        return d
