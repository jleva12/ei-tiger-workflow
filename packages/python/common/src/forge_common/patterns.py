"""
Regular expressions that come with documents, a JSON Schema's ``pattern`` or a
JSONata ``/.../``, on the ``regex`` engine rather than Python's ``re``.

``re`` matches in C holding the interpreter lock for as long as a pattern
backtracks: for ``^(a|aa)+$`` on a long enough string, for ever, and nothing
else of the process runs meanwhile, neither its event loop nor its other
threads. Here every match lets go of the lock while it runs (``concurrent``),
and stops once it has taken ``seconds`` (all of one iteration's matching,
lazily or not), raising :class:`PatternTimeout`, a ``ValueError``: what it
fails isn't a hiccup to try again. Compiling isn't bounded so: it holds the
lock, and takes the engine time in proportion to the parts a pattern comes to,
each repeat counted as many times as it must at least match, nested repeats
multiplying (``((a{1000}){1000}){1000}`` would take minutes, ``re`` reads it at
once). A pattern that comes to more than ``MAX_COMPILED_SIZE`` parts is refused
before it's compiled.

A JSON Schema that comes with a document is checked with :func:`schema_errors`,
on :class:`SchemaValidator`: draft 2020-12's validator with every keyword that
matches a pattern matching here (``pattern``, ``patternProperties``, and the
properties ``additionalProperties`` and ``unevaluatedProperties`` leave to
it), whatever draft a subschema names (``$schema``), where jsonschema's own
match with ``re`` and check such a subschema with that draft's validator. All
of a value's patterns take ``SCHEMA_SECONDS`` at most. Its references
(``$ref``, ``$dynamicRef``) are looked up in it, and in the drafts'
meta-schemas, only: jsonschema's own validators fetch any other from wherever
it points (``urlopen``, with no timeout), so a schema could have the process
request any URL, a cloud's metadata service among them, and wait on it for
ever. One that can't be followed refuses the value, where jsonschema's raise:
one that isn't a string, can't be resolved or isn't to a schema, and one that
refers back to itself without going into the value, which jsonschema's follow
until the stack runs out. So does a value, or schema, nested deeper than the
stack goes.

A value's whole check takes ``SCHEMA_SECONDS`` at most, not only its
patterns: a schema can make jsonschema's work double with each level it nests
(an ``anyOf`` of two references to the next level, say), so once the time's up
what's left of the check isn't run, and the value's refused.

Needs ``forge-common[patterns]``.
"""

from __future__ import annotations

import functools
import re
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import referencing
import regex
from jsonschema import Draft202012Validator, ValidationError, validators
from referencing.exceptions import Unresolvable
from referencing.jsonschema import SchemaRegistry

#: The most seconds checking a value against a JSON Schema takes with
#: :func:`schema_errors`, all of its patterns among it; with
#: ``SchemaValidator``'s ``iter_errors``, the most each pattern takes. A value
#: is checked where it arrives, an event loop among them, so briefly.
SCHEMA_SECONDS = 0.5
#: The most parts a pattern may come to, each repeat counted its least number
#: of times (:func:`_compiled_size`): compiling holds the interpreter lock for
#: as long as it takes, which grows with them.
MAX_COMPILED_SIZE = 100_000
#: A replacement's group reference, as forge_jsonata writes ``$1``.
_GROUP_REFERENCE = re.compile(r"\\g<(\d+)>")
#: A counted repeat as the engine reads one (VERSION0): {2}, {2,}, {2,5}, {,5}
#: or {,}, its least count the first group; {} is two characters.
_COUNTED = re.compile(r"\{(?:(\d+)(?:,\d*)?|,\d*)\}")
#: A set's POSIX class, [:alpha:] or [:^alpha:], which is one member of it.
_POSIX_CLASS = re.compile(r"\[:\^?\w+:\]")
#: An inline flags group's flags, after its "(?": (?i), (?i-s:...), (?V0); not
#: a call, (?1) or (?R).
_INLINE_FLAGS = re.compile(r"(?:[abefiLmprsuwx-]|V[01])*")
#: Where a schema's references are looked up: it holds nothing and fetches
#: nothing, so they resolve within the schema, or to the drafts' meta-schemas
#: jsonschema adds to any registry a validator is built with.
_OFFLINE: SchemaRegistry = referencing.Registry()


class PatternTimeout(ValueError):
    """A regular expression went on past the time it had."""


class InvalidPattern(ValueError):
    """A regular expression that doesn't compile."""


@dataclass(frozen=True)
class BoundedPattern:
    """
    A compiled regular expression, as forge_jsonata uses one
    (``regex_engine.CompiledPattern``), each use bounded in time; it reads
    like Python's ``re.Pattern`` (``pattern``, ``groups``).

    :ivar native: The ``regex`` engine's pattern.
    :ivar seconds: The most one use may take.
    """

    native: Any
    seconds: float

    @property
    def pattern(self) -> str:
        return str(self.native.pattern)

    @property
    def groups(self) -> int:
        return int(self.native.groups)

    def search(self, string: str) -> Any:
        return self._bounded(self.native.search, string)

    def finditer(self, string: str) -> Iterator[Any]:
        return self._iterated(self.native.finditer(string, concurrent=True, timeout=self.seconds))

    def sub(self, repl: Any, string: str, count: int = 0) -> str:
        if isinstance(repl, str):
            self._check_references(repl)
        return str(self._bounded(self.native.sub, repl, string, count=count))

    def split(self, string: str, maxsplit: int = 0) -> list[str]:
        return list(self._bounded(self.native.split, string, maxsplit=maxsplit))

    def __str__(self) -> str:
        return f"/{self.pattern}/"

    def _bounded(self, call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return call(*args, concurrent=True, timeout=self.seconds, **kwargs)
        except TimeoutError:
            raise self._timeout() from None

    def _iterated(self, found: Iterator[Any]) -> Iterator[Any]:
        # Matched as it's iterated: a timeout comes from next().
        while True:
            try:
                match = next(found)
            except StopIteration:
                return
            except TimeoutError:
                raise self._timeout() from None
            yield match

    def _check_references(self, template: str) -> None:
        """Python's ``re`` refuses a replacement naming a group the pattern
        hasn't, saying which: forge_jsonata reads that to take ``$12`` with
        one group as group 1 and a 2, as JSONata does. ``regex`` doesn't say
        which, so this says it as ``re`` would."""
        for reference in _GROUP_REFERENCE.finditer(template):
            group, position = reference.group(1), reference.start() + 3
            if int(group) > self.groups:
                raise re.error(f"invalid group reference {group} at position {position}")

    def _timeout(self) -> PatternTimeout:
        return PatternTimeout(
            f"its regular expression {self} took longer than {self.seconds * 1000:g} ms"
        )


def compiled(
    pattern: str, *, seconds: float, ignore_case: bool = False, multiline: bool = False
) -> BoundedPattern:
    """
    :return: ``pattern`` compiled, each use taking ``seconds`` at most.
    :raises InvalidPattern: It doesn't compile, or would take too long to
        (more than ``MAX_COMPILED_SIZE`` parts).
    """
    if not isinstance(pattern, str):
        raise InvalidPattern(f"its regular expression {pattern!r} isn't text")
    return _compiled(pattern, seconds, ignore_case, multiline)


@functools.lru_cache(maxsize=512)
def _compiled(pattern: str, seconds: float, ignore_case: bool, multiline: bool) -> BoundedPattern:
    flags = regex.VERSION0  # as Python's re reads a pattern
    if ignore_case:
        flags |= regex.IGNORECASE
    if multiline:
        flags |= regex.MULTILINE
    refused = _too_big(pattern)
    if refused:
        raise InvalidPattern(f"its regular expression {_shown(pattern)} doesn't compile: {refused}")
    try:
        return BoundedPattern(regex.compile(pattern, flags), seconds)
    except Exception as error:  # noqa: BLE001 - however the engine refuses it
        raise InvalidPattern(
            f"its regular expression /{pattern}/ doesn't compile: {error}"
        ) from None


def _too_big(pattern: str) -> str | None:
    """
    :return: Why ``pattern`` mustn't be compiled: it comes to more than
        ``MAX_COMPILED_SIZE`` parts, or turns on what :func:`_compiled_size`
        doesn't read; None when it may be.
    """
    if len(pattern) > MAX_COMPILED_SIZE:
        return f"it's longer than {MAX_COMPILED_SIZE:,} characters"
    try:
        size = _compiled_size(pattern)
    except ValueError as error:
        return str(error)
    if size > MAX_COMPILED_SIZE:
        return f"its repeats come to more than {MAX_COMPILED_SIZE:,} parts"
    return None


def _compiled_size(pattern: str) -> int:
    """
    How many parts ``pattern`` comes to compiled, at least as many as the
    engine lays out (VERSION0): each character one, a set or an escape one, a
    counted repeat its part as many times as its least count, a group's
    nested repeats multiplied. A pattern the engine refuses may be counted
    wrong: it never gets to the slow part.

    :raises ValueError: It turns on verbose mode or VERSION1, whose syntax
        (comments, nested sets) this doesn't read; neither JSON Schema's nor
        JSONata's regular expressions have them.
    """
    frames = [[0, 0]]  # each open group's [size, size of its last part]
    i, end = 0, len(pattern)
    while i < end:
        frame, char = frames[-1], pattern[i]
        if char == "\\":
            after = i + 2
            if pattern[i + 1 : i + 2] in ("p", "P", "N") and pattern.startswith("{", after):
                close = pattern.find("}", after)  # \p{Letter}: one part
                after = end if close < 0 else close + 1
            frame[0], frame[1], i = frame[0] + 1, 1, after
        elif char == "[":
            frame[0], frame[1], i = frame[0] + 1, 1, _set_end(pattern, i)
        elif char == "(" and pattern.startswith("(?#", i):
            i = _comment_end(pattern, i + 3)  # nothing, between a part and its repeat
        elif char == "(":
            if pattern.startswith("(?", i):
                flags = _INLINE_FLAGS.match(pattern, i + 2)
                found = flags.group() if flags else ""
                if "x" in found:
                    raise ValueError("it turns on verbose mode, (?x)")
                if "V1" in found:
                    raise ValueError("it turns on VERSION1, (?V1)")
                if flags and pattern.startswith(")", flags.end()):
                    i = flags.end() + 1  # (?i): nothing, as a comment is
                    continue
            frames.append([0, 0])
            i += 1
        elif char == ")" and len(frames) > 1:
            size = frames.pop()[0]
            frames[-1][0] += size
            frames[-1][1] = size
            i += 1
        elif char == "{" and frame[1] and (counted := _COUNTED.match(pattern, i)):
            least = int(counted.group(1) or 0)
            if least > 1:
                frame[0] += frame[1] * (least - 1)
                frame[1] *= least
            i = counted.end()
        else:
            frame[0] += 1
            frame[1] = 0 if char == "|" else 1  # after |, nothing to repeat
            i += 1
    while len(frames) > 1:  # a group left open: the engine refuses it
        size = frames.pop()[0]
        frames[-1][0] += size
    return frames[0][0]


def _set_end(pattern: str, start: int) -> int:
    """:return: Just past the set at ``start`` (``[...]``), as the engine reads
    one (VERSION0): a ``]`` first is a member, as is anything escaped, and a
    POSIX class (``[:alpha:]``) is one member."""
    i, end = start + 1, len(pattern)
    if pattern.startswith("^", i):
        i += 1
    if pattern.startswith("]", i):
        i += 1
    while i < end:
        if pattern[i] == "]":
            return i + 1
        if pattern[i] == "\\":
            i += 2
        elif posix := _POSIX_CLASS.match(pattern, i):
            i = posix.end()
        else:
            i += 1
    return end


def _comment_end(pattern: str, start: int) -> int:
    """:return: Just past the comment whose text starts at ``start``
    (``(?#...)``), as the engine reads one: an escaped ``)`` doesn't end it."""
    i, end = start, len(pattern)
    while i < end:
        if pattern[i] == "\\":
            i += 2
        elif pattern[i] == ")":
            return i + 1
        else:
            i += 1
    return end


def _shown(pattern: str) -> str:
    # A pattern as a refusal names it: a long one cut short.
    return f"/{pattern}/" if len(pattern) <= 80 else f"/{pattern[:80]}…/"


def searches(pattern: str, string: str, *, seconds: float) -> bool:
    """
    :return: Whether ``pattern`` is found in ``string`` (a JSON Schema's ``pattern``).
    :raises InvalidPattern: It doesn't compile.
    :raises PatternTimeout: It took longer than ``seconds``.
    """
    return compiled(pattern, seconds=seconds).search(string) is not None


@dataclass
class _Check:
    """
    A value being checked against a schema (:func:`schema_errors`).

    :ivar deadline: When its time is up, its patterns' too
        (``time.monotonic()``).
    :ivar failed: The first of its patterns that couldn't be run (one that
        took too long, or doesn't compile) or references that can't be
        followed (:func:`_followed`), or its time running out
        (:func:`_in_time`).
    :ivar following: The references being followed (:func:`_followed`), the
        innermost last.
    """

    deadline: float
    failed: ValueError | None = None
    following: list[_Following] = field(default_factory=list)


#: The value being checked in this context, if any.
_CHECK: ContextVar[_Check | None] = ContextVar("forge_common_patterns_check", default=None)


def schema_errors(schema: Mapping[str, Any] | bool, instance: Any) -> list[ValidationError]:
    """
    How a value doesn't fit a JSON Schema that comes with a document: checked
    as draft 2020-12 (:class:`SchemaValidator`) in ``SCHEMA_SECONDS`` at most,
    all of its patterns among it.

    A pattern that couldn't be run (it took too long, or doesn't compile)
    refuses the value, even where a schema takes a pattern that doesn't match
    as fitting (``not``, ``anyOf``, ``if``): its error is among those returned.
    So does a reference (``$ref``, ``$dynamicRef``) that can't be followed
    (:func:`_followed`): one that isn't a string, that's to anywhere outside
    the schema but the drafts' meta-schemas (never fetched), to nowhere in it
    or to what isn't a schema, or that refers back to itself without going
    into the value.

    Once its time is up, the rest of the check isn't run (:func:`_in_time`):
    the value's refused, for the first pattern or reference that failed it
    if one did, else for taking too long. Nor is it once the check goes deeper
    than the stack does (a value, or schema, nested that deep): the value's
    refused the same way, else for being nested too deeply, that error alone.

    :param schema: The schema; ``{}`` takes anything.
    :param instance: The value.
    :return: Its errors, as ``iter_errors`` finds them in the time it has;
        empty when it fits.
    """
    check = _Check(deadline=time.monotonic() + SCHEMA_SECONDS)
    errors: list[ValidationError] = []
    token = _CHECK.set(check)
    try:
        for error in SchemaValidator(schema).iter_errors(instance):
            errors.append(error)
    except _OutOfTime:
        pass  # those found in time are kept, and why it stopped (check.failed)
    except RecursionError:
        # A value nested that deep under a schema that refers to itself, a
        # schema nested that deep, or references going round as _followed
        # doesn't tell: through other resources ($id), each time a dynamic
        # scope further out, or once what they're followed from found an
        # error, which each time round finds again: those found are dropped.
        errors = []
        _failed(ValueError("it, or its schema, is nested too deeply to be checked"))
    finally:
        _CHECK.reset(token)
    failed = check.failed
    if failed is not None and all(error.message != str(failed) for error in errors):
        errors.append(ValidationError(str(failed)))
    return errors


def _found(pattern: str, string: str) -> bool:
    """
    :return: Whether a schema's ``pattern`` is found in ``string``, in the time
        the value being checked has left (:func:`schema_errors`), else in
        ``SCHEMA_SECONDS``.
    :raises InvalidPattern: It doesn't compile.
    :raises PatternTimeout: It took longer.
    :raises _OutOfTime: The value's time was up before it ran (:func:`_in_time`).
    """
    check = _CHECK.get()
    try:
        if check is None:
            return searches(pattern, string, seconds=SCHEMA_SECONDS)
        _in_time()
        bounded = compiled(pattern, seconds=SCHEMA_SECONDS)
        left = check.deadline - time.monotonic()
        if left > 0:
            try:
                return bounded.native.search(string, concurrent=True, timeout=left) is not None
            except TimeoutError:
                pass
        raise PatternTimeout(
            f"its regular expression {bounded} took longer than the "
            f"{SCHEMA_SECONDS * 1000:g} ms a value's regular expressions get"
        )
    except (PatternTimeout, InvalidPattern) as error:
        _failed(error)
        raise


def _failed(error: ValueError) -> None:
    # What couldn't be run, if it's the first: it refuses the value being
    # checked wherever it was (schema_errors).
    check = _CHECK.get()
    if check is not None and check.failed is None:
        check.failed = error


class _OutOfTime(Exception):
    """A value's check (:func:`schema_errors`) has had its time (:func:`_in_time`)."""


def _in_time() -> None:
    """
    That the value being checked (:func:`schema_errors`) has time left for
    more of its check: a schema can make jsonschema's work double with each
    level it nests (an ``anyOf`` of two references to the next level, say).

    :raises _OutOfTime: It hasn't. The rest of its check isn't run, and the
        value's refused for the first pattern or reference that failed it
        (:func:`_failed`), else for taking too long.
    """
    check = _CHECK.get()
    if check is not None and time.monotonic() >= check.deadline:
        late = f"checking it took longer than the {SCHEMA_SECONDS * 1000:g} ms a value gets"
        _failed(ValueError(late))
        raise _OutOfTime


def _covers(pattern: str, name: str) -> bool:
    # Whether a patternProperties pattern covers a property. One it can't
    # tell does: the value's refused for it all the same (patternProperties,
    # schema_errors), and saying the property's uncovered would be a guess.
    try:
        return _found(pattern, name)
    except (PatternTimeout, InvalidPattern):
        return True


def _valid(errors: Iterator[ValidationError]) -> bool:
    return next(errors, None) is None


def _unexpected(names: list[Any]) -> str:
    # Properties as jsonschema names them in a refusal: 'a' was, 'a', 'b' were.
    return f"{', '.join(repr(name) for name in names)} {'was' if len(names) == 1 else 'were'}"


def _pattern(validator: Any, pattern: str, instance: Any, schema: Any) -> Iterator[ValidationError]:
    # jsonschema's pattern keyword, on the bounded engine.
    if not validator.is_type(instance, "string"):
        return
    try:
        found = _found(pattern, instance)
    except (PatternTimeout, InvalidPattern) as error:
        yield ValidationError(str(error))
        return
    if not found:
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


def _pattern_properties(
    validator: Any, patterns: Any, instance: Any, schema: Any
) -> Iterator[ValidationError]:
    # jsonschema's patternProperties keyword, on the bounded engine.
    if not validator.is_type(instance, "object"):
        return
    for pattern, subschema in patterns.items():
        for name, value in instance.items():
            try:
                found = _found(pattern, name)
            except (PatternTimeout, InvalidPattern) as error:
                yield ValidationError(str(error))
                continue
            if found:
                yield from validator.descend(value, subschema, path=name, schema_path=pattern)


def _additional_properties(
    validator: Any, additional: Any, instance: Any, schema: Any
) -> Iterator[ValidationError]:
    # jsonschema's additionalProperties keyword, the properties
    # patternProperties covers told on the bounded engine.
    if not validator.is_type(instance, "object"):
        return
    properties = schema.get("properties", {})
    patterns = schema.get("patternProperties", {})
    extras = {
        name
        for name in instance
        if name not in properties and not any(_covers(pattern, name) for pattern in patterns)
    }
    if validator.is_type(additional, "object"):
        for extra in extras:
            yield from validator.descend(instance[extra], additional, path=extra)
    elif not additional and extras:
        if "patternProperties" in schema:
            verb = "does" if len(extras) == 1 else "do"
            joined = ", ".join(repr(each) for each in sorted(extras))
            listed = ", ".join(repr(each) for each in sorted(patterns))
            yield ValidationError(f"{joined} {verb} not match any of the regexes: {listed}")
        else:
            unexpected = _unexpected(sorted(extras, key=str))
            yield ValidationError(
                f"Additional properties are not allowed ({unexpected} unexpected)"
            )


def _unevaluated_properties(
    validator: Any, unevaluated: Any, instance: Any, schema: Any
) -> Iterator[ValidationError]:
    # jsonschema's unevaluatedProperties keyword, the properties
    # patternProperties evaluates told on the bounded engine.
    if not validator.is_type(instance, "object"):
        return
    evaluated = _evaluated_keys(validator, instance, schema)
    invalid = [
        name
        for name in instance
        if name not in evaluated
        for _ in validator.descend(instance[name], unevaluated, path=name, schema_path=name)
    ]
    if not invalid:
        return
    if unevaluated is False:
        unexpected = _unexpected(sorted(invalid, key=str))
        yield ValidationError(f"Unevaluated properties are not allowed ({unexpected} unexpected)")
    else:
        yield ValidationError(
            "Unevaluated properties are not valid under the given schema "
            f"({_unexpected(invalid)} unevaluated and invalid)"
        )


def _evaluated_keys(validator: Any, instance: Any, schema: Any) -> list[str]:
    # jsonschema's find_evaluated_property_keys_by_schema (_utils), the
    # properties patternProperties evaluates told on the bounded engine.
    if validator.is_type(schema, "boolean"):
        return []
    evaluated: list[str] = []
    for keyword in ("$ref", "$dynamicRef"):
        if keyword in schema:
            try:
                resolved, followed = _followed(
                    validator, keyword, schema[keyword], instance, "properties"
                )
            except ValueError:
                # Which it evaluates can't be told: the value's refused for it
                # all the same, and saying one's unevaluated would be a guess.
                return list(instance)
            evolved = validator.evolve(schema=resolved.contents, _resolver=resolved.resolver)
            with _following(followed):
                evaluated += _evaluated_keys(evolved, instance, resolved.contents)
    properties = schema.get("properties")
    if validator.is_type(properties, "object"):
        evaluated += properties.keys() & instance.keys()
    for keyword in ("additionalProperties", "unevaluatedProperties"):
        subschema = schema.get(keyword)
        if subschema is not None:
            evaluated += (
                name
                for name, value in instance.items()
                if _valid(validator.descend(value, subschema))
            )
    if "patternProperties" in schema:
        for name in instance:
            for pattern in schema["patternProperties"]:
                if _covers(pattern, name):
                    evaluated.append(name)
    for name, subschema in schema.get("dependentSchemas", {}).items():
        if name in instance:
            evaluated += _evaluated_keys(validator, instance, subschema)
    for keyword in ("allOf", "oneOf", "anyOf"):
        for subschema in schema.get(keyword, []):
            if _valid(validator.descend(instance, subschema)):
                evaluated += _evaluated_keys(validator, instance, subschema)
    if "if" in schema:
        if validator.evolve(schema=schema["if"]).is_valid(instance):
            evaluated += _evaluated_keys(validator, instance, schema["if"])
            if "then" in schema:
                evaluated += _evaluated_keys(validator, instance, schema["then"])
        elif "else" in schema:
            evaluated += _evaluated_keys(validator, instance, schema["else"])
    return evaluated


def _ref(validator: Any, ref: Any, instance: Any, schema: Any) -> Iterator[ValidationError]:
    yield from _reference(validator, "$ref", ref, instance)


def _dynamic_ref(validator: Any, ref: Any, instance: Any, schema: Any) -> Iterator[ValidationError]:
    yield from _reference(validator, "$dynamicRef", ref, instance)


def _reference(validator: Any, keyword: str, ref: Any, instance: Any) -> Iterator[ValidationError]:
    # jsonschema's $ref and $dynamicRef keywords (_validate_reference), one
    # that can't be followed refusing the value wherever it is, as a pattern
    # that can't be run does, where jsonschema's raise or go round for ever.
    try:
        resolved, followed = _followed(validator, keyword, ref, instance, "check")
    except ValueError as refused:
        yield ValidationError(str(refused))
        return
    errors = validator.descend(instance, resolved.contents, resolver=resolved.resolver)
    check = _CHECK.get()
    following = [] if check is None else check.following
    while True:
        # Followed while its subschema's checked, not while an error it found
        # waits to be taken: what's checked meanwhile isn't inside it. As
        # _following does, without its cost for each error.
        following.append(followed)
        try:
            error = next(errors, None)
        finally:
            following.pop()
        if error is None:
            return
        followed.found += 1
        yield error


@dataclass
class _Following:
    """
    A reference being followed on a value (:func:`_followed`).

    :ivar where: The value, the subschema, the dynamic scope, and what for:
        the value's check, or the properties or items the subschema
        evaluates. Followed there for that again, it does just the same.
    :ivar found: The errors the value's check has found in it so far.
    """

    where: tuple[Any, ...]
    found: int = 0


def _followed(
    validator: Any, keyword: str, ref: Any, instance: Any, purpose: str
) -> tuple[Any, _Following]:
    """
    A schema's reference (``$ref``, ``$dynamicRef``), to be followed on a value.

    :param purpose: What for: ``"check"``, ``"properties"`` or ``"items"``
        (:class:`_Following`).
    :return: What it resolves to (``referencing``'s ``Resolved``), and its
        following, for :func:`_following`.
    :raises ValueError: It can't be followed, which refuses the value wherever
        it is (:func:`schema_errors`): it isn't a string; it's to anywhere but
        the schema and the drafts' meta-schemas, or to nowhere in them; it's
        to what isn't a schema; or it refers back to itself without going into
        the value: it's being followed there for the same already, which
        hasn't found an error yet, so would be again, the same way, for ever.
    """
    if not isinstance(ref, str):
        raise _refused(f"its {keyword} {ref!r} isn't a reference")
    try:
        resolved = validator._resolver.lookup(ref)
    except (Unresolvable, AttributeError, TypeError, ValueError):
        # Or a pointer through what has no such part (#/allOf/x,
        # #/minLength/x), a URL that doesn't parse, or a schema too malformed
        # to search ("$defs": 5).
        raise _refused(
            f"its {keyword} {ref!r} can't be resolved: only references within the schema are read"
        ) from None
    if not isinstance(resolved.contents, (Mapping, bool)):
        raise _refused(f"its {keyword} {ref!r} doesn't refer to a schema")
    scope = (uri for uri, _ in resolved.resolver.dynamic_scope())
    where = (purpose, id(instance), id(resolved.contents), *scope)
    check = _CHECK.get()
    following = check.following if check else []
    inside = next((each for each in reversed(following) if each.where == where), None)
    # Once that's found an error, what takes only the first (not, if,
    # contains) may stop there rather than come round again: it's followed,
    # and if it does go round for ever, the stack runs out (schema_errors).
    if inside is not None and not inside.found:
        raise _refused(f"its {keyword} {ref!r} refers back to itself without going into the value")
    return resolved, _Following(where)


def _refused(message: str) -> ValueError:
    # A reference that can't be followed: it refuses the value being checked
    # wherever it is, as a pattern that can't be run does (_failed).
    refused = ValueError(message)
    _failed(refused)
    return refused


@contextmanager
def _following(followed: _Following) -> Iterator[None]:
    # A reference being followed (_followed), while it is.
    check = _CHECK.get()
    following = [] if check is None else check.following
    following.append(followed)
    try:
        yield
    finally:
        following.pop()


def _unevaluated_items(
    validator: Any, unevaluated: Any, instance: Any, schema: Any
) -> Iterator[ValidationError]:
    # jsonschema's unevaluatedItems keyword, the schema's references followed
    # as _evaluated_keys follows them.
    if not validator.is_type(instance, "array"):
        return
    evaluated = set(_evaluated_items(validator, instance, schema))
    extras = [item for index, item in enumerate(instance) if index not in evaluated]
    if extras:
        yield ValidationError(
            f"Unevaluated items are not allowed ({_unexpected(extras)} unexpected)"
        )


def _evaluated_items(validator: Any, instance: Any, schema: Any) -> list[int]:
    # jsonschema's find_evaluated_item_indexes_by_schema (_utils), the
    # schema's references followed as _evaluated_keys follows them.
    if validator.is_type(schema, "boolean"):
        return []
    if "items" in schema:
        return list(range(len(instance)))
    evaluated: list[int] = []
    for keyword in ("$ref", "$dynamicRef"):
        if keyword in schema:
            try:
                resolved, followed = _followed(
                    validator, keyword, schema[keyword], instance, "items"
                )
            except ValueError:
                return list(range(len(instance)))  # can't be told (_evaluated_keys)
            evolved = validator.evolve(schema=resolved.contents, _resolver=resolved.resolver)
            with _following(followed):
                evaluated += _evaluated_items(evolved, instance, resolved.contents)
    if "prefixItems" in schema:
        evaluated += range(len(schema["prefixItems"]))
    if "if" in schema:
        if validator.evolve(schema=schema["if"]).is_valid(instance):
            evaluated += _evaluated_items(validator, instance, schema["if"])
            if "then" in schema:
                evaluated += _evaluated_items(validator, instance, schema["then"])
        elif "else" in schema:
            evaluated += _evaluated_items(validator, instance, schema["else"])
    for keyword in ("contains", "unevaluatedItems"):
        if keyword in schema:
            evolved = validator.evolve(schema=schema[keyword])
            evaluated += (index for index, item in enumerate(instance) if evolved.is_valid(item))
    for keyword in ("allOf", "oneOf", "anyOf"):
        for subschema in schema.get(keyword, []):
            if _valid(validator.descend(instance, subschema)):
                evaluated += _evaluated_items(validator, instance, subschema)
    return evaluated


def _offline(self: Any, *args: Any, registry: SchemaRegistry = _OFFLINE, **kwargs: Any) -> None:
    # jsonschema's validators fetch a reference they don't hold unless
    # they're built with a registry: these never do.
    _jsonschema_init(self, *args, registry=registry, **kwargs)


def _evolved(self: Any, **changes: Any) -> Any:
    # jsonschema checks each subschema with a validator evolve() makes, and
    # for one naming its draft ("$schema") that's the draft's own, whose
    # keywords match with re. Here each is checked as draft 2020-12, as the
    # schema it's in, on the bounded engine; and none once the value's time
    # is up (schema_errors), however many times a schema has it check one.
    _in_time()
    schema = changes.get("schema", self.schema)
    if isinstance(schema, Mapping) and "$schema" in schema:
        changes["schema"] = {key: value for key, value in schema.items() if key != "$schema"}
    return _jsonschema_evolve(self, **changes)


def _iter_errors(self: Any, instance: Any, _schema: Any = None) -> Iterator[ValidationError]:
    # jsonschema's iter_errors, which is_valid() checks a value with: one
    # validator checks each of an array's items so (contains). Not once the
    # value's time is up (schema_errors).
    _in_time()
    return _jsonschema_iter_errors(self, instance, _schema)


#: JSON Schema draft 2020-12's validator, every keyword that matches a pattern
#: on the bounded engine (SCHEMA_SECONDS each), whatever draft a subschema
#: names, its references resolved within it and the drafts' meta-schemas
#: unless it's built with a ``registry``: for a schema that comes with a
#: document. Check a value with :func:`schema_errors`, which holds all of its
#: check to that time, its patterns among it, and refuses it for one that
#: can't be run or a reference that can't be followed wherever they are, one
#: that refers back to itself among them, for nesting deeper than the stack
#: goes, and for running out of time.
SchemaValidator = validators.extend(
    Draft202012Validator,
    {
        "pattern": _pattern,
        "patternProperties": _pattern_properties,
        "additionalProperties": _additional_properties,
        "unevaluatedProperties": _unevaluated_properties,
        "unevaluatedItems": _unevaluated_items,
        "$ref": _ref,
        "$dynamicRef": _dynamic_ref,
    },
)
_jsonschema_evolve = SchemaValidator.evolve
SchemaValidator.evolve = _evolved
_jsonschema_init = SchemaValidator.__init__
SchemaValidator.__init__ = _offline
_jsonschema_iter_errors = SchemaValidator.iter_errors
SchemaValidator.iter_errors = _iter_errors
