from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"


def render_sources(sources):
    environment = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html", "xml"]),
    )
    return environment.get_template("sources.html").render(
        page="sources",
        profiles=[],
        sources=sources,
        status={
            "active": True,
            "monitoring_effective": True,
            "schedule_active_now": True,
            "judge_ok": True,
            "timezone": "Europe/Amsterdam",
            "build_version": "gui-test",
        },
    )


def source_row(source_id, reference, **fields):
    source = {
        "id": source_id,
        "kind": "channel",
        "reference": reference,
        "title": f"Source {source_id}",
        "state": "approved",
        "safety_verdict": "SAFE",
        "profile_slugs": ["noah"],
        "source_url": "",
        "youtube_source_url": "",
        "safety_reason": "",
        "safety_sample_count": 0,
        "safety_policy_version": "1",
        "safety_checked_at": "",
        "ready_item_count": 0,
        "approved_item_count": 0,
        "item_count": 0,
        "avatar_url": "https://legacy.example/avatar.png",
    }
    source.update(fields)
    return source


def test_sources_template_renders_real_poster_contract_without_fallback_artwork():
    html = render_sources(
        [
            source_row(
                1,
                "UC-real",
                effective_poster_thumbnail_url="https://i.ytimg.com/vi/real/hqdefault.jpg",
                poster_item_id=42,
                genuine_avatar_url="https://yt3.ggpht.com/real/avatar=s88-c-k-c0x00ffffff-no-rj",
            ),
            source_row(2, "UC-no-art-fields"),
            source_row(3, "__youtube_kids_home__", title="System ingest"),
        ]
    )

    assert "https://i.ytimg.com/vi/real/hqdefault.jpg" in html
    assert "https://yt3.ggpht.com/real/avatar=s88-c-k-c0x00ffffff-no-rj" in html
    assert "https://legacy.example/avatar.png" not in html
    assert "source-avatar-initial" not in html
    assert "Custom" in html
    assert "Auto poster unavailable" in html
    assert html.count('class="btn-poster"') == 2
    assert "data-poster-item-id=\"42\"" in html
    assert "data-poster-item-id=\"\"" in html
    assert "Undefined" not in html
    assert "None" not in html

    assert "/api/kids/sources/${encodeURIComponent(stateAtRequest.sourceId)}/poster-items" in html
    assert "/api/kids/sources/${stateAtSave.sourceId}/poster" in html
    assert "item_id: itemId" in html
    assert "savePoster(null)" in html
    assert "reason: 'Parent poster selection'" in html


def test_sources_template_has_single_filter_initialization_path_and_responsive_poster_css():
    html = render_sources([source_row(1, "UC-real")])
    css = (Path(__file__).resolve().parents[1] / "app" / "static" / "styles.css").read_text()

    assert "if (document.readyState === 'loading')" in html
    assert "window.addEventListener('DOMContentLoaded', initFilters, {once: true});" in html
    assert "if (document.readyState !== 'loading') initFilters();" not in html
    assert "source-reference" in html
    assert "aspect-ratio: 2 / 3" in css
    assert ".source-avatar" in css
    assert ".poster-dialog" in css
    assert ".poster-dialog:not([open])" in css
    assert ".poster-option" in css
    assert "@media (max-width: 700px)" in css
