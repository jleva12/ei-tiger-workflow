"""Jinja templates rendered to HTML, by a service an app builds once and injects
into whatever needs it. Needs ``forge-common[templates]``.

:class:`TemplateEngine` reads the templates in a directory (or several, or a
package's ``templates`` folder) and fills one with a dictionary::

    templates = TemplateEngine("templates")
    html = templates.render("emails/welcome.html", {"user": {"name": "Ana"}})

Templates name each other by their path in the directory, so they can
``{% extends "layouts/base.html" %}`` and ``{% include "partials/footer.html" %}``;
no name reaches outside it. The defaults are safe for data from users:

- HTML autoescaping: a value's ``<script>`` renders as text, and ``|safe`` (or
  ``markupsafe.Markup``) opts a trusted value out. Plain-text templates
  (``.txt``, ``.md``, also as ``.txt.j2``) aren't escaped.
- A value the dictionary lacks raises :class:`InvalidDataError` naming it,
  instead of rendering blank. Templates mark optional values with
  ``{{ x | default('') }}`` or ``{% if x is defined %}``.
- Jinja's immutable sandbox: a template can't reach Python internals through
  the objects it's given, or change them.

Rendering is CPU work; :meth:`TemplateEngine.render_async` runs it in a thread,
off the event loop.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any

import jinja2
from jinja2.sandbox import ImmutableSandboxedEnvironment, SecurityError
from pydantic import BaseModel

# Templates rendered as plain text, so never escaped.
_PLAIN_TEXT = (".txt", ".text", ".md", ".markdown")
_JINJA_SUFFIXES = (".j2", ".jinja", ".jinja2")

TemplateSource = str | os.PathLike[str] | Sequence[str | os.PathLike[str]] | jinja2.BaseLoader


class TemplateSettings(BaseModel):
    """An app's templates section, e.g. ``FORGE_ADMIN_TEMPLATES__DIRECTORY=/app/templates``."""

    directory: Path = Path("templates")
    # Re-read a template when its file changes; off saves a stat per render.
    auto_reload: bool = True
    # Raise on a value the data lacks, rather than render it blank.
    strict: bool = True
    # Jinja's immutable sandbox. Off only for trusted templates that call
    # methods on the objects they're given.
    sandboxed: bool = True


class TemplateRenderError(Exception):
    """A template couldn't be rendered; also raised for errors inside one,
    such as arithmetic on a string from the data.

    :param template: The template that failed, which may be one the rendered
        template extends or includes.
    :param line: Its line, when known.
    """

    def __init__(self, message: str, *, template: str | None = None, line: int | None = None):
        super().__init__(message)
        self.template = template
        self.line = line


class TemplateNotFoundError(TemplateRenderError):
    """No template has that name, or one it extends or includes is missing."""


class InvalidTemplateError(TemplateRenderError):
    """The template's Jinja doesn't parse."""


class InvalidDataError(TemplateRenderError):
    """The data lacks a value the template uses."""


class TemplateEngine:
    """Renders the Jinja templates in a directory to HTML.

    :param templates: The templates' directory, or several searched in order
        (an app's overrides before shared templates), or a Jinja loader, such
        as a ``DictLoader`` in tests.
    :param auto_reload: Re-read a template when its file changes.
    :param strict: Raise :class:`InvalidDataError` on a value the data lacks;
        off renders it (and anything read from it) blank.
    :param sandboxed: Render in Jinja's immutable sandbox.
    :param filters: Filters every template can use, by name.
    :param template_globals: Values every template sees, by name.
    :raises FileNotFoundError: If a directory doesn't exist.
    """

    def __init__(
        self,
        templates: TemplateSource = "templates",
        *,
        auto_reload: bool = True,
        strict: bool = True,
        sandboxed: bool = True,
        filters: Mapping[str, Callable[..., Any]] | None = None,
        template_globals: Mapping[str, Any] | None = None,
    ) -> None:
        if isinstance(templates, jinja2.BaseLoader):
            loader = templates
            self._roots: list[Path] = []
        else:
            paths = [templates] if isinstance(templates, str | os.PathLike) else list(templates)
            self._roots = [Path(p).resolve() for p in paths]
            for root in self._roots:
                if not root.is_dir():
                    raise FileNotFoundError(f"No template directory at {root}")
            loader = jinja2.FileSystemLoader(self._roots)
        environment = ImmutableSandboxedEnvironment if sandboxed else jinja2.Environment
        self._env = environment(
            loader=loader,
            autoescape=_autoescape,
            undefined=jinja2.StrictUndefined if strict else jinja2.ChainableUndefined,
            auto_reload=auto_reload,
            # A block tag's line leaves no blank line or indent in the HTML.
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._env.filters.update(filters or {})
        self._env.globals.update(template_globals or {})

    @classmethod
    def from_settings(cls, settings: TemplateSettings, **options: Any) -> TemplateEngine:
        """:param options: Any other :class:`TemplateEngine` argument, e.g. ``filters``."""
        return cls(
            settings.directory,
            auto_reload=settings.auto_reload,
            strict=settings.strict,
            sandboxed=settings.sandboxed,
            **options,
        )

    @classmethod
    def from_package(cls, package: str, path: str = "templates", **options: Any) -> TemplateEngine:
        """The templates shipped inside an installed package, wherever it's
        installed, e.g. ``TemplateEngine.from_package("forge_admin")``.

        :param options: Any other :class:`TemplateEngine` argument.
        """
        return cls(jinja2.PackageLoader(package, path), **options)

    @property
    def environment(self) -> jinja2.Environment:
        """The Jinja environment, to add tests, extensions and the like."""
        return self._env

    def render(self, template: str, context: Mapping[str, Any] | None = None) -> str:
        """Fills a template with data.

        :param template: Its path in the templates directory, e.g. ``emails/welcome.html``.
        :param context: The values it uses, by name.
        :return: The rendered HTML.
        :raises TemplateNotFoundError: If it, or a template it uses, doesn't exist.
        :raises InvalidTemplateError: If a template's Jinja doesn't parse.
        :raises InvalidDataError: If ``context`` lacks a value it uses.
        :raises TemplateRenderError: If it fails any other way.
        """
        if context is not None and not isinstance(context, Mapping):
            raise TypeError(f"Template context must be a mapping, not {type(context).__name__}")
        try:
            return self._env.get_template(template).render(context or {})
        except jinja2.TemplateNotFound as exc:
            missing = str(exc.name) if exc.name is not None else template
            via = f" (used by {template!r})" if missing != template else ""
            raise TemplateNotFoundError(f"No template {missing!r}{via}", template=missing) from exc
        except jinja2.TemplateSyntaxError as exc:
            name = exc.name or template
            raise InvalidTemplateError(
                f"{name}, line {exc.lineno}: {exc.message}", template=name, line=exc.lineno
            ) from exc
        except jinja2.UndefinedError as exc:
            name, line = self._where(exc.__traceback__, template)
            raise InvalidDataError(
                f"{_at(name, line)}: the data lacks a value: {exc.message}",
                template=name,
                line=line,
            ) from exc
        except SecurityError as exc:
            name, line = self._where(exc.__traceback__, template)
            raise TemplateRenderError(
                f"{_at(name, line)}: unsafe operation: {exc}", template=name, line=line
            ) from exc
        except Exception as exc:
            name, line = self._where(exc.__traceback__, template)
            raise TemplateRenderError(
                f"{_at(name, line)}: {type(exc).__name__}: {exc}", template=name, line=line
            ) from exc

    async def render_async(self, template: str, context: Mapping[str, Any] | None = None) -> str:
        """:meth:`render` in a thread, so a big template doesn't hold up the event loop."""
        return await asyncio.to_thread(self.render, template, context)

    def exists(self, template: str) -> bool:
        loader = self._env.loader
        if loader is None:
            return False
        try:
            loader.get_source(self._env, template)
        except jinja2.TemplateNotFound:
            return False
        return True

    def list_templates(self) -> list[str]:
        """Every template's name, sorted."""
        return sorted(self._env.list_templates())

    def _where(self, tb: TracebackType | None, template: str) -> tuple[str, int | None]:
        """The template and line an error was raised at. Jinja rewrites the
        traceback so a template's frames carry its file and line."""
        name, line = template, None
        while tb is not None:
            filename = tb.tb_frame.f_code.co_filename
            if not filename.endswith(".py") and not filename.startswith("<"):
                name, line = self._name_of(filename), tb.tb_lineno
            elif filename == "<template>":
                line = tb.tb_lineno
            tb = tb.tb_next
        return name, line

    def _name_of(self, filename: str) -> str:
        path = Path(filename)
        for root in self._roots:
            if path.is_relative_to(root):
                return path.relative_to(root).as_posix()
        return path.name


def _autoescape(name: str | None) -> bool:
    if name is None:  # a template from a string
        return True
    name = name.lower()
    for suffix in _JINJA_SUFFIXES:
        name = name.removesuffix(suffix)
    return not name.endswith(_PLAIN_TEXT)


def _at(template: str, line: int | None) -> str:
    return f"{template}, line {line}" if line else template
