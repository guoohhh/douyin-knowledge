"""Text normalization shared by entity resolution, FTS indexing and dedup."""

from __future__ import annotations

import hashlib
import re
import unicodedata

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[\s　!-/:-@\[-`{-~ -⁯、-〿！-･]+")

# Full-width -> half-width plus common Chinese/English noise stripped for identity keys.
_NOISE_PREFIXES = ("【", "[", "#")


def normalize_ws(text: str) -> str:
    return _WS.sub(" ", text).strip()


def normalize_identity(text: str) -> str:
    """Aggressive normalization used for ``normalized_name`` / ``normalized_alias``.

    Case-folded, NFKC-normalized, punctuation and whitespace removed. Two strings
    with the same identity key are treated as the *same* surface form. This is the
    only automatic merge signal we trust (ENT-002).
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _PUNCT.sub("", folded)


def content_hash(*parts: str | None) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update((part or "").encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def strip_noise(text: str) -> str:
    out = normalize_ws(text)
    for prefix in _NOISE_PREFIXES:
        out = out.replace(prefix, " ")
    return normalize_ws(out)
