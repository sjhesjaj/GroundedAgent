"""The after-sales knowledge base behind the m3 read tool search_knowledge_base.

M3 Phase 1 (docs/v2/m3-policy-rag.md, "Tool search_knowledge_base" and
"Corpus"). Product-owned and read-only; it is not
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
from .embedding_cache import CachedEmbedder, EmbeddingCacheMiss, EmbeddingUnavailable, content_hash

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
EMBEDDING_CACHE_DIRECTORY = CORPUS_DIRECTORY.parent / ".cache" / "m3-embeddings"
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
_NUMBER = r"(?:[0-9０-９]+|[零〇一二两三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾佰仟]+)"
_UNIT = r"(?:个\s*)?(?:自然日|自然天|工作日|工作天|天|日|小时)"
DAY_COUNT = re.compile(r"(?<![第\d０-９零〇一二两三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾佰仟年月])"
                       r"(?P<first>" + _NUMBER + r")\s*"
                       r"(?:(?P<first_unit>" + _UNIT + r")\s*)?"
                       r"(?:(?P<range>至|到|[-—~～])\s*(?P<last>" + _NUMBER + r")\s*)?"
                       r"(?P<unit>" + _UNIT + r")(?![期历界数])")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")

# Concrete eligibility language only: refund timing and shipping durations are
# operational information and must not be classified as return windows.
_ELIGIBILITY_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"(?:可(?:以)?|允许|支持|受理|能(?:够)?|不能|不可|不给|不让|不予|不支持|不接受|不适用|适用)"
    r"\s*(?:申请|办理)?\s*(?:无理由)?\s*(?:退(?:换)?货|换货)",
    r"(?:退(?:换)?货|换货)\s*(?:资格|条件|窗口|期限|时限|申请时间|申请期)",
    r"无理由.{0,8}" + _NUMBER + r"\s*" + _UNIT,
    _NUMBER + r"\s*" + _UNIT + r"\s*无理由",
    r"(?:退货|换货)(?:申请)?.{0,10}" + _NUMBER + r"\s*" + _UNIT
    + r".{0,8}(?:内|以内).{0,8}(?:提交|申请|办理|受理)",
    r"(?:不退|不可退|不能退|不予退|不可换|不能换|不予换)(?:品类|商品|货)?",
    r"(?:质量争议|责任认定|需要鉴定).{0,40}(?:人工|鉴定)",
    r"质量问题[^。；！？]{0,25}(?:必须|需要|应(?:当)?|需|就)[^。；！？]{0,12}"
    r"(?:转(?:交)?|交由|由)人工",
    r"(?:转人工|转交人工|人工处理)\s*(?:条件|触发条件)",
))
_SHORT_ELIGIBILITY = re.compile(r"(?:可(?:以)?|能(?:够)?|不能|不可以|不可|不给|不让|不予|不得|不适用|没法|无法)"
                                r"(?:申请|办理)?(?:无理由)?(?:退|换)(?!回|还|到|费|款|出)")


class CorpusError(ValueError):
    """The corpus breaks its contract; the knowledge base is not built."""


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
class SectionPosition:
    heading_line: int
    # Offsets in the joined section text -> original Markdown line numbers.
    text_offsets: tuple[tuple[int, int], ...]
    end_line: int


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
    positions: tuple[SectionPosition, ...] = ()
    source_lines: tuple[tuple[int, str], ...] = ()
    metadata_lines: tuple[tuple[str, int], ...] = ()
    title_line: int = 1

    def in_force(self, as_of: datetime) -> bool:
        return self.effective_from <= as_of and (self.effective_to is None or as_of < self.effective_to)

    @property
    def body_chars(self) -> int:
        """Non-whitespace body characters, excluding front matter and headings."""
        return sum(len(re.sub(r"\s", "", text)) for _, text in self.sections)


def _error(source: str, line: int, message: str) -> CorpusError:
    return CorpusError(source + ":" + str(line) + ": " + message)


def _metadata_line(document: KnowledgeDocument, key: str) -> int:
    return dict(document.metadata_lines).get(key, 1)


def parse_document(path: Path) -> KnowledgeDocument:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    except (OSError, UnicodeError) as error:
        raise _error(str(path), 1, "cannot read UTF-8 document (" + type(error).__name__ + ")") from None
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise _error(str(path), 1, "front matter must sit between two --- lines")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise _error(str(path), len(lines) or 1, "front matter closing --- is missing") from None
    metadata_lines: dict[str, int] = {}
    for number, line in enumerate(lines[1:end], start=2):
        for match in re.finditer(r'"((?:[^"\\]|\\.)*)"\s*:', line):
            try:
                key = json.loads('"' + match.group(1) + '"')
            except ValueError:
                continue  # The JSON parser below supplies the syntax error line.
            if key in metadata_lines:
                raise _error(str(path), number, "duplicate front matter key: " + key)
            metadata_lines[key] = number
    try:
        meta = json.loads("\n".join(lines[1:end]), parse_constant=lambda value: _invalid_json(value))
    except json.JSONDecodeError as error:
        raise _error(str(path), error.lineno + 1, "front matter is not JSON: " + error.msg) from None
    except ValueError as error:
        raise _error(str(path), 2, str(error)) from None
    if not isinstance(meta, dict) or set(meta) != FRONT_MATTER_KEYS:
        raise _error(str(path), 2, "front matter keys must be " + ", ".join(sorted(FRONT_MATTER_KEYS)))
    def fail(key: str, message: str) -> None:
        raise _error(str(path), metadata_lines.get(key, 2), message)
    if (not isinstance(meta["doc_id"], str) or not _SAFE_ID.fullmatch(meta["doc_id"])
            or meta["doc_id"] != path.stem):
        fail("doc_id", "doc_id must be a safe identifier equal to the file name")
    for key in ("title", "version"):
        if not isinstance(meta[key], str) or not meta[key].strip():
            fail(key, key + " must be a non-empty string")
    if not _SAFE_ID.fullmatch(meta["version"]):
        fail("version", "version must be a safe identifier")
    if not isinstance(meta["doc_type"], str) or meta["doc_type"] not in DOC_TYPES:
        fail("doc_type", "doc_type must be one of " + ", ".join(sorted(DOC_TYPES)))
    scope = meta["scope"]
    if (not isinstance(scope, list) or not all(isinstance(item, str) and item.strip() for item in scope)
            or len(set(scope)) != len(scope)):
        fail("scope", "scope must be a list of unique non-empty category names")
    restates = meta["restates"]
    if restates is not None and (not isinstance(restates, str) or not _SAFE_ID.fullmatch(restates)):
        fail("restates", "restates must be a policy id or null")
    effective_from = _aware(str(path) + ":" + str(metadata_lines.get("effective_from", 2))
                            + ": effective_from", meta["effective_from"])
    effective_to = (None if meta["effective_to"] is None
                    else _aware(str(path) + ":" + str(metadata_lines.get("effective_to", 2))
                                + ": effective_to", meta["effective_to"]))
    if effective_to is not None and effective_to <= effective_from:
        fail("effective_to", "effective_to must be later than effective_from")
    sections: list[tuple[str, str]] = []
    positions: list[SectionPosition] = []
    source_lines: list[tuple[int, str]] = []
    heading: str | None = None
    heading_line = 0
    body_lines: list[tuple[int, str]] = []
    def finish_section() -> None:
        if heading is None:
            return
        if not body_lines:
            raise _error(str(path), heading_line, "every section needs a heading and text")
        offsets, offset = [], 0
        for number, body_line in body_lines:
            offsets.append((offset, number))
            offset += len(body_line) + 1
        sections.append((heading, " ".join(body_line for _, body_line in body_lines)))
        positions.append(SectionPosition(heading_line, tuple(offsets), body_lines[-1][0]))
    fenced = False
    for number, line in enumerate(lines[end + 1:], start=end + 2):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
        match = None if fenced else _HEADING.fullmatch(line)
        source_lines.append((number, match.group(2).strip() if match else stripped))
        if match:
            finish_section()
            heading, heading_line, body_lines = match.group(2).strip(), number, []
        elif heading is None:
            raise _error(str(path), number, "body text must follow a Markdown heading")
        else:
            body_lines.append((number, stripped))
    finish_section()
    if not sections:
        raise _error(str(path), end + 2, "a document needs at least one Markdown heading with text")
    return KnowledgeDocument(doc_id=meta["doc_id"], title=meta["title"], doc_type=meta["doc_type"],
                             scope=tuple(scope), effective_from=effective_from,
                             effective_to=effective_to, version=meta["version"],
                             restates=restates, source="knowledge_base/" + path.name,
                             sections=tuple(sections), positions=tuple(positions),
                             source_lines=tuple(source_lines), metadata_lines=tuple(metadata_lines.items()),
                             title_line=metadata_lines.get("title", 2))


def _invalid_json(value: str) -> None:
    raise ValueError("invalid JSON constant: " + value)


def _document_segments(document: KnowledgeDocument):
    """Scan joined paragraphs, so wrapping a clause cannot bypass a check."""
    yield document.title, ((0, document.title_line),)
    for index, (heading, text) in enumerate(document.sections):
        position = document.positions[index] if document.positions else SectionPosition(1, ((0, 1),), 1)
        offsets = ((0, position.heading_line),) + tuple(
            (len(heading) + 1 + offset, line) for offset, line in position.text_offsets)
        yield heading + " " + text, offsets


def _offset_line(offsets: tuple[tuple[int, int], ...], offset: int) -> int:
    return max((line for start, line in offsets if start <= offset), default=1)


def check_eligibility(document: KnowledgeDocument) -> None:
    """KB-only documents must not assert eligibility, windows, or quality hand-off rules."""
    if document.restates is not None:
        return
    for text, offsets in _document_segments(document):
        for pattern in _ELIGIBILITY_PATTERNS + (_SHORT_ELIGIBILITY,):
            for match in pattern.finditer(text):
                if (pattern is _SHORT_ELIGIBILITY
                        and re.search(r"(?:运费|款项|费用|差额)\s*$", text[max(0, match.start() - 8):match.start()])):
                    continue
                raise _error(document.source, _offset_line(offsets, match.start()),
                             "eligibility statement requires restates: "
                             + match.group(0))


def _integer(text: str) -> int:
    if text.isdecimal():
        return int(text)
    digits = dict(zip("零〇一二两三四五六七八九壹贰叁肆伍陆柒捌玖", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9,
                                                               1, 2, 3, 4, 5, 6, 7, 8, 9)))
    units = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000, "万": 10000}
    if all(character in digits for character in text):
        return int("".join(str(digits[character]) for character in text))
    total, current = 0, 0
    for character in text:
        if character in digits:
            current = digits[character]
        else:
            total += (current or 1) * units[character]
            current = 0
    return total + current


def check_restatement(document: KnowledgeDocument, rules: Mapping[str, object]) -> None:
    """A restating document must agree with its rule: day counts and effective period."""
    if document.restates is None:
        return
    record = rules.get(document.restates)
    if record is None:
        raise _error(document.source, _metadata_line(document, "restates"),
                     "restates an unknown rule " + document.restates)
    window_days = record.params.get("window_days")
    rule_from = datetime.fromisoformat(record.effective_from)
    rule_to = None if record.effective_to is None else datetime.fromisoformat(record.effective_to)
    if document.effective_from < rule_from or (rule_to is not None and (
            document.effective_to is None or document.effective_to > rule_to)):
        raise _error(document.source, _metadata_line(document, "effective_from"),
                     "restates a rule outside the rule's effective period")
    counts = 0
    for text, offsets in _document_segments(document):
        for found in DAY_COUNT.finditer(text):
            number = _offset_line(offsets, found.start())
            if (text[max(0, found.start() - 2):found.start()] == "最后"
                    and _integer(found.group("first")) == 1 and found.group("last") is None
                    and found.group("unit") in {"日", "天"}):
                continue
            counts += 1
            values = [_integer(found.group("first"))]
            if found.group("last") is not None:
                values.append(_integer(found.group("last")))
            units = [found.group("unit"), found.group("first_unit") or found.group("unit")]
            if (window_days is None or any(value != window_days for value in values)
                    or any("工作" in unit or "时" in unit for unit in units)):
                raise _error(document.source, number, "a restated day count/unit differs from "
                             + document.restates + " window_days=" + str(window_days)
                             + " (natural days): " + found.group(0))
            # A matching number must not silently bind to the other kind of
            # action, e.g. a 15-day exchange sentence citing the promotion rule.
            other_action = "换货" if record.rule_type.value == "return_window" else "退货"
            clause_start = max(text.rfind(mark, 0, found.start()) for mark in "。；！？") + 1
            clause_end = min((position for mark in "。；！？"
                              if (position := text.find(mark, found.end())) >= 0), default=len(text))
            if other_action in text[clause_start:clause_end]:
                raise _error(document.source, number, "numbered " + other_action
                             + " clause cites a different rule: " + document.restates)
    if window_days is not None and counts == 0:
        raise _error(document.source, document.title_line, "window restatement must state "
                     + document.restates + " window_days=" + str(window_days))


def published_rules() -> dict[str, object]:
    """policy_id -> PolicyRecord of the published catalog (the frozen 6 rules)."""
    return {record.policy_id: record for record in PublishedPolicyCatalog().snapshot().records}


def _reviewed_short_bodies(directory: Path,
                           documents: Sequence[KnowledgeDocument]) -> dict[str, str]:
    """Load explicit content-bound length approvals for the formal build only."""
    path = directory / "reviewed-short-bodies.json"
    if not path.exists():
        return {}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise _error(str(path), 1, "cannot read short-body approvals ("
                     + type(error).__name__ + ")") from None
    if (not isinstance(record, dict)
            or set(record) != {"schema_version", "user_confirmed", "hash_normalization", "exceptions"}
            or type(record["schema_version"]) is not int or record["schema_version"] != 1
            or record["user_confirmed"] is not True
            or record["hash_normalization"] != "utf-8-lf"
            or not isinstance(record["exceptions"], list)):
        raise _error(str(path), 1, "invalid user-confirmed short-body approval manifest")
    approvals: dict[str, str] = {}
    document_ids = {document.doc_id for document in documents}
    for entry in record["exceptions"]:
        if (not isinstance(entry, dict)
                or set(entry) != {"doc_id", "normalized_markdown_sha256"}
                or not isinstance(entry["doc_id"], str)
                or entry["doc_id"] not in document_ids
                or entry["doc_id"] in approvals
                or not isinstance(entry["normalized_markdown_sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", entry["normalized_markdown_sha256"]) is None):
            raise _error(str(path), 1, "invalid or duplicate short-body approval entry")
        # read_text normalizes CRLF/CR to LF, retaining all other Markdown text.
        normalized = (directory / (entry["doc_id"] + ".md")).read_text(encoding="utf-8")
        if content_hash(normalized) != entry["normalized_markdown_sha256"]:
            raise _error(str(path), 1, "short-body approval content hash differs for " + entry["doc_id"])
        approvals[entry["doc_id"]] = entry["normalized_markdown_sha256"]
    return approvals


def load_corpus(directory: Path = CORPUS_DIRECTORY,
                rules: Mapping[str, object] | None = None, *,
                validate_lengths: bool = False) -> tuple[KnowledgeDocument, ...]:
    directory = Path(directory)
    if not directory.is_dir():
        raise _error(str(directory), 1, "corpus directory does not exist")
    rules = published_rules() if rules is None else rules
    documents = tuple(parse_document(path) for path in sorted(Path(directory).glob("*.md")))
    if not documents:
        raise _error(str(directory), 1, "corpus contains no Markdown documents")
    if len({document.doc_id for document in documents}) != len(documents):
        raise _error(str(directory), 1, "doc_id must be unique")
    short_body_approvals = _reviewed_short_bodies(directory, documents) if validate_lengths else {}
    for document in documents:
        check_eligibility(document)
        check_restatement(document, rules)
        if validate_lengths and (document.body_chars > 800
                or (document.body_chars < 150 and document.doc_id not in short_body_approvals)):
            raise _error(document.source, document.positions[0].heading_line,
                         "body must contain 150-800 non-whitespace characters excluding headings, found "
                         + str(document.body_chars))
    return documents


# --------------------------------------------------------------------------
# Passages
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Passage:
    document: KnowledgeDocument
    section: str
    text: str
    start_line: int = 1
    end_line: int = 1
    heading_line: int = 1

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
    passages = []
    for index, (heading, text) in enumerate(document.sections):
        position = document.positions[index] if document.positions else SectionPosition(1, ((0, 1),), 1)
        offset = 0
        for piece in _pieces(text):
            last = offset + len(piece) - 1
            start_line = max((line for start, line in position.text_offsets if start <= offset), default=1)
            end_line = max((line for start, line in position.text_offsets if start <= last), default=start_line)
            passages.append(Passage(document=document, section=heading, text=piece,
                                    start_line=start_line, end_line=end_line,
                                    heading_line=position.heading_line))
            offset += len(piece)
    return tuple(passages)


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
                or not all(isinstance(vector, list) and vector
                           and all(isinstance(item, (int, float)) and not isinstance(item, bool)
                                   and math.isfinite(item) for item in vector) for vector in vectors)
                or len({len(vector) for vector in vectors}) > 1):
            raise EmbeddingUnavailable("malformed embeddings")
        return vectors


def cached_embedder(*, cache_dir: Path = EMBEDDING_CACHE_DIRECTORY,
                    model: str = EMBEDDING_MODEL, offline: bool = False,
                    base_url: str = DEFAULT_OLLAMA_URL) -> CachedEmbedder:
    return CachedEmbedder(cache_dir, model=model, offline=offline,
                          provider=None if offline else OllamaEmbedder(base_url=base_url, model=model))


def environment_embedder() -> CachedEmbedder:
    setting = os.environ.get("AFTERSALES_KB_OFFLINE", "0").lower().strip()
    if setting not in {"0", "1", "false", "true"}:
        raise ValueError("AFTERSALES_KB_OFFLINE must be 0, 1, false or true")
    return cached_embedder(cache_dir=Path(os.environ.get("AFTERSALES_KB_EMBED_CACHE")
                                          or EMBEDDING_CACHE_DIRECTORY),
                           offline=setting in {"1", "true"},
                           base_url=os.environ.get("AFTERSALES_KB_OLLAMA_URL") or DEFAULT_OLLAMA_URL)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise EmbeddingUnavailable("query and passage embedding dimensions differ")
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
                if any(len(vector) != len(question) for vector in vectors):
                    raise EmbeddingUnavailable("query and passage embedding dimensions differ")
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
            _shared = KnowledgeBase(load_corpus(), embedder=environment_embedder())
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
                                   "rank": rank, "heading_line": passage.heading_line,
                                   "start_line": passage.start_line, "end_line": passage.end_line,
                                   TRACE_OBSERVATION_ID: observation_id}
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


def build_corpus(directory: Path = CORPUS_DIRECTORY, *,
                 cache_dir: Path = EMBEDDING_CACHE_DIRECTORY, offline: bool = False,
                 model: str = EMBEDDING_MODEL, base_url: str = DEFAULT_OLLAMA_URL) -> dict[str, object]:
    """Validate the formal corpus and materialize its index vectors.

    No queries, evaluations, tuning, or external customer data are involved.
    Unlike product search, an embedding failure makes this build fail.
    """
    documents = load_corpus(directory, validate_lengths=True)
    passages = tuple(passage for document in documents for passage in split_passages(document))
    embedder = cached_embedder(cache_dir=cache_dir, model=model, offline=offline, base_url=base_url)
    try:
        vectors = embedder.embed([passage.search_text for passage in passages])
    except EmbeddingCacheMiss as error:
        absent = set(error.content_hashes)
        details = [passage.document.source + ":" + str(passage.start_line)
                   + ": embedding cache miss model=" + model
                   + " content_sha256=" + content_hash(passage.search_text)
                   for passage in passages if content_hash(passage.search_text) in absent]
        raise CorpusError("\n".join(details)) from None
    except EmbeddingUnavailable as error:
        affected = set(getattr(error, "content_hashes", ()))
        failing = [passage for passage in passages
                   if not affected or content_hash(passage.search_text) in affected]
        details = [passage.document.source + ":" + str(passage.start_line)
                   + ": embedding build failed: " + str(error) for passage in failing]
        raise CorpusError("\n".join(details)) from None
    types = {doc_type: sum(document.doc_type == doc_type for document in documents)
             for doc_type in sorted(DOC_TYPES)}
    return {"documents": len(documents), "passages": len(passages), "doc_types": types,
            "restatements": sum(document.restates is not None for document in documents),
            "kb_only": sum(document.restates is None for document in documents),
            "eligibility_lint": "passed", "restated_numbers": "passed", "body_length": "passed",
            "min_body_chars": min(document.body_chars for document in documents),
            "max_body_chars": max(document.body_chars for document in documents),
            "embedding_model": model, "embedding_dimensions": len(vectors[0]),
            "cache_dir": str(Path(cache_dir).resolve()), "offline": offline}


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Build and validate the M3 knowledge corpus")
    parser.add_argument("--corpus-dir", type=Path, default=CORPUS_DIRECTORY)
    parser.add_argument("--cache-dir", type=Path, default=EMBEDDING_CACHE_DIRECTORY)
    parser.add_argument("--model", default=EMBEDDING_MODEL)
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--offline", action="store_true", help="read cache only; fail on any missing vector")
    arguments = parser.parse_args(argv)
    try:
        summary = build_corpus(arguments.corpus_dir, cache_dir=arguments.cache_dir,
                               offline=arguments.offline, model=arguments.model,
                               base_url=arguments.ollama_url)
    except CorpusError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
