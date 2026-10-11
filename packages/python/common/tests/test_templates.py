from __future__ import annotations

import os
from pathlib import Path

import jinja2
import pytest

from forge_common.templates import (
    InvalidDataError,
    InvalidTemplateError,
    TemplateEngine,
    TemplateNotFoundError,
    TemplateRenderError,
    TemplateSettings,
)


def write(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


@pytest.fixture
def templates(tmp_path: Path) -> Path:
    return write(
        tmp_path / "templates",
        {
            "layouts/base.html": (
                "<html><body>\n{% block body %}{% endblock %}\n"
                '{% include "partials/footer.html" %}</body></html>'
            ),
            "partials/footer.html": "<footer>{{ company }}</footer>",
            "emails/welcome.html": (
                '{% extends "layouts/base.html" %}\n'
                "{% block body %}\n"
                "<h1>Hi {{ user.name }}</h1>\n"
                "{% for item in items %}\n"
                "  <li>{{ item }}</li>\n"
                "{% endfor %}\n"
                "{% endblock %}"
            ),
            "emails/welcome.txt.j2": "Hi {{ user.name }} & welcome",
            "note.html": "<p>{{ note }}</p>{{ trusted | safe }}",
        },
    )


def test_renders_a_template_with_its_layout_and_includes(templates: Path) -> None:
    engine = TemplateEngine(templates)
    html = engine.render(
        "emails/welcome.html", {"user": {"name": "Ana"}, "items": ["a", "b"], "company": "Acme"}
    )
    # block tags leave no blank lines behind; the content's own indent stays
    assert html == (
        "<html><body>\n<h1>Hi Ana</h1>\n  <li>a</li>\n  <li>b</li>\n"
        "<footer>Acme</footer></body></html>"
    )


def test_escapes_html_but_not_plain_text_templates(templates: Path) -> None:
    engine = TemplateEngine(templates)
    html = engine.render("note.html", {"note": "<script>x()</script>", "trusted": "<b>ok</b>"})
    assert html == "<p>&lt;script&gt;x()&lt;/script&gt;</p><b>ok</b>"
    assert engine.render("emails/welcome.txt.j2", {"user": {"name": "<Bo>"}}) == "Hi <Bo> & welcome"


def test_a_missing_value_is_named(templates: Path) -> None:
    engine = TemplateEngine(templates)
    with pytest.raises(InvalidDataError) as caught:
        engine.render("emails/welcome.html", {"user": {"name": "Ana"}, "items": []})
    # the value the included footer reads, where it reads it
    assert caught.value.template == "partials/footer.html" and caught.value.line == 1
    assert "'company' is undefined" in str(caught.value)
    with pytest.raises(InvalidDataError, match="'dict object' has no attribute 'name'"):
        engine.render("emails/welcome.html", {"user": {}, "items": [], "company": "Acme"})


def test_lenient_engine_renders_missing_values_blank(templates: Path) -> None:
    engine = TemplateEngine(templates, strict=False)
    assert engine.render("emails/welcome.html").startswith("<html><body>\n<h1>Hi </h1>\n")


def test_missing_templates(templates: Path) -> None:
    engine = TemplateEngine(templates)
    with pytest.raises(TemplateNotFoundError, match="No template 'nope.html'"):
        engine.render("nope.html")
    # no name reaches outside the directory
    (templates.parent / "secret.html").write_text("secret")
    with pytest.raises(TemplateNotFoundError):
        engine.render("../secret.html")
    (templates / "partials/footer.html").unlink()
    with pytest.raises(TemplateNotFoundError) as caught:
        engine.render("emails/welcome.html", {"user": {"name": "A"}, "items": [], "company": "C"})
    assert caught.value.template == "partials/footer.html"
    assert "used by 'emails/welcome.html'" in str(caught.value)


def test_syntax_errors_name_the_line(templates: Path) -> None:
    write(templates, {"broken.html": "<p>ok</p>\n{% if x %}\n<p>never closed</p>"})
    with pytest.raises(InvalidTemplateError) as caught:
        TemplateEngine(templates).render("broken.html", {"x": 1})
    assert caught.value.template == "broken.html" and caught.value.line == 3
    assert str(caught.value).startswith("broken.html, line 3: Unexpected end of template")


def test_errors_inside_a_template_name_the_line(templates: Path) -> None:
    write(templates, {"math.html": "<p>{{ total }}</p>\n<p>{{ total + 1 }}</p>"})
    with pytest.raises(TemplateRenderError) as caught:
        TemplateEngine(templates).render("math.html", {"total": "12"})
    assert type(caught.value) is TemplateRenderError
    assert caught.value.line == 2 and "TypeError" in str(caught.value)


def test_the_sandbox_keeps_templates_from_python_internals_and_the_data(templates: Path) -> None:
    write(
        templates,
        {"escape.html": "{{ ''.__class__.__mro__ }}", "mutate.html": "{{ items.append(4) }}"},
    )
    engine = TemplateEngine(templates)
    with pytest.raises(TemplateRenderError, match="unsafe operation"):
        engine.render("mutate.html", {"items": [1, 2, 3]})
    with pytest.raises(TemplateRenderError, match="attribute '__class__' of 'str' object"):
        engine.render("escape.html")
    items = [1]
    assert TemplateEngine(templates, sandboxed=False).render("mutate.html", {"items": items})
    assert items == [1, 4]


def test_directories_are_searched_in_order(tmp_path: Path, templates: Path) -> None:
    overrides = write(tmp_path / "overrides", {"partials/footer.html": "<footer>custom</footer>"})
    engine = TemplateEngine([overrides, templates])
    html = engine.render("emails/welcome.html", {"user": {"name": "A"}, "items": []})
    assert html.endswith("<footer>custom</footer></body></html>")
    assert "emails/welcome.html" in engine.list_templates()
    assert engine.exists("partials/footer.html") and not engine.exists("nope.html")


def test_a_missing_directory_fails_at_construction(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="No template directory"):
        TemplateEngine(tmp_path / "missing")


def test_filters_globals_and_loaders() -> None:
    engine = TemplateEngine(
        jinja2.DictLoader({"price.html": "{{ amount | money }} {{ currency }}"}),
        filters={"money": lambda value: f"{value:,.2f}"},
        template_globals={"currency": "USD"},
    )
    assert engine.render("price.html", {"amount": 1234.5}) == "1,234.50 USD"
    assert engine.list_templates() == ["price.html"]


def test_from_settings_and_auto_reload(templates: Path) -> None:
    engine = TemplateEngine.from_settings(TemplateSettings(directory=templates))
    assert engine.render("note.html", {"note": "v1", "trusted": ""}) == "<p>v1</p>"
    path = templates / "note.html"
    path.write_text("<div>{{ note }}</div>")
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 5))  # a later mtime, whatever the clock
    assert engine.render("note.html", {"note": "v2"}) == "<div>v2</div>"


def test_from_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(
        tmp_path,
        {"shop/__init__.py": "", "shop/templates/receipt.html": "Paid {{ total }}"},
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    engine = TemplateEngine.from_package("shop")
    assert engine.render("receipt.html", {"total": 3}) == "Paid 3"


def test_context_must_be_a_mapping(templates: Path) -> None:
    with pytest.raises(TypeError, match="mapping"):
        TemplateEngine(templates).render("note.html", ["not", "a", "dict"])  # type: ignore[arg-type]


async def test_render_async(templates: Path) -> None:
    engine = TemplateEngine(templates)
    assert await engine.render_async("note.html", {"note": "hi", "trusted": ""}) == "<p>hi</p>"
