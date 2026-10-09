"""Content-addressed embedding storage; offline access is strictly read-only.

The key is the model name plus SHA256 of the exact UTF-8 input. Cache misses
are never sent to a provider in offline mode, including malformed entries.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
from pathlib import Path
from typing import Protocol


class EmbeddingUnavailable(RuntimeError):
    """An embedding provider or its cache could not supply valid vectors."""


class EmbeddingCacheMiss(EmbeddingUnavailable):
    def __init__(self, model: str, hashes: list[str]) -> None:
        self.content_hashes = tuple(hashes)
        super().__init__("offline embedding cache miss: model=" + model
                         + " content_sha256=" + ",".join(hashes))


class EmbeddingCacheError(EmbeddingUnavailable):
    def __init__(self, message: str, text: str) -> None:
        self.content_hashes = (content_hash(text),)
        super().__init__(message)


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _vector(value: object) -> list[float]:
    if (not isinstance(value, list) or not value
            or any(isinstance(item, bool) or not isinstance(item, (int, float))
                   or not math.isfinite(item) for item in value)):
        raise EmbeddingUnavailable("embedding must be a non-empty finite numeric vector")
    return [float(item) for item in value]


class CachedEmbedder:
    """Read existing entries; compute and atomically persist misses only online.

    Construction does not create a directory. The optional provider is not
    consulted at all when offline=True, even if one has been supplied.
    """

    def __init__(self, directory: Path, *, model: str,
                 provider: EmbeddingProvider | None = None, offline: bool = False) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("embedding model must be a non-empty string")
        self.directory = Path(directory)
        self.model = model
        self.provider = provider
        self.offline = offline
        self._model_directory = self.directory / content_hash(model)
        self._lock = threading.Lock()

    def entry_path(self, text: str) -> Path:
        return self._model_directory / (content_hash(text) + ".json")

    def _read(self, text: str) -> list[float] | None:
        path = self.entry_path(text)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as error:
            raise EmbeddingCacheError(str(path) + ": cannot read cache ("
                                      + type(error).__name__ + ")", text) from None
        try:
            record = json.loads(raw)
        except (ValueError, TypeError):
            raise EmbeddingCacheError(str(path) + ": malformed cache JSON", text) from None
        if (not isinstance(record, dict)
                or set(record) != {"schema", "model", "content_sha256", "embedding"}
                or type(record["schema"]) is not int or record["schema"] != 1
                or record["model"] != self.model
                or record["content_sha256"] != content_hash(text)):
            raise EmbeddingCacheError(str(path) + ": cache identity/schema mismatch", text)
        try:
            return _vector(record["embedding"])
        except EmbeddingUnavailable as error:
            raise EmbeddingCacheError(str(path) + ": " + str(error), text) from None

    def _write(self, text: str, vector: list[float]) -> None:
        path = self.entry_path(text)
        temporary: str | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            record = {"schema": 1, "model": self.model,
                      "content_sha256": content_hash(text), "embedding": vector}
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".tmp",
                                             dir=path.parent, delete=False) as handle:
                temporary = handle.name
                json.dump(record, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        except OSError as error:
            raise EmbeddingCacheError(str(path) + ": cannot persist cache ("
                                      + type(error).__name__ + ")", text) from None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not isinstance(texts, list) or not all(isinstance(text, str) for text in texts):
            raise ValueError("embedding input must be a list of strings")
        if not texts:
            return []
        with self._lock:
            # Preserve caller order while computing repeated content only once.
            vectors = {text: self._read(text) for text in dict.fromkeys(texts)}
            missing = [text for text, vector in vectors.items() if vector is None]
            if missing and self.offline:
                raise EmbeddingCacheMiss(self.model, [content_hash(text) for text in missing])
            if missing:
                if self.provider is None:
                    raise EmbeddingUnavailable("embedding provider is not configured")
                produced = self.provider.embed(missing)
                if not isinstance(produced, list) or len(produced) != len(missing):
                    raise EmbeddingUnavailable("embedding provider returned the wrong batch size")
                validated = [_vector(vector) for vector in produced]
                dimensions = {len(vector) for vector in vectors.values() if vector is not None}
                dimensions.update(len(vector) for vector in validated)
                if len(dimensions) != 1:
                    raise EmbeddingUnavailable("embedding dimensions differ for the same model")
                for text, vector in zip(missing, validated):
                    self._write(text, vector)
                    vectors[text] = vector
            if len({len(vector) for vector in vectors.values()}) != 1:
                raise EmbeddingUnavailable("cached embedding dimensions differ for the same model")
            return [list(vectors[text]) for text in texts]
