"""Typed {{ value }} references in expressions (not text interpolation).

Keep this scanner in sync with forge-web's, which the builder checks
expressions with: both are tested on tests/fixtures/expression-references.json.
Grouping preserves types and precedence; literals and comments are untouched.
"""

from __future__ import annotations

import re

WORD = re.compile(r"[\w$]+", re.ASCII)


def normalize_references(text: str) -> str:
    result = list(text)
    braces: list[tuple[bool, int]] = []
    operand = True
    i = 0
    while i < len(text):
        c = text[i]
        pair = text[i : i + 2]
        if c.isspace():
            i += 1
            continue
        if pair == "/*":
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            continue
        if c in "\"'`" or (c == "/" and operand):
            quote = c
            character_class = False
            i += 1
            while i < len(text):
                next_char = text[i]
                i += 1
                if next_char == "\\" and quote != "`":
                    i += 1
                elif quote == "/" and next_char == "[":
                    character_class = True
                elif quote == "/" and next_char == "]":
                    character_class = False
                elif next_char == quote and not character_class:
                    break
            operand = False
            continue
        if pair == "{{":
            braces.append((True, i))
            result[i : i + 2] = "( "
            i += 2
            operand = True
            continue
        top = braces[-1] if braces else None
        if pair == "}}" and top and top[0]:
            if not text[top[1] + 2 : i].strip():
                raise ValueError("Choose a field inside {{ }}.")
            braces.pop()
            result[i : i + 2] = " )"
            i += 2
            operand = False
            continue
        if c == "{":
            braces.append((False, i))
        elif c == "}" and top and not top[0]:
            braces.pop()
        word = WORD.match(text, i)
        if word:
            operand = word[0] in ("and", "or", "in")
            i = word.end()
        else:
            operand = not operand if c in "*%" else c not in ")]}"
            i += 1
    if any(reference for reference, _ in braces):
        raise ValueError("Close this reference with }}.")
    return "".join(result)
