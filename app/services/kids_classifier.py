from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

import httpx


AGE_SUITABILITY_VALUES = frozenset({"SUITABLE", "UNSUITABLE", "UNCERTAIN"})


def normalize_age_suitability(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"2", "6"}:
        raise ValueError("invalid age suitability")
    if any(
        not isinstance(value[age], str) or value[age] not in AGE_SUITABILITY_VALUES
        for age in ("2", "6")
    ):
        raise ValueError("invalid age suitability")
    return {age: value[age] for age in ("2", "6")}


class KidsClassificationError(RuntimeError):
    """The safety model could not return a usable decision."""


class OpenCodexKidsClassifier:
    """OpenAI-compatible, local OpenCodex classifier with deny-on-error semantics."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.client = client or httpx.AsyncClient(base_url=self.base_url, timeout=45.0)
        self._owns_client = client is None
        self._model_checked = False

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def check_model(self) -> str:
        if self._model_checked:
            return self.model
        response = await self.client.get(f"{self.base_url}/models")
        response.raise_for_status()
        model_ids = {str(entry.get("id", "")) for entry in response.json().get("data", [])}
        if self.model not in model_ids:
            raise KidsClassificationError("configured OpenCodex model is unavailable")
        self._model_checked = True
        return self.model

    async def classify(self, metadata: dict[str, Any]) -> dict[str, Any]:
        try:
            await self.check_model()
            thumbnail_urls: list[str] = []
            for sample in metadata.get("sample_videos", []):
                value = str(sample.get("thumbnail_url", "")) if isinstance(sample, dict) else ""
                parsed = urlsplit(value)
                if parsed.scheme == "https" and parsed.hostname == "i.ytimg.com" and value not in thumbnail_urls:
                    thumbnail_urls.append(value)
            user_content: str | list[dict[str, Any]] = json.dumps(metadata, ensure_ascii=True)
            if thumbnail_urls:
                user_content = [{"type": "text", "text": user_content}]
                user_content.extend(
                    {
                        "type": "image_url",
                        "image_url": {"url": url, "detail": "low"},
                    }
                    for url in thumbnail_urls[:4]
                )
            response = await self.client.post(
                f"{self.base_url}/chat/completions",
                json={
                    "model": self.model,
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Classify this Kids source or video for a calm children's catalog "
                                "with separate target ages 2 and 6. "
                                "When kind is channel, judge the channel from all supplied samples, including thumbnails. "
                                "Mark brainrot, shouting, rapid-cut stimulation, manipulative engagement, dangerous challenges, "
                                "horror, violence, weapons, pranks, Elsagate patterns, purchase pressure, and repetitive "
                                "low-value content UNSAFE. Calm animals, nature, building, LEGO-style creativity, stories, "
                                "and age-appropriate learning may be SAFE. "
                                "Return only strict JSON with verdict SAFE, UNSAFE, or UNCERTAIN, "
                                "language exactly one of nl, en, mixed, or unknown, "
                                "content_kind exactly one of learning, entertainment, mixed, or unknown, "
                                'age_suitability exactly an object with keys "2" and "6", each '
                                "set to SUITABLE, UNSUITABLE, or UNCERTAIN. "
                                "a short reason, and confidence 0-100. "
                                "Always include the language field: use nl for Dutch, en for English, mixed for both, "
                                "and unknown when language evidence is incomplete. "
                                "Always include the content_kind field: use learning for educational content, "
                                "entertainment for fun content, mixed when both are central, and unknown when evidence is incomplete. "
                                "Judge safety independently from age. For age_suitability, only mark an age "
                                "SUITABLE when the supplied channel evidence explicitly supports that target age; "
                                "use UNSUITABLE for clearly inappropriate developmental level and UNCERTAIN when "
                                "evidence is incomplete. "
                                "Choose UNCERTAIN whenever evidence is incomplete."
                            ),
                        },
                        {"role": "user", "content": user_content},
                    ],
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            result = self._parse_json(content)
            verdict = result.get("verdict")
            if verdict not in {"SAFE", "UNSAFE", "UNCERTAIN"}:
                raise ValueError("invalid verdict")
            language = result.get("language", "unknown")
            if not isinstance(language, str) or language not in {"nl", "en", "mixed", "unknown"}:
                raise ValueError("invalid language")
            content_kind = result.get("content_kind")
            if not isinstance(content_kind, str) or content_kind not in {
                "learning",
                "entertainment",
                "mixed",
                "unknown",
            }:
                raise ValueError("invalid content kind")
            age_suitability = normalize_age_suitability(result.get("age_suitability"))
            confidence = int(result.get("confidence", 0))
            if not 0 <= confidence <= 100:
                raise ValueError("invalid confidence")
            return {
                "verdict": verdict,
                "language": language,
                "content_kind": content_kind,
                "age_suitability": age_suitability,
                "reason": str(result.get("reason", ""))[:1000],
                "confidence": confidence,
            }
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise KidsClassificationError("OpenCodex classification failed") from exc

    @staticmethod
    def _parse_json(content: Any) -> dict[str, Any]:
        if not isinstance(content, str):
            raise ValueError("classifier content is not text")
        text = content.strip()
        # OpenCodex may wrap an otherwise valid JSON object in a markdown fence.
        if text.startswith("```") and text.endswith("```"):
            lines = text.splitlines()
            if len(lines) < 3:
                raise ValueError("empty fenced classifier response")
            text = "\n".join(lines[1:-1]).strip()
        result = json.loads(text)
        if not isinstance(result, dict):
            raise ValueError("classifier response is not an object")
        return result
