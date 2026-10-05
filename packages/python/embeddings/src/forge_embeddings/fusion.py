"""Reciprocal Rank Fusion, same formula as MongoDB's $rankFusion:

    score(d) = sum over legs L containing d of  weight_L / (k + rank_L(d)),  k = 60

Used by backends that fuse client-side (in-memory, Mongo on clusters without
$rankFusion, or any DB you add later).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence

RRF_K = 60


def rrf_fuse(
    legs: Mapping[str, Sequence[str]],
    weights: Mapping[str, float] | None = None,
    *,
    k: int = RRF_K,
) -> list[tuple[str, float, dict[str, int]]]:
    """Fuse ranked id lists. Returns (id, score, {leg: 1-based rank}) best first."""
    weights = weights or {}
    scores: dict[str, float] = defaultdict(float)
    ranks: dict[str, dict[str, int]] = defaultdict(dict)
    for leg, ids in legs.items():
        w = float(weights.get(leg, 1.0))
        for rank, item in enumerate(ids, start=1):
            if leg in ranks[item]:
                continue
            ranks[item][leg] = rank
            scores[item] += w / (k + rank)
    order = sorted(scores, key=lambda i: (-scores[i], min(ranks[i].values())))
    return [(i, scores[i], ranks[i]) for i in order]
