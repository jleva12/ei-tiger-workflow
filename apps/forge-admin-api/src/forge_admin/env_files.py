"""The .env file settings are read from, and the values it shares with the
other apps by reference.

``${NAME}`` in an unquoted or double-quoted value of .env is NAME from an
earlier line of .env, or else from the shared file: FORGE_ENV_COMMON_FILE
when that is set in the process environment (empty disables it), or else
../../.env.common, the repository root's, when it exists. Single-quoted
values are literal. The shared file is only a source for references: nothing
in it reaches the settings unless .env references it, and the process
environment takes no part in resolving them. Its own values may reference
its earlier lines.

A reference that nothing defines stops startup, unless the variable holding
it is set in the process environment, whose value wins anyway: Compose
resolves the references itself and passes the results. Errors name
variables and files, never values.

The async worker (apps/forge-async-worker, forge_tasks.env_files) has the same
loader; the apps share no Python package.
"""

import os
import re
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import NamedTuple, Self

from dotenv.main import with_warn_for_invalid_lines
from dotenv.parser import parse_stream
from pydantic_settings import (
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsError,
)
from pydantic_settings.sources.utils import parse_env_vars

# Names the shared file; set but empty, there is none.
COMMON_FILE_VARIABLE = "FORGE_ENV_COMMON_FILE"
# The repository root's, from the app's directory.
DEFAULT_COMMON_FILE = Path("../../.env.common")

_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# A variable whose value is single-quoted, in python-dotenv's grammar
# (dotenv.parser): leading blank lines, an optional "export", the name, "=".
_SINGLE_QUOTED = re.compile(
    r"\s*(?:export[^\S\r\n]+)?(?:'[^']+'|[^=#\s]+)[^\S\r\n]*=[^\S\r\n]*'"
)


class CommonFile(NamedTuple):
    """
    The shared file, as .env references see it.

    :ivar values: Its values by name, their own references resolved.
    :ivar missing: How errors say that a name is defined nowhere.
    """

    values: Mapping[str, str]
    missing: str


def read_common_file(encoding: str = "utf8") -> CommonFile:
    """
    :return: The shared file named by FORGE_ENV_COMMON_FILE or, when that is
        unset, ../../.env.common; no values when there is none.
    :raises SettingsError: When FORGE_ENV_COMMON_FILE names a file that
        doesn't exist, or the file references a name no earlier line of it
        defines.
    """
    configured = os.environ.get(COMMON_FILE_VARIABLE)
    if configured == "":
        return CommonFile(
            {}, f"no earlier line defines ({COMMON_FILE_VARIABLE} is empty)"
        )
    if configured is None:
        path = DEFAULT_COMMON_FILE
        missing = (
            "neither .env.common nor an earlier line defines "
            "(make env creates .env.common)"
        )
        if not path.exists():
            return CommonFile({}, missing)
    else:
        path = Path(configured)
        missing = f"neither {configured} nor an earlier line defines"
        if not path.exists():
            raise SettingsError(
                f"{COMMON_FILE_VARIABLE} names {configured}, which doesn't exist"
            )
    values = resolve_file(
        path, encoding=encoding, shared={}, missing="no earlier line defines"
    )
    return CommonFile(
        {name: value for name, value in values.items() if value is not None},
        missing,
    )


def resolve_file(
    path: Path,
    *,
    encoding: str,
    shared: Mapping[str, str],
    missing: str,
    skip: Callable[[str], bool] = lambda variable: False,
) -> dict[str, str | None]:
    """
    :param path: A .env file.
    :param shared: The values references fall back to after the file's
        earlier lines.
    :param missing: How the error says that a name is defined nowhere.
    :param skip: Whether to leave out, rather than fail on, a variable whose
        reference nothing defines.
    :return: The file's values by variable, references resolved; None for a
        name without "=".
    :raises SettingsError: When a reference is defined nowhere.
    """
    values: dict[str, str | None] = {}

    def lookup(name: str) -> str | None:
        earlier = values.get(name)
        return earlier if earlier is not None else shared.get(name)

    for variable, value, literal in _bindings(path, encoding):
        if value is not None and not literal:
            names = _REFERENCE.findall(value)
            undefined = next((name for name in names if lookup(name) is None), None)
            if undefined is not None:
                if not skip(variable):
                    raise SettingsError(
                        f"{variable} in {path} references ${{{undefined}}}, "
                        f"which {missing}"
                    )
                values.pop(variable, None)
                continue
            value = _REFERENCE.sub(lambda match: lookup(match[1]) or "", value)
        values[variable] = value
    return values


def environment(
    env_file: Path | None = Path(".env"), encoding: str = "utf8"
) -> dict[str, str]:
    """
    The process environment over .env, as settings read them but by every
    name, not only FORGE_ADMIN_ ones: for configuration files whose ${NAME}
    references aren't settings, e.g. the model provider configuration's
    ${OPENAI_API_KEY}.

    :param env_file: The .env file; None, or a file that doesn't exist, for the
        process environment alone.
    :raises SettingsError: As settings do, for a reference defined nowhere.
    """
    values: dict[str, str | None] = {}
    if env_file is not None and env_file.exists():
        common = read_common_file(encoding)
        values = resolve_file(
            env_file,
            encoding=encoding,
            shared=common.values,
            missing=common.missing,
            skip=lambda variable: variable in os.environ,
        )
    return {
        **{name: value for name, value in values.items() if value is not None},
        **os.environ,
    }


def _bindings(path: Path, encoding: str) -> Iterator[tuple[str, str | None, bool]]:
    """
    :return: Each variable the file sets, in order, as python-dotenv reads
        it: its name, its value and whether that is single-quoted.
    """
    with path.open(encoding=encoding) as stream:
        for binding in with_warn_for_invalid_lines(parse_stream(stream)):
            if binding.key is not None:
                literal = _SINGLE_QUOTED.match(binding.original.string) is not None
                yield binding.key, binding.value, literal


class EnvFileSettingsSource(DotEnvSettingsSource):
    """pydantic-settings' .env source, with the references resolved."""

    _common: CommonFile

    @classmethod
    def replacing(cls, source: PydanticBaseSettingsSource) -> Self:
        """
        :param source: pydantic-settings' own .env source, as
            ``settings_customise_sources`` receives it.
        :return: One reading the same files, e.g. a ``_env_file`` argument's.
        """
        if not isinstance(source, DotEnvSettingsSource):
            raise TypeError(f"expected a DotEnvSettingsSource, got {source!r}")
        return cls(
            source.settings_cls,
            env_file=source.env_file,
            env_file_encoding=source.env_file_encoding,
            dotenv_filtering=source.dotenv_filtering,
            case_sensitive=source.case_sensitive,
            env_prefix=source.env_prefix,
            env_prefix_target=source.env_prefix_target,
            env_nested_delimiter=source.env_nested_delimiter,
            env_nested_max_split=source.env_nested_max_split,
            env_ignore_empty=source.env_ignore_empty,
            env_parse_none_str=source.env_parse_none_str,
            env_parse_enums=source.env_parse_enums,
            _init_state=source._init_state,
        )

    def _read_env_files(self) -> Mapping[str, str | None]:
        if self.env_file is None:
            return {}
        # Before .env, so a wrong FORGE_ENV_COMMON_FILE fails without one too.
        self._common = read_common_file(self.env_file_encoding or "utf8")
        return super()._read_env_files()

    def _read_env_file(self, file_path: Path) -> Mapping[str, str | None]:
        values = resolve_file(
            file_path,
            encoding=self.env_file_encoding or "utf8",
            shared=self._common.values,
            missing=self._common.missing,
            skip=self._set_in_environment,
        )
        return parse_env_vars(
            values, self.case_sensitive, self.env_ignore_empty, self.env_parse_none_str
        )

    def _set_in_environment(self, variable: str) -> bool:
        # As the process environment's source reads it.
        environment = parse_env_vars(
            os.environ, self.case_sensitive, self.env_ignore_empty
        )
        return self._apply_case_sensitive(variable) in environment
