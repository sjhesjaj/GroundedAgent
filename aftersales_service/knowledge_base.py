"""The after-sales knowledge base behind the m3 read tool search_knowledge_base.

M3 Phase 0 (docs/v2/m3-policy-rag.md, "Tool search_knowledge_base" and
"Corpus"): the smallest real retrieval. Product-owned and read-only; it is not
a CapabilityGate tool and not in the frozen registry.

Corpus
    `knowledge_base/*.md`, one document per file: JSON front matter between two
    `---` lines (doc_id = file stem, title, doc_type, scope, effective_from,
    effective_to, version, restates), then `## ` sections. A section is one
    passage of at most PASSAGE_CHARS characters (a longer one is split at
    sentence ends). A document that restates one of the 6 structured rules
    names it in `restates`; every day count it states must equal that rule's
    window_days, and its effective period must lie within the rule's. The
    build refuses a corpus that breaks either.

Retrieval
    BM25 over CJK character bigrams and ASCII words, plus bge-m3 embeddings
    from Ollama, fused with reciprocal rank fusion (RRF). Only documents in
    force at the business time take part. At most MAX_PASSAGES passages are
    returned. If Ollama or bge-m3 is not available the search falls back to
    BM25 alone and the result says so: the retrieval mode is recorded in every
    result's trace.

Evidence
    One plain document Evidence per passage, its content wrapped in fixed
    delimiters together with doc_id and version. A passage that restates a
    rule carries `restates_policy_id`, never `policy_ref`: the frozen answer
    layer attaches any `policy_ref` evidence to derived window facts. Scores are
    ranking signals only and never appear as confidence.

This module never reads a clock: the business time is an argument.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from aftersales.arguments import validate_arguments
from aftersales.executor import TRACE_OBSERVATION_ID
from aftersales.policy_catalog import PublishedPolicyCatalog
from orchestration.contracts import Evidence, SourceType, ToolResult, ToolStatus

KNOWLEDGE_TOOL_NAME = "search_knowledge_base"
KNOWLEDGE_TOOL_PARAMETERS = ("query",)
CORPUS_DIRECTORY = Path(__file__).resolve().parent.parent / "knowledge_base"

MAX_PASSAGES = 4
PASSAGE_CHARS = 400
RRF_K = 60
BM25_K1 = 1.5
BM25_B = 0.75
KNOWLEDGE_AUTHORITY = 60

EMBEDDING_MODEL = "bge-m3"
# 127.0.0.1, not localhost: on Windows "localhost" tries IPv6 first and adds
# about two seconds to every call when Ollama listens on IPv4 only.
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
EMBED_TIMEOUT = (2.0, 30.0)   # (connect, read) seconds

MODE_HYBRID = "hybrid"        # BM25 + bge-m3, RRF
MODE_BM25 = "bm25"            # lexical only
FALLBACK_NO_EMBEDDER = "embedder_not_configured"
FALLBACK_EMBEDDING_UNAVAILABLE = "embedding_unavailable"

PASSAGE_OPEN = "<<<KB_PASSAGE"
PASSAGE_CLOSE = "<<<END_KB_PASSAGE>>>"

FRONT_MATTER_KEYS = frozenset({"doc_id", "title", "doc_type", "scope", "effective_from",
                               "effective_to", "version", "restates"})
DOC_TYPES = frozenset({"faq", "guide", "promotion"})
DAY_COUNT = re.compile(r"(\d+)\s*(?:个自然日|个工作日|天)")


class CorpusError(ValueError):
    """The corpus breaks its contract; the knowledge base is not built."""


class EmbeddingUnavailable(RuntimeError):
    """Ollama or the embedding model could not produce vectors."""


# --------------------------------------------------------------------------
# Corpus
# --------------------------------------------------------------------------


def _aware(path: str, value: object) -> datetime:
    if not isinstance(value, str):
        raise CorpusError(path + " must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise CorpusError(path + " must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None:
        raise CorpusError(path + " must carry a timezone offset")
    return parsed


@dataclass(frozen=True)
class KnowledgeDocument:
    doc_id: str
    title: str
    doc_type: str
    scope: tuple[str, ...]
    effective_from: datetime
    effective_to: datetime | None
    version: str
    restates: str | None
    source: str
    sections: tuple[tuple[str, str], ...]   # (heading, text)

    def in_force(self, as_of: datetime) -> bool:
        return self.effective_from <= as_of and (self.effective_to is None or as_of < self.effective_to)


def parse_document(path: Path) -> KnowledgeDocument:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    parts = text.split("\n---\n", 1)
    if not parts[0].startswith("---\n") or len(parts) != 2:
        raise CorpusError(path.name + ": front matter must sit between two --- lines")
    try:
        meta = json.loads(parts[0][len("---\n"):])
    except json.JSONDecodeError:
        raise CorpusError(path.name + ": front matter is not JSON") from None
    if not isinstance(meta, dict) or set(meta) != FRONT_MATTER_KEYS:
        raise CorpusError(path.name + ": front matter keys must be " + ", ".join(sorted(FRONT_MATTER_KEYS)))
    if meta["doc_id"] != path.stem:
        raise CorpusError(path.name + ": doc_id must equal the file name")
    for key in ("title", "version"):
        if not isinstance(meta[key], str) or not meta[key].strip():
            raise CorpusError(path.name + ": " + key + " must be a non-empty string")
    if meta["doc_type"] not in DOC_TYPES:
        raise CorpusError(path.name + ": doc_type must be one of " + ", ".join(sorted(DOC_TYPES)))
    scope = meta["scope"]
    if not isinstance(scope, list) or not all(isinstance(item, str) and item for item in scope):
        raise CorpusError(path.name + ": scope must be a list of category names")
    restates = meta["restates"]
    if restates is not None and (not isinstance(restates, str) or not restates.strip()):
        raise CorpusError(path.name + ": restates must be a policy id or null")
    effective_from = _aware(path.name + ": effective_from", meta["effective_from"])
    effective_to = (None if meta["effective_to"] is None
                    else _aware(path.name + ": effective_to", meta["effective_to"]))
    sections = []
    for block in re.split(r"(?m)^## ", parts[1])[1:]:
        heading, _, body = block.partition("\n")
        body = " ".join(line.strip() for line in body.strip().splitlines() if line.strip())
        if not heading.strip() or not body:
            raise CorpusError(path.name + ": every section needs a heading and text")
        sections.append((heading.strip(), body))
    if not sections:
        raise CorpusError(path.name + ": a document needs at least one ## section")
    return KnowledgeDocument(doc_id=meta["doc_id"], title=meta["title"], doc_type=meta["doc_type"],
                             scope=tuple(scope), effective_from=effective_from,
                             effective_to=effective_to, version=meta["version"],
                             restates=restates, source="knowledge_base/" + path.name,
                             sections=tuple(sections))


def check_restatement(document: KnowledgeDocument, rules: Mapping[str, object]) -> None:
    """A restating document must agree with its rule: day counts and effective period."""
    if document.restates is None:
        return
    record = rules.get(document.restates)
    if record is None:
        raise CorpusError(document.doc_id + ": restates an unknown rule " + document.restates)
    window_days = record.params.get("window_days")
    for heading, text in document.sections:
        for found in DAY_COUNT.finditer(heading + " " + text):
            if window_days is None or int(found.group(1)) != window_days:
                raise CorpusError(document.doc_id + ": a restated day count differs from "
                                  + document.restates + " window_days")
    rule_from = datetime.fromisoformat(record.effective_from)
    rule_to = None if record.effective_to is None else datetime.fromisoformat(record.effective_to)
    if document.effective_from < rule_from or (rule_to is not None and (
            document.effective_to is None or document.effective_to > rule_to)):
        raise CorpusError(document.doc_id + ": restates a rule outside the rule's effective period")


def published_rules() -> dict[str, object]:
    """policy_id -> PolicyRecord of the published catalog (the frozen 6 rules)."""
    return {record.policy_id: record for record in PublishedPolicyCatalog().snapshot().records}


def load_corpus(directory: Path = CORPUS_DIRECTORY,
                rules: Mapping[str, object] | None = None) -> tuple[KnowledgeDocument, ...]:
    rules = published_rules() if rules is None else rules
    documents = tuple(parse_document(path) for path in sorted(Path(directory).glob("*.md")))
    if len({document.doc_id for document in documents}) != len(documents):
        raise CorpusError("doc_id must be unique")
    for document in documents:
        check_restatement(document, rules)
    return documents


# --------------------------------------------------------------------------
# Passages
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Passage:
    document: KnowledgeDocument
    section: str
    text: str

    @property
    def search_text(self) -> str:
        return self.document.title + " " + self.section + " " + self.text


def _pieces(text: str) -> list[str]:
    """Sentence-end split into pieces of at most PASSAGE_CHARS characters."""
    if len(text) <= PASSAGE_CHARS:
        return [text]
    pieces, current = [], ""
    for sentence in re.findall(r"[^。；！？]*[。；！？]?", text):
        if not sentence:
            continue
        while len(sentence) > PASSAGE_CHARS:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:PASSAGE_CHARS])
            sentence = sentence[PASSAGE_CHARS:]
        if len(current) + len(sentence) > PASSAGE_CHARS:
            pieces.append(current)
            current = ""
        current += sentence
    if current:
        pieces.append(current)
    return pieces


def split_passages(document: KnowledgeDocument) -> tuple[Passage, ...]:
    return tuple(Passage(document=document, section=heading, text=piece)
                 for heading, text in document.sections for piece in _pieces(text))


# --------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------


def tokens(text: str) -> list[str]:
    """CJK character bigrams (a lone character as itself) and lower-case ASCII words."""
    found: list[str] = []
    for run in re.findall(r"[一-鿿]+|[A-Za-z0-9]+", text):
        if run[0].isascii():
            found.append(run.lower())
        elif len(run) == 1:
            found.append(run)
        else:
            found.extend(run[index:index + 2] for index in range(len(run) - 1))
    return found


class BM25:
    def __init__(self, documents: Sequence[Sequence[str]]) -> None:
        self._documents = [list(document) for document in documents]
        self._average = (sum(len(document) for document in self._documents)
                         / max(len(self._documents), 1)) or 1.0
        frequency: dict[str, int] = {}
        for document in self._documents:
            for term in set(document):
                frequency[term] = frequency.get(term, 0) + 1
        count = len(self._documents)
        self._idf = {term: math.log(1 + (count - n + 0.5) / (n + 0.5)) for term, n in frequency.items()}

    def score(self, query: Sequence[str], index: int) -> float:
        document = self._documents[index]
        total = 0.0
        for term in set(query):
            occurrences = document.count(term)
            if occurrences:
                norm = BM25_K1 * (1 - BM25_B + BM25_B * len(document) / self._average)
                total += self._idf[term] * occurrences * (BM25_K1 + 1) / (occurrences + norm)
        return total


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class OllamaEmbedder:
    """bge-m3 through Ollama's /api/embed. Any failure is EmbeddingUnavailable."""

    def __init__(self, base_url: str = DEFAULT_OLLAMA_URL, model: str = EMBEDDING_MODEL) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

    @classmethod
    def from_environment(cls) -> "OllamaEmbedder":
        return cls(os.environ.get("AFTERSALES_KB_OLLAMA_URL") or DEFAULT_OLLAMA_URL)

    def embed(self, texts: list[str]) -> list[list[float]]:
        import requests

        try:
            response = requests.post(self.base_url + "/api/embed",
                                     json={"model": self.model, "input": texts},
                                     timeout=EMBED_TIMEOUT)
            response.raise_for_status()
            vectors = response.json()["embeddings"]
        except Exception as error:
            raise EmbeddingUnavailable(type(error).__name__) from None
        if (not isinstance(vectors, list) or len(vectors) != len(texts)
                or not all(isinstance(vector, list) and vector for vector in vectors)):
            raise EmbeddingUnavailable("malformed embeddings")
        return vectors


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


def reciprocal_rank_fusion(rankings: Sequence[Sequence[int]], k: int = RRF_K) -> list[int]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, index in enumerate(ranking, start=1):
            scores[index] = scores.get(index, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda index: (-scores[index], index))


@dataclass(frozen=True)
class KnowledgeSearch:
    passages: tuple[Passage, ...]
    mode: str
    fallback: str | None


class KnowledgeBase:
    """Hybrid search over the corpus' passages. Passage vectors are computed once, lazily."""

    def __init__(self, documents: Sequence[KnowledgeDocument], *,
                 embedder: Embedder | None = None) -> None:
        self.passages = tuple(passage for document in documents for passage in split_passages(document))
        self._bm25 = BM25([tokens(passage.search_text) for passage in self.passages])
        self._embedder = embedder
        self._vectors: list[list[float]] | None = None
        self._lock = threading.Lock()

    def _passage_vectors(self) -> list[list[float]]:
        with self._lock:
            if self._vectors is None:
                self._vectors = self._embedder.embed([passage.search_text for passage in self.passages])
            return self._vectors

    def search(self, query: str, *, as_of: datetime) -> KnowledgeSearch:
        candidates = [index for index, passage in enumerate(self.passages)
                      if passage.document.in_force(as_of)]
        terms = tokens(query)
        scored = [(self._bm25.score(terms, index), index) for index in candidates]
        lexical = [index for score, index in sorted(scored, key=lambda item: (-item[0], item[1]))
                   if score > 0]
        mode, fallback, ranked = MODE_BM25, FALLBACK_NO_EMBEDDER, lexical
        if self._embedder is not None and candidates:
            try:
                vectors = self._passage_vectors()
                question = self._embedder.embed([query])[0]
            except EmbeddingUnavailable:
                fallback = FALLBACK_EMBEDDING_UNAVAILABLE
            else:
                dense = sorted(candidates, key=lambda index: (-_cosine(question, vectors[index]), index))
                mode, fallback, ranked = MODE_HYBRID, None, reciprocal_rank_fusion([lexical, dense])
        return KnowledgeSearch(passages=tuple(self.passages[index] for index in ranked[:MAX_PASSAGES]),
                               mode=mode, fallback=fallback)


_shared: KnowledgeBase | None = None
_shared_lock = threading.Lock()


def shared_knowledge_base() -> KnowledgeBase:
    """The process-wide knowledge base over CORPUS_DIRECTORY, built on first use."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = KnowledgeBase(load_corpus(), embedder=OllamaEmbedder.from_environment())
        return _shared


# --------------------------------------------------------------------------
# The tool result
# --------------------------------------------------------------------------


def _clean(text: str) -> str:
    """Passage text can never forge a delimiter."""
    return text.replace("<<<", "«").replace(">>>", "»")


def passage_evidence(passage: Passage, rank: int, *, observation_id: str,
                     as_of: datetime) -> Evidence:
    document = passage.document
    content = (PASSAGE_OPEN + ' doc_id="' + document.doc_id + '" version="' + document.version
               + '">>>\n## ' + _clean(passage.section) + "\n" + _clean(passage.text) + "\n" + PASSAGE_CLOSE)
    metadata: dict[str, object] = {"doc_id": document.doc_id, "doc_type": document.doc_type,
                                   "title": document.title, "section": passage.section,
                                   "rank": rank, TRACE_OBSERVATION_ID: observation_id}
    if document.restates is not None:
        metadata["restates_policy_id"] = document.restates
    return Evidence(content=content, source_type=SourceType.DOCUMENT, source=document.source,
                    locator="kb:" + document.doc_id + "#" + passage.section, version=document.version,
                    observed_at=as_of.isoformat(), authority=KNOWLEDGE_AUTHORITY, metadata=metadata)


def knowledge_tool_result(knowledge: KnowledgeBase, arguments: Mapping[str, str], *,
                          observation_id: str, as_of: datetime) -> ToolResult:
    """One search_knowledge_base call as a ToolResult, with its retrieval mode in the trace."""
    trace: dict[str, object] = {TRACE_OBSERVATION_ID: observation_id, "tool": KNOWLEDGE_TOOL_NAME}
    try:
        query = validate_arguments(KNOWLEDGE_TOOL_NAME, KNOWLEDGE_TOOL_PARAMETERS, arguments)["query"]
    except ValueError:
        return ToolResult(tool_name=KNOWLEDGE_TOOL_NAME, status=ToolStatus.ERROR,
                          error_code="invalid_arguments",
                          error_message="search_knowledge_base takes exactly one non-empty query",
                          trace=trace)
    search = knowledge.search(query, as_of=as_of)
    trace.update({"retrieval_mode": search.mode, "fallback": search.fallback,
                  "passages": len(search.passages)})
    if not search.passages:
        return ToolResult(tool_name=KNOWLEDGE_TOOL_NAME, status=ToolStatus.EMPTY, trace=trace)
    evidence = tuple(passage_evidence(passage, rank, observation_id=observation_id, as_of=as_of)
                     for rank, passage in enumerate(search.passages, start=1))
    return ToolResult(tool_name=KNOWLEDGE_TOOL_NAME, status=ToolStatus.OK, evidence=evidence,
                      trace=trace)
