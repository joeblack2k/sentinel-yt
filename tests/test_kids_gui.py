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
    assert "const labels = { learning: 'Leren & ontdekken', fun: 'Verhalen & plezier', again: 'Nog een keer' };" in html
    assert "const safeLabel = escapeHTML(labels[shelf.id] || 'Plank');" in html
