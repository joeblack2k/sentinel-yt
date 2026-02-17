import importlib


def test_history_allow_candidates_filters_and_dedupes():
    module = importlib.import_module("app.main")
    rows = [
        {"verdict": "ALLOW", "video_id": "aaa111"},
        {"verdict": "BLOCK", "video_id": "bbb222"},
        {"verdict": "ALLOW", "video_id": "aaa111"},  # duplicate allow
        {"verdict": "ALLOW", "video_id": "ccc333"},
        {"verdict": "ALLOW", "video_id": "bbb222"},  # blocked current video
        {"verdict": "ALLOW", "video_id": ""},
    ]

    got = module.RuntimeState._history_allow_candidates(rows, blocked_video_id="bbb222")
    assert got == ["aaa111", "ccc333"]


def test_randomized_history_candidates_avoids_immediate_repeat(monkeypatch):
    module = importlib.import_module("app.main")

    # Keep ordering deterministic for this test so we can verify anti-repeat behavior.
    monkeypatch.setattr(module.random, "shuffle", lambda seq: None)

    state = module.RuntimeState.__new__(module.RuntimeState)
    state.last_history_choice = {42: "aaa111"}

    got = state._randomized_history_candidates(
        device_id=42,
        candidate_ids=["aaa111", "bbb222", "ccc333"],
    )

    assert got[0] != "aaa111"
    assert sorted(got) == ["aaa111", "bbb222", "ccc333"]
