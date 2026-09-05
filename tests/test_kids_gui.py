from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"

def render_kids(profiles=None):
    if profiles is None:
        profiles = [
            {"slug": "felix", "display_name": "Felix", "age_years": 2, "avatar_url": None, "approved_source_count": 5, "source_count": 10},
            {"slug": "noah", "display_name": "Noah", "age_years": 6, "avatar_url": None, "approved_source_count": 12, "source_count": 20},
        ]
    environment = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html", "xml"]),
    )
    return environment.get_template("kids.html").render(
        page="kids",
        profiles=profiles,
        kids_status={},
        sources=[],
        items=[]
    )

def test_kids_template_includes_shelves_preview_and_api_contract():
    html = render_kids()
    assert 'class="profile-shelves-preview"' in html
    assert 'data-slug="felix"' in html
    assert 'data-slug="noah"' in html

    assert 'fetch(`/v1/kids/shelves?profile=${encodeURIComponent(slug)}`)' in html
    assert "'lego-build': 'LEGO bouwen'" in html
    assert "'learning-nl': 'Leren & ontdekken (NL)'" in html
    assert "'fun-en': 'Engels & plezier (EN)'" in html
    assert "const safeLabel = escapeHTML(labels[shelf.id] || shelf.title || shelf.id);" in html


def test_kids_template_includes_editorial_classification_controls():
    items = [
        {"id": 42, "video_id": "testvideo123", "title": "Test Lego Video", "state": "approved", "source_id": 10, "thumbnail_url": None}
    ]
    environment = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html", "xml"]),
    )
    html = environment.get_template("kids.html").render(
        page="kids",
        profiles=[],
        kids_status={},
        sources=[],
        items=items
    )
    assert 'class="btn-classify secondary"' in html
    assert 'data-item-id="42"' in html
    assert 'id="editorial-row-42"' in html
    assert 'id="editorial-box-42"' in html
    assert 'for="editorial-audience-${itemId}"' in html
    assert 'id="editorial-audience-${itemId}"' in html
    assert 'for="editorial-category-${itemId}"' in html
    assert 'id="editorial-category-${itemId}"' in html
    assert 'for="editorial-reason-${itemId}"' in html
    assert 'id="editorial-reason-${itemId}"' in html
    assert 'maxlength="1000"' in html
    assert 'fetch(`/api/kids/items/${itemId}/editorial`)' in html
    assert 'method: \'PUT\'' in html
    assert 'target_audience:' in html
    assert 'lego_build' in html
    assert 'preschool' in html
    assert 'Peuter / Kleuter' in html
