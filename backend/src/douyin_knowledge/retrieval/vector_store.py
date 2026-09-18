"""Local vector index backed by numpy + a single .npz file on disk.

Why not a vector database
-------------------------
The corpus here is one person's saved videos — thousands of chunks, not
millions. A brute-force cosine scan over a float32 matrix of that size costs
under a millisecond, and it removes an entire moving part from a local-first
app the user is expected to run with `pip install` and no daemon. LanceDB stays
available as an optional extra for anyone whose collection outgrows this.

The store owns *only* vectors. All bookkeeping — which object a vector belongs
to, which model produced it, whether it is stale — lives in ``vector_documents``
so it participates in the same transaction as the rest of the write. The store
is keyed by the string ``vector_key`` ("chunk:chk_abc"), never by row position,
because positions shift on every prune.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from douyin_knowledge.observability.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

logger = get_logger(__name__)

_INDEX_FILENAME = "vectors.npz"


@dataclass(frozen=True)
class VectorHit:
    vector_key: str
    score: float
    doc_type: str
    object_id: str

    @classmethod
    def from_key(cls, vector_key: str, score: float) -> VectorHit:
        doc_type, _, object_id = vector_key.partition(":")
        return cls(vector_key=vector_key, score=score, doc_type=doc_type, object_id=object_id)


class VectorStore:
    """Cosine-similarity store persisted as a single compressed npz file.

    Vectors are L2-normalized on write, which turns cosine similarity into a
    plain dot product and lets the whole search be one matrix multiply.
    """

    def __init__(self, directory: Path | str, *, dimensions: int | None = None) -> None:
        self.directory = Path(directory)
        self.path = self.directory / _INDEX_FILENAME
        self.dimensions = dimensions
        self._keys: list[str] = []
        self._key_to_row: dict[str, int] = {}
        self._matrix: np.ndarray | None = None
        self._dirty = False
        # Guards mutation because the worker may index while the API queries.
        self._lock = threading.RLock()
        self._load()

    # ---------------------------------------------------------------- persistence

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with np.load(self.path, allow_pickle=False) as payload:
                keys = [str(k) for k in payload["keys"].tolist()]
                matrix = np.asarray(payload["vectors"], dtype=np.float32)
        except (OSError, ValueError, KeyError) as exc:
            # A corrupt index is recoverable state: it can always be rebuilt
            # from search_documents, so warn and start empty rather than crash
            # the whole app on startup.
            logger.warning(
                "vector_index_load_failed",
                extra={"path": str(self.path), "error": str(exc)},
            )
            return

        if matrix.ndim != 2 or matrix.shape[0] != len(keys):
            logger.warning("vector_index_shape_mismatch", extra={"path": str(self.path)})
            return

        self._keys = keys
        self._key_to_row = {key: i for i, key in enumerate(keys)}
        self._matrix = matrix
        if matrix.shape[0]:
            self.dimensions = int(matrix.shape[1])

    def save(self) -> None:
        """Write the index to disk. No-op when nothing changed."""
        with self._lock:
            if not self._dirty:
                return
            self.directory.mkdir(parents=True, exist_ok=True)
            matrix = self._matrix if self._matrix is not None else np.zeros((0, 0), dtype=np.float32)
            tmp = self.path.with_suffix(".npz.tmp")
            # Write through an open handle: np.savez_compressed appends ".npz"
            # to any *path* that lacks it, which would silently produce
            # "vectors.npz.tmp.npz" and leave the rename below with nothing
            # to move.
            with tmp.open("wb") as handle:
                np.savez_compressed(
                    handle,
                    keys=np.array(self._keys, dtype=object).astype("U"),
                    vectors=matrix,
                )
            tmp.replace(self.path)  # atomic swap; a crash mid-write cannot truncate the index
            self._dirty = False
            logger.info("vector_index_saved", extra={"count": len(self._keys)})

    # ------------------------------------------------------------------ mutation

    @staticmethod
    def _normalize(vector: Sequence[float]) -> np.ndarray:
        array = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(array))
        if norm > 0:
            array = array / norm
        return array

    def upsert(
        self, vector_key: str, vector: Sequence[float], metadata: dict[str, Any] | None = None
    ) -> None:
        """Insert or replace one vector. `metadata` is accepted and ignored.

        Metadata is deliberately not stored: ``vector_documents`` is the record
        of truth for it, and duplicating it here would create a second thing to
        keep in sync. The parameter exists so callers read naturally.
        """
        del metadata
        with self._lock:
            array = self._normalize(vector)
            if self.dimensions is None:
                self.dimensions = int(array.shape[0])
            elif array.shape[0] != self.dimensions:
                raise ValueError(
                    f"vector dimension {array.shape[0]} does not match index dimension "
                    f"{self.dimensions}; a full reindex is required after changing model"
                )

            row = self._key_to_row.get(vector_key)
            if row is not None and self._matrix is not None:
                self._matrix[row] = array
            else:
                block = array.reshape(1, -1)
                self._matrix = (
                    block if self._matrix is None or not len(self._keys)
                    else np.vstack([self._matrix, block])
                )
                self._key_to_row[vector_key] = len(self._keys)
                self._keys.append(vector_key)
            self._dirty = True

    def upsert_many(self, items: Iterable[tuple[str, Sequence[float]]]) -> int:
        count = 0
        for key, vector in items:
            self.upsert(key, vector)
            count += 1
        return count

    def delete(self, vector_keys: Iterable[str]) -> int:
        targets = {k for k in vector_keys if k in self._key_to_row}
        if not targets:
            return 0
        return self._keep_only([k for k in self._keys if k not in targets])

    def prune(self, keep_keys: Iterable[str]) -> int:
        """Delete every vector whose key is not in `keep_keys`."""
        keep = set(keep_keys)
        return self._keep_only([k for k in self._keys if k in keep])

    def _keep_only(self, ordered_keys: list[str]) -> int:
        with self._lock:
            removed = len(self._keys) - len(ordered_keys)
            if removed <= 0:
                return 0
            if self._matrix is not None and ordered_keys:
                rows = [self._key_to_row[k] for k in ordered_keys]
                self._matrix = self._matrix[rows]
            else:
                self._matrix = None
            self._keys = ordered_keys
            self._key_to_row = {k: i for i, k in enumerate(ordered_keys)}
            self._dirty = True
            return removed

    def clear(self) -> None:
        with self._lock:
            self._keys = []
            self._key_to_row = {}
            self._matrix = None
            self._dirty = True

    # -------------------------------------------------------------------- search

    def search(
        self,
        query_vector: Sequence[float],
        *,
        limit: int = 20,
        doc_types: Sequence[str] | None = None,
        min_score: float = 0.0,
        allowed_keys: Iterable[str] | None = None,
    ) -> list[VectorHit]:
        """Return the `limit` closest vectors by cosine similarity.

        `allowed_keys` lets the retriever apply the currency filter (DB-004)
        before scoring, so superseded chunks cannot occupy result slots.
        """
        with self._lock:
            if self._matrix is None or not self._keys:
                return []
            query = self._normalize(query_vector)
            if query.shape[0] != self._matrix.shape[1]:
                logger.warning(
                    "vector_query_dim_mismatch",
                    extra={"query_dim": int(query.shape[0]), "index_dim": int(self._matrix.shape[1])},
                )
                return []

            scores = self._matrix @ query  # normalized rows => dot product is cosine
            allowed = set(allowed_keys) if allowed_keys is not None else None

            candidates: list[tuple[float, str]] = []
            for index, key in enumerate(self._keys):
                if allowed is not None and key not in allowed:
                    continue
                if doc_types and key.partition(":")[0] not in doc_types:
                    continue
                score = float(scores[index])
                if score < min_score:
                    continue
                candidates.append((score, key))

            candidates.sort(key=lambda pair: pair[0], reverse=True)
            return [VectorHit.from_key(key, score) for score, key in candidates[:limit]]

    # ------------------------------------------------------------------ inspect

    def __len__(self) -> int:
        return len(self._keys)

    def __contains__(self, vector_key: object) -> bool:
        return vector_key in self._key_to_row

    def stats(self) -> dict[str, Any]:
        by_type: dict[str, int] = {}
        for key in self._keys:
            by_type[key.partition(":")[0]] = by_type.get(key.partition(":")[0], 0) + 1
        return {
            "count": len(self._keys),
            "dimensions": self.dimensions,
            "path": str(self.path),
            "persisted": self.path.exists(),
            "by_doc_type": by_type,
        }


def open_vector_store(directory: Path | str, *, dimensions: int | None = None) -> VectorStore:
    return VectorStore(directory, dimensions=dimensions)


__all__ = ["VectorHit", "VectorStore", "open_vector_store"]
