from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class ControlStateRequest(BaseModel):
    active: bool


CatalogState = Literal["candidate", "approved", "blocked", "revoked", "unknown"]


class CatalogSourceRequest(BaseModel):
    kind: Literal["channel", "playlist"]
    reference: str = Field(min_length=1, max_length=256)
    title: str = Field(default="", max_length=500)
    language: Literal["nl", "en", "mixed", "unknown"] = "unknown"


class CatalogItemRequest(BaseModel):
    video_id: str = Field(min_length=1, max_length=64)
    title: str = Field(default="", max_length=500)
    source_id: int | None = Field(default=None, ge=1)
    thumbnail_url: str = Field(default="", max_length=2000)
    duration_seconds: int = Field(default=0, ge=0, le=86400)
    visual_category: str = Field(default="general", max_length=64)


class CatalogTransitionRequest(BaseModel):
    state: CatalogState
    actor: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)
    correlation_id: str = Field(min_length=1, max_length=128)

class KidsKillSwitchRequest(BaseModel):
    enabled: bool
    actor: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)
    correlation_id: str = Field(min_length=1, max_length=128)


class KidsWatchEventRequest(BaseModel):
    video_id: str = Field(min_length=1, max_length=64)
    event: Literal["selected", "started", "completed", "stopped"]
    profile: str = Field(default="noah", min_length=1, max_length=64)
    position_seconds: float | None = Field(default=None, ge=0, le=86400)
    session_id: str = Field(default="", max_length=128)
    startup_ms: int | None = Field(default=None, ge=0, le=120000)
    correlation_id: str = Field(min_length=1, max_length=128)


class KidsPlaybackSessionRequest(BaseModel):
    asset_id: str = Field(min_length=8, max_length=128)


class KidsDataplaneEventRequest(BaseModel):
    asset_id: str = Field(min_length=8, max_length=128)
    event: Literal["selected", "started", "completed", "stopped"]
    profile: str = Field(default="noah", min_length=1, max_length=64)
    position_seconds: float | None = Field(default=None, ge=0, le=86400)
    session_id: str = Field(default="", max_length=128)
    startup_ms: int | None = Field(default=None, ge=0, le=120000)
    correlation_id: str = Field(default="", max_length=128)


class WebhookControlRequest(BaseModel):
    active: bool
    source: str = "home_assistant"


class RuleRequest(BaseModel):
    video_id: Optional[str] = None
    channel_id: Optional[str] = None
    label: Optional[str] = None
    url: Optional[str] = None
    scope: Literal["video", "channel"]

    @field_validator("video_id", "channel_id", "label", "url")
    @classmethod
    def normalize_empty(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        v = value.strip()
        return v or None


class PolicyFlagsRequest(BaseModel):
    flags: dict[str, bool] = Field(default_factory=dict)


class ScheduleRequest(BaseModel):
    enabled: bool
    start: str
    end: str
    timezone: str


class ScheduleWindowRequest(BaseModel):
    name: str = "Schedule"
    enabled: bool = True
    start: str
    end: str
    timezone: str
    mode: Literal["blocklist"] = "blocklist"

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        out = (value or "").strip()
        return out or "Schedule"


class WebhookSettingsRequest(BaseModel):
    failure_webhook_url: str = ""


class RulesImportSourcesRequest(BaseModel):
    urls: list[str] = Field(default_factory=list)

    @field_validator("urls")
    @classmethod
    def normalize_urls(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for item in value:
            raw = (item or "").strip()
            if not raw:
                continue
            out.append(raw)
        return out


class LocalBlocklistContentRequest(BaseModel):
    content: str


class PurgeRequest(BaseModel):
    target: Literal["history", "all"]
