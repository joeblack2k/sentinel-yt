import httpx
import pytest

from app.services.kids_classifier import KidsClassificationError, OpenCodexKidsClassifier


@pytest.mark.asyncio
async def test_classifier_checks_exact_model_and_returns_strict_safe_result():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gemini-kids"}]})
        assert request.url.path == "/v1/chat/completions"
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"verdict":"SAFE","reason":"calm animals","confidence":96}'
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
    assert await classifier.classify({"video_id": "v1", "category": "animals"}) == {
        "verdict": "SAFE",
        "reason": "calm animals",
        "confidence": 96,
    }
    await classifier.close()


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
