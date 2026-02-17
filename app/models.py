from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class ControlStateRequest(BaseModel):
    active: bool


class WebhookControlRequest(BaseModel):
    active: bool
    source: str = "home_assistant"


class SponsorBlockStateRequest(BaseModel):
    active: bool


class SponsorBlockScheduleRequest(BaseModel):
    enabled: bool
    start: str
    end: str
    timezone: str


class SponsorBlockConfigRequest(BaseModel):
    categories: list[str] = Field(default_factory=list)
    min_length_seconds: float = Field(default=1.0, ge=0.0, le=30.0)

    @field_validator("categories")
    @classmethod
    def normalize_categories(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            key = (item or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out


class SponsorBlockReleaseRequest(BaseModel):
    minutes: int = Field(ge=0, le=240, default=0)
    reason: str = "manual"
    source: str = "dashboard"


class PairDeviceRequest(BaseModel):
    device_ref: str
    pairing_code: str = Field(min_length=4, max_length=32)


class PairCodeOnlyRequest(BaseModel):
    pairing_code: str = Field(min_length=4, max_length=32)


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


class PromptRequest(BaseModel):
    custom_prompt: str = ""


class PolicyFlagsRequest(BaseModel):
    flags: dict[str, bool] = Field(default_factory=dict)


class AllowPolicyFlagsRequest(BaseModel):
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
    mode: Literal["blocklist", "whitelist"] = "blocklist"

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        out = (value or "").strip()
        return out or "Schedule"


class WebhookSettingsRequest(BaseModel):
    failure_webhook_url: str = ""


class GeminiSettingsRequest(BaseModel):
    api_key: str = ""
    enabled: Optional[bool] = None


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
    target: Literal["analysis_cache", "history", "all"]


class AnalyzeVideoInput(BaseModel):
    video_id: str
    title: str = ""
    channel_id: str = ""
    channel_title: str = ""


class DecisionOutput(BaseModel):
    verdict: Literal["ALLOW", "BLOCK"]
    reason: str
    confidence: int = Field(ge=0, le=100)
    source: Literal[
        "gemini",
        "whitelist",
        "blacklist",
        "file_blacklist",
        "file_whitelist",
        "fallback",
        "policy",
        "policy_allowlist",
    ]
