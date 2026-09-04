import json
import httpx
import pytest

from app.services.kids_classifier import KidsClassificationError, OpenCodexKidsClassifier


@pytest.mark.asyncio
async def test_classifier_checks_exact_model_and_returns_strict_safe_result():
    model_checks = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal model_checks
        if request.url.path == "/v1/models":
            model_checks += 1
            return httpx.Response(200, json={"data": [{"id": "gemini-kids"}]})
        assert request.url.path == "/v1/chat/completions"
        assert "authorization" not in request.headers
        payload = json.loads(request.content)
        assert "language exactly one of nl, en, mixed, or unknown" in payload["messages"][0]["content"]
        assert "content_kind exactly one of learning, entertainment, mixed, or unknown" in payload["messages"][0]["content"]
        assert 'age_suitability exactly an object with keys "2" and "6"' in payload["messages"][0]["content"]
        user_content = payload["messages"][1]["content"]
        assert user_content[0]["type"] == "text"
        assert user_content[1] == {
            "type": "image_url",
            "image_url": {
                "url": "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg",
                "detail": "low",
            },
        }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"verdict":"SAFE","language":"nl",'
                                '"content_kind":"learning",'
                                '"age_suitability":{"2":"SUITABLE","6":"SUITABLE"},'
                                '"reason":"calm animals","confidence":96}'
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    classifier = OpenCodexKidsClassifier(
        base_url="http://opencodex.test/v1",
        model="gemini-kids",
        client=client,
    )
    metadata = {
        "kind": "channel",
        "sample_videos": [
            {
                "video_id": "abcdefghijk",
                "title": "calm animals",
                "thumbnail_url": "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg",
            }
        ],
    }
    assert await classifier.classify(metadata) == {
        "verdict": "SAFE",
        "language": "nl",
        "content_kind": "learning",
        "age_suitability": {"2": "SUITABLE", "6": "SUITABLE"},
        "reason": "calm animals",
        "confidence": 96,
    }
    assert await classifier.classify(metadata)
    assert model_checks == 1
    await classifier.close()


def test_classifier_accepts_observed_markdown_fence_but_not_extra_prose():
    fenced = OpenCodexKidsClassifier._parse_json(
        '```json\n{"verdict":"SAFE","language":"nl","reason":"ok","confidence":99}\n```'
    )
    assert fenced["verdict"] == "SAFE"
    assert fenced["language"] == "nl"
    with pytest.raises((ValueError, json.JSONDecodeError)):
        OpenCodexKidsClassifier._parse_json("Here is the JSON: {}")


@pytest.mark.asyncio
async def test_classifier_fails_closed_for_unknown_model_or_bad_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "other-model"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    classifier = OpenCodexKidsClassifier(
        base_url="http://opencodex.test/v1",
        model="gemini-kids",
        client=client,
    )
    with pytest.raises(KidsClassificationError):
        await classifier.classify({"video_id": "v1"})
    await classifier.close()


@pytest.mark.asyncio
async def test_classifier_fails_closed_for_invalid_language():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gemini-kids"}]})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"verdict":"SAFE","language":"de",'
                                '"content_kind":"learning",'
                                '"age_suitability":{"2":"SUITABLE","6":"SUITABLE"},'
                                '"reason":"ok","confidence":99}'
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    classifier = OpenCodexKidsClassifier(
        base_url="http://opencodex.test/v1",
        model="gemini-kids",
        client=client,
    )
    with pytest.raises(KidsClassificationError):
        await classifier.classify({"video_id": "v1"})
    await classifier.close()


@pytest.mark.asyncio
async def test_classifier_fails_closed_for_missing_content_kind():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gemini-kids"}]})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"verdict":"SAFE","language":"nl",'
                                '"age_suitability":{"2":"SUITABLE","6":"SUITABLE"},'
                                '"reason":"ok","confidence":99}'
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    classifier = OpenCodexKidsClassifier(
        base_url="http://opencodex.test/v1",
        model="gemini-kids",
        client=client,
    )
    with pytest.raises(KidsClassificationError):
        await classifier.classify({"video_id": "v1"})
    await classifier.close()


@pytest.mark.asyncio
async def test_classifier_fails_closed_for_missing_or_invalid_age_suitability():
    responses = iter(
        [
            '{"verdict":"SAFE","language":"nl","content_kind":"learning",'
            '"reason":"ok","confidence":99}',
            '{"verdict":"SAFE","language":"nl","content_kind":"learning",'
            '"age_suitability":{"2":"SUITABLE","6":"MAYBE"},'
            '"reason":"ok","confidence":99}',
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gemini-kids"}]})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": next(responses)}}]},
        )

    classifier = OpenCodexKidsClassifier(
        base_url="http://opencodex.test/v1",
        model="gemini-kids",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(KidsClassificationError):
        await classifier.classify({"video_id": "v1"})
    with pytest.raises(KidsClassificationError):
        await classifier.classify({"video_id": "v2"})
    await classifier.close()
