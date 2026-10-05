"""Tokenizers. Size chunks with the tokenizer of your embedding model in
production (HuggingFaceTokenizer("voyageai/voyage-4") or tiktoken for OpenAI);
the heuristic one needs no downloads and is used in tests and as a fallback."""

from __future__ import annotations

import re
from typing import Any

_PIECE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def _piece_cost(piece: str) -> int:
    # ~1 token per short word, +1 per extra 6 chars; each punctuation mark ~1.
    return 1 + max(0, (len(piece) - 1) // 6)


class HeuristicTokenizer:
    """Approximates BPE token counts (within ~15% for English prose)."""

    name = "heuristic"

    def count(self, text: str) -> int:
        return sum(_piece_cost(m.group()) for m in _PIECE.finditer(text))

    def split(self, text: str, max_tokens: int, overlap: int = 0) -> list[str]:
        spans = [(m.start(), m.end(), _piece_cost(m.group())) for m in _PIECE.finditer(text)]
        if not spans:
            return []
        windows: list[str] = []
        i = 0
        while i < len(spans):
            cost, j = 0, i
            while j < len(spans) and (cost + spans[j][2] <= max_tokens or j == i):
                cost += spans[j][2]
                j += 1
            windows.append(text[spans[i][0] : spans[j - 1][1]].strip())
            if j >= len(spans):
                break
            back, k = 0, j
            while overlap and k - 1 > i and back + spans[k - 1][2] <= overlap:
                k -= 1
                back += spans[k][2]
            i = k if k > i else j
        return [w for w in windows if w]


class TiktokenTokenizer:
    def __init__(self, encoding: str = "cl100k_base") -> None:
        import tiktoken

        self._enc = tiktoken.get_encoding(encoding)
        self.name = f"tiktoken:{encoding}"

    def count(self, text: str) -> int:
        return len(self._enc.encode(text, disallowed_special=()))

    def split(self, text: str, max_tokens: int, overlap: int = 0) -> list[str]:
        ids = self._enc.encode(text, disallowed_special=())
        step = max(1, max_tokens - overlap)
        return [
            self._enc.decode(ids[i : i + max_tokens]).strip()
            for i in range(0, len(ids), step)
            if ids[i : i + max_tokens]
        ]


class HuggingFaceTokenizer:
    """``tokenizers`` fast tokenizer, e.g. HuggingFaceTokenizer("voyageai/voyage-4")."""

    def __init__(self, name_or_path: str) -> None:
        from tokenizers import Tokenizer

        if name_or_path.endswith(".json"):
            self._tok: Any = Tokenizer.from_file(name_or_path)
        else:
            self._tok = Tokenizer.from_pretrained(name_or_path)
        self.name = f"hf:{name_or_path}"

    def count(self, text: str) -> int:
        return len(self._tok.encode(text, add_special_tokens=False).ids)

    def split(self, text: str, max_tokens: int, overlap: int = 0) -> list[str]:
        enc = self._tok.encode(text, add_special_tokens=False)
        offsets = enc.offsets
        step = max(1, max_tokens - overlap)
        out: list[str] = []
        for i in range(0, len(offsets), step):
            window = offsets[i : i + max_tokens]
            if window:
                out.append(text[window[0][0] : window[-1][1]].strip())
        return [w for w in out if w]
