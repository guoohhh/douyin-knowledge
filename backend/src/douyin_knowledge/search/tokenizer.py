"""CJK segmentation for FTS5 indexing and querying (DEC-C10).

Why this module exists
----------------------
`search_fts` is declared with ``tokenize='unicode61 remove_diacritics 2'``.
unicode61 splits on whitespace and punctuation, which is correct for English and
useless for Chinese: the whole sentence "好运茶餐厅人均八十" becomes a single
token, so a query for 人均 matches nothing. The alternatives were:

* ``tokenize='trigram'`` — works for 3+ char queries but silently fails on the
  2-character queries that dominate this corpus (人均 / 日料 / 好吃 / 探店).
* a custom SQLite tokenizer extension — needs a compiled C extension, which
  breaks the "local-first, pip install and go" promise.

So we keep unicode61 and insert spaces ourselves. The invariant that makes this
work is that the *same* segmentation runs on both sides: text is segmented
before being written to ``search_documents`` (the FTS triggers copy it verbatim)
and the query is segmented before being handed to MATCH. Any change here
requires a search reindex, which is why `TOKENIZER_VERSION` is mixed into
``search_documents.content_hash``: bumping it makes every existing row compare
as stale, so the indexer rebuilds them instead of serving results from an index
segmented by older rules.

jieba is an optional dependency. If it is missing we fall back to per-character
segmentation, which is coarser (it can produce false positives across word
boundaries) but never produces false negatives, so search degrades rather than
breaks.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

TOKENIZER_VERSION = "jieba-1"
"""Bump when segmentation behavior changes; forces a reindex of stale rows."""

_CJK_RANGES = (
    (0x3400, 0x4DBF),  # CJK Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xF900, 0xFAFF),  # Compatibility ideographs
    (0x3040, 0x30FF),  # Hiragana + Katakana
)

_LATIN_RUN = re.compile(r"[0-9A-Za-z_'’\-.]+")

# FTS5 gives these characters syntactic meaning inside MATCH expressions.
_FTS_SPECIAL = re.compile(r'[\^\*\:\(\)\"\-\+,]')


class _Segmenter(Protocol):
    def cut(self, sentence: str, HMM: bool = ...) -> Iterable[str]: ...


def is_cjk_char(ch: str) -> bool:
    code = ord(ch)
    return any(low <= code <= high for low, high in _CJK_RANGES)


@lru_cache(maxsize=1)
def _load_jieba() -> _Segmenter | None:
    """Import jieba once. Returns None when unavailable.

    Cached because jieba builds a ~2MB prefix dictionary on first use and we do
    not want that cost repeated per document during a reindex.
    """
    try:
        import jieba  # type: ignore[import-untyped]
    except ImportError:
        return None
    jieba.setLogLevel(60)  # suppress the dictionary-build chatter
    return jieba  # type: ignore[return-value]


def jieba_available() -> bool:
    return _load_jieba() is not None


def _segment_cjk_run(run: str) -> list[str]:
    segmenter = _load_jieba()
    if segmenter is None:
        return list(run)
    return [tok for tok in segmenter.cut(run, HMM=True) if tok.strip()]


def tokenize(text: str) -> list[str]:
    """Split mixed CJK/Latin text into search tokens.

    Latin runs (including version numbers like ``3.12`` and identifiers like
    ``mcp_server``) are kept whole and lowercased; CJK runs go through jieba.
    """
    if not text:
        return []

    tokens: list[str] = []
    buffer: list[str] = []

    def flush_cjk() -> None:
        if buffer:
            tokens.extend(_segment_cjk_run("".join(buffer)))
            buffer.clear()

    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if is_cjk_char(char):
            buffer.append(char)
            index += 1
            continue

        flush_cjk()
        match = _LATIN_RUN.match(text, index)
        if match:
            tokens.append(match.group(0).casefold())
            index = match.end()
        else:
            index += 1  # punctuation and whitespace are separators

    flush_cjk()
    return [tok for tok in tokens if tok.strip()]


def segment_for_index(text: str) -> str:
    """Produce the space-joined form stored in ``search_documents``."""
    return " ".join(tokenize(text))


# Function words stripped from *queries only*, never from the index. Keeping
# them indexed preserves exact-phrase matching; keeping them in a query is what
# makes the OR fallback match every document in the corpus, because "的" appears
# everywhere. That turns "我收藏里有关于量子计算的内容吗" into a hit on all
# sources, and the answer generator then confidently cites unrelated videos.
_QUERY_STOPWORDS = frozenset(
    {
        # Chinese function words, pronouns, and interrogatives.
        "的", "了", "是", "在", "有", "和", "跟", "或", "也", "都", "就", "还",
        "又", "把", "被", "对", "从", "给", "让", "而", "但", "因", "所以",
        "我", "你", "他", "她", "它", "们", "这", "那", "里", "个", "吗", "呢",
        "吧", "啊", "哦", "嘛", "会", "要", "没", "不", "什么", "怎么", "哪些",
        "哪个", "关于", "一些", "可以", "能不能", "有没有", "内容",
        # English equivalents.
        "the", "a", "an", "of", "is", "are", "was", "were", "to", "in", "on",
        "and", "or", "do", "does", "did", "i", "my", "me", "any", "about",
        "what", "which", "how",
    }
)


def strip_query_stopwords(tokens: list[str]) -> list[str]:
    """Drop function words, unless that would empty the query.

    The fallback matters: a query that is *entirely* stopwords ("有什么吗") still
    has to search for something, and returning nothing at all would look like a
    bug rather than a vague question.
    """
    kept = [tok for tok in tokens if tok not in _QUERY_STOPWORDS]
    return kept or tokens


def build_match_query(query: str, *, mode: str = "and") -> str:
    """Turn user input into a safe FTS5 MATCH expression.

    Every token is quoted, which both escapes FTS5 operators and prevents a
    user typing ``NOT`` or ``*`` from rewriting the query — the search-side
    equivalent of parameterizing SQL.
    """
    tokens = strip_query_stopwords(tokenize(query))
    if not tokens:
        return ""
    quoted = [f'"{_FTS_SPECIAL.sub(" ", tok).strip()}"' for tok in tokens]
    quoted = [tok for tok in quoted if tok != '""']
    if not quoted:
        return ""
    joiner = " OR " if mode == "or" else " AND "
    return joiner.join(quoted)


__all__ = [
    "TOKENIZER_VERSION",
    "build_match_query",
    "is_cjk_char",
    "jieba_available",
    "segment_for_index",
    "strip_query_stopwords",
    "tokenize",
]
