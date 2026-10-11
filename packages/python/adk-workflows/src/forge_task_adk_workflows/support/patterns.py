"""
JSONata's regular expressions (``/.../``) in a run, on forge-common's bounded
engine (``forge_common.patterns``) rather than Python's ``re``: each use lets
go of the interpreter lock while it matches and stops once it has taken its
time, raising ``PatternTimeout``, a ``ValueError``; one that doesn't compile,
or would hold the lock too long compiling, is ``InvalidPattern``. A JSON
Schema's patterns are checked there too (``schema_errors``).
"""

from collections.abc import Callable

from forge_common.patterns import BoundedPattern, compiled

from forge_jsonata.regex_engine import RegexFlags


def regex_engine(seconds: float) -> Callable[[str, RegexFlags], BoundedPattern]:
    """:return: forge_jsonata's ``regex_engine``: its regular expressions, each use taking ``seconds`` at most."""

    def compile_bounded(pattern: str, flags: RegexFlags) -> BoundedPattern:
        return compiled(pattern, seconds=seconds, ignore_case=flags.case_insensitive, multiline=flags.multiline)

    return compile_bounded
