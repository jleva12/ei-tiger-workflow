"""Tiny BM25 used by the in-memory reference backends (tests, local dev)."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence

_TOKEN = re.compile(r"[a-z0-9_]+")
_STOP = frozenset(
    "a an and are as at be by did do does for from has have how in is it its of on or that the to was were what when where which who why will with".split()
)


def analyze(text: str, *, stem: bool = True) -> list[str]:
    """Lowercase, split, drop stopwords, crude plural stripping (~ lucene.english)."""
    out = []
    for t in _TOKEN.findall(text.lower()):
        if t in _STOP:
            continue
        if stem and len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


def fielded_bm25(
    query: str,
    docs: Sequence[Mapping[str, str]],
    boosts: Mapping[str, float],
    *,
    k1: float = 1.2,
    b: float = 0.75,
    stem: bool = True,
) -> list[float]:
    """Sum of per-field BM25 scores, each multiplied by its boost."""
    terms = analyze(query, stem=stem)
    n = len(docs)
    scores = [0.0] * n
    if not n or not terms:
        return scores
    for field, boost in boosts.items():
        toks = [analyze(d.get(field) or "", stem=stem) for d in docs]
        avgdl = (sum(len(t) for t in toks) / n) or 1.0
        df: Counter[str] = Counter()
        for t in toks:
            df.update(set(t))
        for i, t in enumerate(toks):
            if not t:
                continue
            tf = Counter(t)
            for term in terms:
                f = tf.get(term)
                if not f:
                    continue
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                scores[i] += boost * idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(t) / avgdl))
    return scores


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
