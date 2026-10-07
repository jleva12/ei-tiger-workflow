"""Impact: what would be affected by changing a declaration.

Dependants are walked backwards breadth-first from the root, and from what
the change reaches through dispatch, one depth level per query over the edge
indexes rather than one per node. The walk stops at the depth, the limit or a
level's edge budget and reports truncation instead of failing. An assessment
turns the hits into what a reviewer asks for: how the impact spreads over
modules and source roots, the entry points nothing else depends on, and the
tests to run.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Any

from forge_codegraph_mcp.graph.errors import InvalidRequest
from forge_codegraph_mcp.graph.model import (
    EDGE_CALLS,
    EDGE_FRAMEWORK_BINDING,
    EDGE_IMPLEMENTS,
    EDGE_INHERITS,
    EDGE_OVERRIDES,
    EDGE_REFERENCES,
    EDGE_USES_TYPE,
    IN,
    OUT,
    SEMANTIC_EDGE_KINDS,
    CrossLink,
    CrossLinkQuery,
    Version,
    jfield,
    text,
)
from forge_codegraph_mcp.graph.query.compact import Brief, briefly
from forge_codegraph_mcp.graph.query.crossrepo import Readable, resolve_end
from forge_codegraph_mcp.graph.store import GraphStore

DEFAULT_DEPTH = 2
MAX_DEPTH = 6
DEFAULT_LIMIT = 200
MAX_LIMIT = 2000
# Edges read per depth level; a level past it is reported as truncated.
EDGE_BUDGET = 20000
# How many cross-repository links one walk crosses.
MAX_CROSSINGS = 100

# Whoever calls, references, uses, extends, overrides, implements or is bound
# to a declaration depends on it.
IMPACT_KINDS = (
    EDGE_CALLS,
    EDGE_REFERENCES,
    EDGE_USES_TYPE,
    EDGE_INHERITS,
    EDGE_OVERRIDES,
    EDGE_IMPLEMENTS,
    EDGE_FRAMEWORK_BINDING,
)
# The edges a behavior change propagates along.
INVOCATION_KINDS = (EDGE_CALLS, EDGE_REFERENCES, EDGE_FRAMEWORK_BINDING)
# A declaration and what it can be called through: a method and what it
# overrides, a lambda and the abstract method it implements.
DISPATCH_KINDS = (EDGE_OVERRIDES, EDGE_IMPLEMENTS)

# Declarations scoped to one body; their method is the dependant to act on.
LOCAL_KINDS = frozenset({"local_variable", "parameter", "pattern_variable", "type_parameter"})

CHANGE_ANY = "any"
CHANGE_BODY = "body"
CHANGE_SIGNATURE = "signature"
CHANGE_REMOVE = "remove"
CHANGE_CONTRACT = "contract"


@dataclass(frozen=True, slots=True)
class ChangeProfile:
    """What a kind of change selects."""

    name: str
    kinds: tuple[str, ...]
    # Add what the root overrides or implements as roots.
    upward: bool
    # Add what overrides or implements the root as roots.
    downward: bool
    note: str


PROFILES = {
    CHANGE_ANY: ChangeProfile(
        CHANGE_ANY,
        IMPACT_KINDS,
        upward=True,
        downward=False,
        note="every dependant of the declaration and of what it overrides or implements",
    ),
    CHANGE_BODY: ChangeProfile(
        CHANGE_BODY,
        INVOCATION_KINDS,
        upward=True,
        downward=False,
        note="callers and references, including callers of what the declaration overrides "
        "or implements",
    ),
    CHANGE_SIGNATURE: ChangeProfile(
        CHANGE_SIGNATURE,
        IMPACT_KINDS,
        upward=False,
        downward=False,
        note="every direct user must change: callers, references, type uses, subtypes, "
        "overriders and implementors",
    ),
    CHANGE_REMOVE: ChangeProfile(
        CHANGE_REMOVE,
        IMPACT_KINDS,
        upward=False,
        downward=False,
        note="everything that touches the declaration",
    ),
    CHANGE_CONTRACT: ChangeProfile(
        CHANGE_CONTRACT,
        IMPACT_KINDS,
        upward=False,
        downward=True,
        note="implementors and overriders, and the callers of the declaration and of each of them",
    ),
}


def profile_for(change: str) -> ChangeProfile:
    profile = PROFILES.get(change or CHANGE_ANY)
    if profile is None:
        raise InvalidRequest("change must be any, body, signature, remove or contract")
    return profile


@dataclass(slots=True)
class ImpactHit:
    """A node reached walking dependency edges backwards: ``via`` is the kind
    of the first edge that reached it, ``from_`` the node it leads to,
    ``edges`` how many dependency edges of that level reach it, and module and
    root where it lives in the build."""

    node: Version | None = jfield()
    via: str = jfield("")
    from_: str = jfield("", name="from")
    depth: int = jfield(0)
    edges: int = jfield(0)
    module: str = jfield("", omitempty=True)
    root: str = jfield("", omitempty=True)


@dataclass(slots=True)
class ImpactGroup:
    module: str = jfield("")
    root: str = jfield("")
    nodes: int = jfield(0)
    edges: int = jfield(0)


@dataclass(slots=True)
class TestClass:
    """A test source file with impacted declarations, named the way the
    build's test runner selects it."""

    module: str = jfield("")
    # The class the runner selects; not named class, a Python keyword.
    test_class: str = jfield("", name="class")
    file: str = jfield("")
    language: str = jfield("", omitempty=True)
    nodes: int = jfield(0)


@dataclass(slots=True)
class Assessment:
    groups: list[ImpactGroup] = jfield(factory=list)
    by_depth: dict[str, int] = jfield(factory=dict)
    entry_points: list[Brief] = jfield(factory=list)
    entry_points_complete: bool = jfield(True)
    tests: list[TestClass] = jfield(factory=list)
    commands: list[str] = jfield(factory=list)


@dataclass(slots=True)
class ImpactAcross:
    """Where a change reaches another repository through a link: the linking
    code there and its own dependants, their depths counted from the root.
    ``node`` is absent and ``stale`` set when the linking declaration is gone."""

    repository_id: str = jfield("")
    link: CrossLink = jfield(factory=CrossLink)
    from_: str = jfield("", name="from")
    from_repository_id: str = jfield("")
    depth: int = jfield(0)
    node: Version | None = jfield(None, omitempty=True)
    stale: bool = jfield(False, omitempty=True)
    hits: list[ImpactHit] = jfield(factory=list)


@dataclass(slots=True)
class ImpactResult:
    """The dependants of the roots: the changed declaration first, then what
    the change reaches through dispatch. Each impacted node appears once."""

    root: str = jfield("", omitempty=True)
    roots: list[str] = jfield(factory=list)
    change: str = jfield("")
    note: str = jfield("")
    hits: list[ImpactHit] = jfield(factory=list)
    edges: int = jfield(0)
    skipped_locals: int = jfield(0, omitempty=True)
    truncated: bool = jfield(False)
    assessment: Assessment | None = jfield(None, omitempty=True)
    across: list[ImpactAcross] = jfield(factory=list, omitempty=True)


async def impact(
    store: GraphStore,
    *,
    repository_id: str,
    node_id: str,
    generation: int = 0,
    change: str = "",
    depth: int = 0,
    limit: int = 0,
    include_locals: bool = False,
    across: Readable | None = None,
) -> ImpactResult:
    """What depends on a node, up to ``depth`` hops, for a change of the given
    kind. With ``across``, the walk continues over cross-repository links into
    the repositories it says the caller may read."""
    if not 0 <= depth <= MAX_DEPTH or not 0 <= limit <= MAX_LIMIT:
        raise InvalidRequest(f"impact depth 0-{MAX_DEPTH} and limit 0-{MAX_LIMIT}")
    profile = profile_for(change)
    depth = depth or DEFAULT_DEPTH
    limit = limit or DEFAULT_LIMIT
    await store.get_node(repository_id, node_id, generation)
    roots = await with_dispatch(store, repository_id, generation, [node_id], profile)
    result = await impact_from(
        store, repository_id, generation, roots, profile, depth, limit, include_locals
    )
    result.root = node_id
    await assess(store, repository_id, generation, result)
    if across is not None:
        crossings, cut = await impact_across(
            store, repository_id, generation, depth, limit, include_locals, across, result
        )
        result.across = crossings
        result.truncated = result.truncated or cut
    return result


async def impact_from(
    store: GraphStore,
    repo: str,
    generation: int,
    roots: list[str],
    profile: ChangeProfile,
    depth: int,
    limit: int,
    include_locals: bool,
) -> ImpactResult:
    """The walk: every root at depth zero, then one batched read per level."""
    result = ImpactResult(roots=list(roots), change=profile.name, note=profile.note)
    visited = set(roots)
    frontier = list(roots)
    level_depth = 1
    while level_depth <= depth and frontier and not result.truncated:
        refs, cut = await store.dependency_edges(
            repo, frontier, IN, profile.kinds, generation, EDGE_BUDGET
        )
        if cut:
            result.truncated = True
        result.edges += len(refs)
        # Per node reached: the first edge's kind, where it leads, how many.
        level: dict[str, list[Any]] = {}
        for ref in refs:
            far = ref.source_id
            if not far:
                continue
            if far in level:
                level[far][2] += 1
                continue
            if far in visited:
                continue
            level[far] = [ref.kind, ref.target_id, 1]
        nodes = await store.get_nodes(repo, list(level), generation)
        next_frontier: list[str] = []
        for node_id, (via, reached_from, edges) in level.items():
            version = nodes.get(node_id)
            if version is None:
                visited.add(node_id)
                continue
            kind = (version.node or {}).get("kind")
            if not include_locals and kind in LOCAL_KINDS:
                visited.add(node_id)
                result.skipped_locals += 1
                continue
            if len(result.hits) >= limit:
                result.truncated = True
                break
            visited.add(node_id)
            result.hits.append(
                ImpactHit(node=version, via=via, from_=reached_from, depth=level_depth, edges=edges)
            )
            next_frontier.append(node_id)
        frontier = next_frontier
        level_depth += 1
    return result


async def with_dispatch(
    store: GraphStore, repo: str, generation: int, roots: list[str], profile: ChangeProfile
) -> list[str]:
    """The roots with what they override or implement (upward) or what
    overrides or implements them (downward): a caller bound to a base
    declaration reaches its overrides at runtime."""
    out = list(roots)
    seen = set(roots)
    for wanted, direction in ((profile.upward, OUT), (profile.downward, IN)):
        if not wanted:
            continue
        refs, _ = await store.dependency_edges(
            repo, roots, direction, DISPATCH_KINDS, generation, EDGE_BUDGET
        )
        for ref in refs:
            far = ref.source_id if direction == IN else ref.target_id
            if far and far not in seen:
                seen.add(far)
                out.append(far)
    return out


# --- assessment -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileInfo:
    path: str
    module: str
    root: str
    language: str


def locate(properties: dict[str, Any] | None) -> FileInfo:
    """Module and source root from a file node's properties: the source set
    tells main from test; the path gives the module directory in a Maven or
    Gradle layout (<module>/src/<root>/...), "." for the root module."""
    path = text(properties, "file_path")
    language = text(properties, "language")
    source_set = text(properties, "source_set_id")
    root = source_set[source_set.rfind(":") + 1 :] if ":" in source_set else ""
    rest = ""
    marker = path.find("/src/")
    if path.startswith("src/"):
        module, rest = ".", path[len("src/") :]
    elif marker >= 0:
        module, rest = path[:marker], path[marker + len("/src/") :]
    elif "/" in path:
        module = path[: path.index("/")]
    else:
        module = "."
    if not root:
        segment = (rest + "/").split("/", 1)[0]
        if (
            segment == "test"
            or segment.endswith("Test")
            or segment.endswith("test")
            or "/test/" in path
            or path.startswith("test/")
        ):
            root = "test"
        else:
            root = "main"
    return FileInfo(path=path, module=module, root=root, language=language)


def test_class_of(info: FileInfo) -> TestClass | None:
    """The test class the build's runner selects for a test file: its base
    name, qualified by the package its path implies for Java and Kotlin."""
    base = posixpath.basename(info.path)
    dot = base.rfind(".")
    if dot < 0:
        return None
    test_class = base[:dot]
    for marker in ("/java/", "/kotlin/"):
        at = info.path.find(marker)
        if at >= 0:
            package = info.path[at + len(marker) :].removesuffix(base)
            package = package.replace("/", ".").strip(".")
            if package:
                test_class = f"{package}.{test_class}"
            break
    return TestClass(
        module=info.module, test_class=test_class, file=info.path, language=info.language
    )


def test_commands(tests: list[TestClass]) -> list[str]:
    """One Maven selection per module for Java and Kotlin tests; other
    languages list their files and get no command."""
    by_module: dict[str, list[str]] = {}
    for test in tests:
        if test.language in ("java", "kotlin"):
            by_module.setdefault(test.module, []).append(test.test_class)
    out = []
    for module in sorted(by_module):
        command = "mvn" if module == "." else f"mvn -pl {module} -am"
        classes = ",".join(sorted(by_module[module]))
        out.append(f"{command} -Dtest={classes} -Dsurefire.failIfNoSpecifiedTests=false test")
    return out


async def assess(store: GraphStore, repo: str, generation: int, result: ImpactResult) -> None:
    """Fills the hits' module and root and builds the assessment."""
    lineages = [
        hit.node.lineage for hit in result.hits if hit.node is not None and hit.node.lineage
    ]
    files = await store.get_nodes(repo, lineages, generation)
    infos = {
        file_id: locate(version.node.get("properties"))
        for file_id, version in files.items()
        if version.node is not None
    }
    assessment = Assessment()
    groups: dict[tuple[str, str], ImpactGroup] = {}
    tests: dict[tuple[str, str], TestClass] = {}
    main_ids: list[str] = []
    for hit in result.hits:
        node = hit.node.node if hit.node is not None else None
        info = infos.get(hit.node.lineage if hit.node is not None else "")
        if info is None:
            info = locate((node or {}).get("properties"))
        hit.module, hit.root = info.module, info.root
        assessment.by_depth[str(hit.depth)] = assessment.by_depth.get(str(hit.depth), 0) + 1
        group = groups.setdefault(
            (info.module, info.root), ImpactGroup(module=info.module, root=info.root)
        )
        group.nodes += 1
        group.edges += hit.edges
        if info.root == "test":
            if (test := test_class_of(info)) is not None:
                tests.setdefault((info.module, test.test_class), test).nodes += 1
            continue
        if node is not None:
            main_ids.append(str(node["id"]))
    assessment.groups = sorted(groups.values(), key=lambda g: (-g.nodes, g.module + g.root))
    assessment.tests = sorted(tests.values(), key=lambda t: (t.module, t.test_class))
    assessment.commands = test_commands(assessment.tests)
    # Entry points: impacted declarations outside tests that nothing depends
    # on, through which the change surfaces to callers outside the graph.
    if main_ids:
        refs, cut = await store.dependency_edges(
            repo, main_ids, IN, SEMANTIC_EDGE_KINDS, generation, EDGE_BUDGET
        )
        assessment.entry_points_complete = not cut
        depended = {ref.target_id for ref in refs}
        if not cut:
            for hit in result.hits:
                node = hit.node.node if hit.node is not None else None
                if hit.root != "test" and node is not None and node["id"] not in depended:
                    assessment.entry_points.append(briefly(node))
    result.assessment = assessment


# --- across repositories --------------------------------------------------


@dataclass(slots=True)
class _Reach:
    """One repository's part of a walk: the nodes reached there, at what
    depth, and their qualified names, to cross links from."""

    repo: str
    depths: dict[str, int]
    names: dict[str, str]

    def add(self, version: Version | None, depth: int) -> None:
        node = version.node if version is not None else None
        if node is not None:
            self.depths[node["id"]] = depth
            if node.get("qualified_name"):
                self.names[node["id"]] = node["qualified_name"]

    def reached(self, end: Any) -> tuple[str, int] | None:
        """A link's end among the nodes reached, by id or qualified name."""
        if end.repository_id != self.repo:
            return None
        if end.node_id in self.depths:
            return end.node_id, self.depths[end.node_id]
        if not end.qualified_name:
            return None
        for node_id, name in self.names.items():
            if name == end.qualified_name:
                return node_id, self.depths[node_id]
        return None


async def impact_across(
    store: GraphStore,
    repo: str,
    generation: int,
    depth: int,
    limit: int,
    include_locals: bool,
    readable: Readable,
    start: ImpactResult,
) -> tuple[list[ImpactAcross], bool]:
    """Continues a walk across the links into the nodes it reached,
    repository by repository, following callers and references as for a body
    change at each repository's live generation, until the depth, the limit
    or the crossings run out. Returns the crossings and whether it stopped short."""
    first = _Reach(repo=repo, depths={}, names={})
    for root in (await store.get_nodes(repo, start.roots, generation)).values():
        first.add(root, 0)
    for hit in start.hits:
        first.add(hit.node, hit.depth)
    visited = {(first.repo, node_id) for node_id in first.depths}
    body = profile_for(CHANGE_BODY)
    left = limit - len(start.hits)
    crossings: list[ImpactAcross] = []
    truncated = False
    queue = [first]
    while queue and not truncated:
        here = queue.pop(0)
        query = CrossLinkQuery(
            repository_id=here.repo,
            direction=IN,
            node_ids=tuple(sorted(here.depths)),
            qualified_names=tuple(sorted(here.names.values())),
        )
        for link in await store.cross_links(query):
            reached = here.reached(link.target)
            if reached is None or reached[1] + 1 > depth:
                continue
            reached_from, reached_depth = reached
            far = link.source
            if not readable(far.repository_id) or (far.repository_id, far.node_id) in visited:
                continue
            if len(crossings) >= MAX_CROSSINGS or left <= 0:
                truncated = True
                break
            visited.add((far.repository_id, far.node_id))
            crossing = ImpactAcross(
                repository_id=far.repository_id,
                link=link,
                from_=reached_from,
                from_repository_id=here.repo,
                depth=reached_depth + 1,
            )
            version = await resolve_end(store, far)
            if version is None or version.node is None:
                crossing.stale = True
                crossings.append(crossing)
                continue
            crossing.node = version
            linked_id = str(version.node["id"])
            visited.add((far.repository_id, linked_id))
            left -= 1
            following = _Reach(repo=far.repository_id, depths={}, names={})
            following.add(version, crossing.depth)
            rest = depth - crossing.depth
            if rest > 0 and left > 0:
                walk_roots = await with_dispatch(store, far.repository_id, 0, [linked_id], body)
                walk = await impact_from(
                    store, far.repository_id, 0, walk_roots, body, rest, left, include_locals
                )
                truncated = truncated or walk.truncated
                for hit in walk.hits:
                    hit_id = hit.node.id if hit.node is not None else ""
                    if (far.repository_id, hit_id) in visited:
                        continue
                    visited.add((far.repository_id, hit_id))
                    hit.depth += crossing.depth
                    crossing.hits.append(hit)
                    following.add(hit.node, hit.depth)
                    left -= 1
            crossings.append(crossing)
            queue.append(following)
    return crossings, truncated


# --- compact form ---------------------------------------------------------


@dataclass(slots=True)
class ImpactBrief:
    brief: Brief = jfield(factory=Brief, inline=True)
    via: str = jfield("")
    from_: str = jfield("", name="from")
    depth: int = jfield(0)
    edges: int = jfield(0)
    module: str = jfield("", omitempty=True)
    root: str = jfield("", omitempty=True)


@dataclass(slots=True)
class CompactImpactAcross:
    repository_id: str = jfield("")
    via: str = jfield("")
    label: str = jfield("", omitempty=True)
    link_id: str = jfield("")
    from_: str = jfield("", name="from")
    from_repository_id: str = jfield("")
    depth: int = jfield(0)
    node: Brief = jfield(factory=Brief)
    stale: bool = jfield(False, omitempty=True)
    nodes: list[ImpactBrief] = jfield(factory=list)


@dataclass(slots=True)
class CompactImpactResult:
    """An impact without records: one brief per impacted node with the edges
    that reached it, the totals, a breakdown by kind and the assessment."""

    root: str = jfield("", omitempty=True)
    roots: list[str] = jfield(factory=list)
    change: str = jfield("")
    note: str = jfield("")
    nodes: list[ImpactBrief] = jfield(factory=list)
    edges: int = jfield(0)
    skipped_locals: int = jfield(0, omitempty=True)
    by_kind: dict[str, int] = jfield(factory=dict)
    truncated: bool = jfield(False)
    assessment: Assessment | None = jfield(None, omitempty=True)
    across: list[CompactImpactAcross] = jfield(factory=list, omitempty=True)


def _impact_brief(hit: ImpactHit) -> ImpactBrief:
    item = ImpactBrief(
        via=hit.via,
        from_=hit.from_,
        depth=hit.depth,
        edges=hit.edges,
        module=hit.module,
        root=hit.root,
    )
    if hit.node is not None and hit.node.node is not None:
        item.brief = briefly(hit.node.node)
    return item


def compact_impact(result: ImpactResult) -> CompactImpactResult:
    out = CompactImpactResult(
        root=result.root,
        roots=result.roots,
        change=result.change,
        note=result.note,
        edges=result.edges,
        skipped_locals=result.skipped_locals,
        truncated=result.truncated,
        assessment=result.assessment,
    )
    for hit in result.hits:
        out.nodes.append(_impact_brief(hit))
        if hit.node is not None and hit.node.node is not None:
            kind = str(hit.node.node.get("kind") or "")
            out.by_kind[kind] = out.by_kind.get(kind, 0) + 1
    for crossing in result.across:
        node = crossing.node.node if crossing.node is not None else None
        source = crossing.link.source
        out.across.append(
            CompactImpactAcross(
                repository_id=crossing.repository_id,
                via=crossing.link.kind,
                label=crossing.link.label,
                link_id=crossing.link.id,
                from_=crossing.from_,
                from_repository_id=crossing.from_repository_id,
                depth=crossing.depth,
                node=briefly(node)
                if node is not None
                else Brief(
                    id=source.node_id, kind=source.kind, qualified_name=source.qualified_name
                ),
                stale=crossing.stale,
                nodes=[_impact_brief(hit) for hit in crossing.hits],
            )
        )
    return out
