from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def uid() -> str:
    return uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return dt.astimezone(timezone.utc)


class Strict(BaseModel):
    model_config = {"extra": "forbid"}


class Persona(Strict):
    name: str = Field(min_length=1, max_length=40)
    age: int = Field(ge=18, le=60)
    school: str
    major: str
    year: str
    personality: str
    speaking_style: str
    interests: list[str]
    background: str
    courses: list[str]
    people: list[str]


class Link(Strict):
    target: str
    kind: Literal["event", "cause", "continuation", "correction"]
    strength: float = Field(ge=0, le=1)


class MemoryDraft(Strict):
    content: str = Field(min_length=1, max_length=1200)
    subject: str
    kind: Literal["fact", "preference", "experience", "promise", "feeling", "inference"]
    source_type: Literal["user", "virtual", "feeling", "inference"]
    source_ids: list[str]
    occurred_at: str
    entities: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    importance: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    unresolved: bool = False
    supersedes: str | None = None
    explicit_correction: bool = False
    links: list[Link] = Field(default_factory=list)

    @field_validator("occurred_at")
    @classmethod
    def valid_time(cls, v):
        parse(v)
        return v


class Plan(Strict):
    at: str
    intent: str
    condition: str
    reason: str
    memory_ids: list[str] = Field(default_factory=list)

    @field_validator("at")
    @classmethod
    def valid_time(cls, v):
        parse(v)
        return v


class Decision(Strict):
    action: Literal["reply", "defer", "wait", "cancel"]
    messages: list[str] = Field(default_factory=list, max_length=8)
    # Normal conversation is one or two bubbles; share is for a worthwhile story.
    message_mode: Literal["normal", "share"] = "normal"
    reply_at: str | None = None
    next_plan: Plan | None = None
    reason: str
    state: str
    used_memory_ids: list[str] = Field(default_factory=list)
    confirmed_memory_ids: list[str] = Field(default_factory=list)
    resolved_memory_ids: list[str] = Field(default_factory=list)
    memories: list[MemoryDraft] = Field(default_factory=list)
    change_future_schedule: bool = False


class Activity(Strict):
    at: str
    until: str
    title: str
    outline: str


class DaySchedule(Strict):
    activities: list[Activity] = Field(max_length=16)


class Experience(Strict):
    content: str
    state: str
    memories: list[MemoryDraft] = Field(default_factory=list)


class Diary(Strict):
    content: str


class CompressedMemory(Strict):
    content: str = Field(min_length=1, max_length=200)
