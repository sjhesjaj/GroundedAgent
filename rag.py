from __future__ import annotations

import io
import json
import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

import requests
import jieba
from pypdf import PdfReader

import llm_provider


# Embeddings and the health check stay on local Ollama; chat goes through llm_provider.
OLLAMA_URL = "http://localhost:11434"
CHAT_MODEL = llm_provider.load_config().model
EMBED_MODEL = "nomic-embed-text"
CACHE_FILE = Path(".cache/embeddings.json")
REFUSAL_MARKERS = ("无法确定", "没有相关", "未提及", "资料不足", "无法回答")


@dataclass
class Chunk:
    text: str
    source: str
    index: int
    embedding: list[float] | None = None


def check_ollama() -> tuple[bool, str]:
    try:
        response = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        response.raise_for_status()
        names = [item["name"] for item in response.json().get("models", [])]
        return True, "、".join(names) if names else "服务已启动，但尚未下载模型"
    except requests.RequestException as exc:
        return False, f"无法连接 Ollama：{exc}"


def read_file(name: str, raw: bytes) -> str:
    suffix = name.lower().rsplit(".", 1)[-1]
    if suffix == "pdf":
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if suffix in {"txt", "md"}:
        for encoding in ("utf-8", "gb18030"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                pass
    raise ValueError(f"暂不支持文件：{name}")


def split_text(text: str, source: str, size: int = 220, overlap: int = 40) -> list[Chunk]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # Markdown 优先按二级标题分节，避免把不同主题混在同一文本块中。
    sections: list[str] = []
    current: list[str] = []
    document_title = ""
    for line in lines:
        if line.startswith("# ") and not document_title:
            document_title = line
            continue
        if line.startswith("## ") and current:
            sections.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        sections.append("\n".join(current))
    cleaned_sections = sections or ["\n".join(lines)]
    chunks: list[Chunk] = []
    for section in cleaned_sections:
        prefix = f"{document_title}\n" if document_title else ""
        start = 0
        while start < len(section):
            end = min(start + size, len(section))
            piece = (prefix + section[start:end]).strip()
            if piece:
                chunks.append(Chunk(text=piece, source=source, index=len(chunks) + 1))
            if end == len(section):
                break
            start = end - overlap
    return chunks


def reindex_chunks(chunks: list[Chunk]) -> list[Chunk]:
    """Assign stable, globally unique positions after merging multiple files."""
    for index, chunk in enumerate(chunks, start=1):
        chunk.index = index
    return chunks


def embed(text: str) -> list[float]:
    return embed_many([text])[0]


def embed_many(texts: list[str]) -> list[list[float]]:
    response = requests.post(
        f"{OLLAMA_URL}/api/embed",
        json={"model": EMBED_MODEL, "input": texts},
        timeout=120,
    )
    response.raise_for_status()
    return response.json()["embeddings"]


def build_index(chunks: list[Chunk], stats: dict | None = None) -> list[Chunk]:
    started = perf_counter()
    cache: dict[str, list[float]] = {}
    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}
    changed = False
    cache_hits = 0
    for chunk in chunks:
        cache_key = hashlib.sha256(
            f"{EMBED_MODEL}\n{chunk.source}\n{chunk.text}".encode("utf-8")
        ).hexdigest()
        if cache_key in cache:
            chunk.embedding = cache[cache_key]
            cache_hits += 1
        else:
            chunk.embedding = embed(chunk.text)
            cache[cache_key] = chunk.embedding
            changed = True
    if changed:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache), encoding="utf-8")
    if stats is not None:
        stats.update({
            "chunks": len(chunks),
            "cache_hits": cache_hits,
            "cache_misses": len(chunks) - cache_hits,
            "index_seconds": perf_counter() - started,
        })
    return chunks


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def retrieve(
    question: str,
    chunks: list[Chunk],
    top_k: int = 4,
    question_vector: list[float] | None = None,
) -> list[tuple[Chunk, float]]:
    question_vector = question_vector or embed(question)
    scored = [(chunk, cosine(question_vector, chunk.embedding or [])) for chunk in chunks]
    return sorted(scored, key=lambda item: item[1], reverse=True)[:top_k]


def lexical_tokens(text: str) -> list[str]:
    """使用 Jieba 为中文分词，同时保留英文、数字和下划线词项。"""
    lowered = text.lower()
    return [
        token.strip()
        for token in jieba.lcut(lowered)
        if token.strip() and re.search(r"[a-z0-9_\u4e00-\u9fff]", token)
    ]


def bm25_rank(question: str, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75) -> list[tuple[Chunk, float]]:
    query_tokens = lexical_tokens(question)
    documents = [lexical_tokens(chunk.text) for chunk in chunks]
    average_length = sum(map(len, documents)) / len(documents) if documents else 1
    document_frequency = Counter()
    for tokens in documents:
        document_frequency.update(set(tokens))

    ranked: list[tuple[Chunk, float]] = []
    total = len(documents)
    for chunk, tokens in zip(chunks, documents):
        frequencies = Counter(tokens)
        score = 0.0
        for token in query_tokens:
            frequency = frequencies[token]
            if not frequency:
                continue
            df = document_frequency[token]
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            denominator = frequency + k1 * (1 - b + b * len(tokens) / average_length)
            score += idf * frequency * (k1 + 1) / denominator
        ranked.append((chunk, score))
    return sorted(ranked, key=lambda item: item[1], reverse=True)


def hybrid_retrieve(
    question: str,
    chunks: list[Chunk],
    top_k: int = 4,
    rrf_k: int = 60,
    vector_weight: float = 1.0,
    keyword_weight: float = 1.2,
    question_vector: list[float] | None = None,
) -> list[tuple[Chunk, float]]:
    """使用 RRF 融合向量排名与 BM25 排名。"""
    vector_results = retrieve(question, chunks, top_k=len(chunks), question_vector=question_vector)
    keyword_results = bm25_rank(question, chunks)
    scores = {chunk.index: 0.0 for chunk in chunks}
    by_index = {chunk.index: chunk for chunk in chunks}
    for rank, (chunk, _) in enumerate(vector_results, start=1):
        scores[chunk.index] += vector_weight / (rrf_k + rank)
    for rank, (chunk, _) in enumerate(keyword_results, start=1):
        scores[chunk.index] += keyword_weight / (rrf_k + rank)
    fused = [(by_index[index], score) for index, score in scores.items()]
    return sorted(fused, key=lambda item: item[1], reverse=True)[:top_k]


def rerank(question: str, candidates: list[tuple[Chunk, float]], top_k: int = 4) -> list[tuple[Chunk, float]]:
    """让本地聊天模型对候选片段做一次语义重排序。"""
    candidate_text = "\n\n".join(
        f"候选ID={chunk.index}\n{chunk.text}" for chunk, _ in candidates
    )
    messages = [
        {
            "role": "system",
            "content": (
                "你是检索重排序器。根据用户问题判断候选片段的相关性。"
                "只输出JSON，格式为{\"ranking\":[片段ID,...]}。"
                "最能直接回答问题的片段排在最前，不要解释。 /no_think"
            ),
        },
        {"role": "user", "content": f"问题：{question}\n\n{candidate_text}"},
    ]
    try:
        content = llm_provider.get_provider().chat(messages, response_format="json").raw_content
        if "</think>" in content:
            content = content.rsplit("</think>", 1)[-1]
        ranking = [int(value) for value in json.loads(content)["ranking"]]
        by_index = {chunk.index: chunk for chunk, _ in candidates}
        # 模型可能把同一个ID排两次；不去重的话，同一片段会占掉 top_k 里的两个名额。
        ordered: list[Chunk] = []
        for index in ranking:
            if index in by_index and index not in {item.index for item in ordered}:
                ordered.append(by_index[index])
        ordered.extend(chunk for chunk, _ in candidates if chunk.index not in {item.index for item in ordered})
        return [(chunk, 1.0 / rank) for rank, chunk in enumerate(ordered[:top_k], start=1)]
    except (requests.RequestException, KeyError, ValueError, TypeError, json.JSONDecodeError):
        return candidates[:top_k]


class _EvidenceSelection(list):
    """Internal list-compatible selection with explicit clause-to-source links."""

    def __init__(self, items, coverage):
        super().__init__(items)
        self.coverage = coverage


def select_for_subquestions(
    sub_questions: list[str], candidates: list[tuple[Chunk, float]]
) -> list[tuple[Chunk, float]]:
    """选择覆盖各个可回答分句的片段；一次合并查询可能需要多条证据。"""
    candidate_text = "\n\n".join(
        f"候选ID={chunk.index}\n{chunk.text}" for chunk, _ in candidates
    )
    # Recall is limited to three merged queries. Selection checks their original
    # clauses in this same request, without doing additional retrieval rounds.
    clauses = list(dict.fromkeys(part for q in sub_questions for part in q.split("；") if part))
    questions = "\n".join(f"分句{i}：{q}" for i, q in enumerate(clauses, start=1))
    schema = {
        "type": "object", "required": ["selections"],
        "properties": {"selections": {"type": "array", "items": {
            "type": "object", "required": ["clause", "ids"],
            "properties": {"clause": {"type": "integer"},
                           "ids": {"type": "array", "items": {"type": "integer"}}},
        }}},
    }
    messages = [
        {
            "role": "system",
            "content": (
                "你是检索片段选择器。逐个检查每组问题里的全部分句，选择直接包含答案的候选ID。"
                "一组可能问多件事，需要不同片段时可选多个ID；同一片段回答多个分句时，在每个分句下分别列出它。"
                "分句没有对应资料时不要为它凑一条，也不要选择仅主题相似的片段。"
                "选择时保留适用对象、条件和所问属性；例如询问最晚到岗时间，要有明确到岗时间。"
                "优先选择能覆盖更多所问事实的片段。"
                "逐个返回分句编号与对应候选ID；没有直接依据的分句用空数组。"
                "只输出JSON：{\"selections\":[{\"clause\":1,\"ids\":[片段ID,...]},...]}。 /no_think"
            ),
        },
        {"role": "user", "content": f"{questions}\n\n{candidate_text}"},
    ]
    try:
        content = llm_provider.get_provider().chat(messages, response_format=schema).raw_content
        if "</think>" in content:
            content = content.rsplit("</think>", 1)[-1]
        selections = json.loads(content)["selections"]
        by_index = {chunk.index: chunk for chunk, _ in candidates}
        ordered: list[Chunk] = []
        coverage: dict[int, set[int]] = {}
        for selection in selections:
            if isinstance(selection, dict):
                clause = int(selection["clause"])
                if not 1 <= clause <= len(clauses):
                    continue
                indices = selection["ids"]
            else:
                # Older scripted/model responses used a flat list of IDs.
                # Keep that fallback but do not invent ownership for its items.
                clause, indices = None, [selection]
            for value in indices:
                index = int(value)
                if index not in by_index:
                    continue
                if clause is not None:
                    coverage.setdefault(index, set()).add(clause)
                if index not in {chunk.index for chunk in ordered}:
                    ordered.append(by_index[index])
        if not ordered:
            raise ValueError("模型没有返回有效候选ID")
        # 多个分句可以由同一片段回答。去重后条数少于查询数并不代表漏选，
        # 不能按候选顺序补入没有被选择的主题来凑数。
        return _EvidenceSelection(
            [(chunk, 1.0 / rank) for rank, chunk in enumerate(ordered, start=1)], coverage
        )
    except (requests.RequestException, KeyError, ValueError, TypeError, json.JSONDecodeError):
        return rerank("；".join(sub_questions), candidates, top_k=len(sub_questions))


def retrieve_with_rerank(
    question: str,
    chunks: list[Chunk],
    top_k: int = 4,
    candidate_k: int = 8,
    trace: dict | None = None,
) -> list[tuple[Chunk, float]]:
    """复合问题先分解召回候选，再由 Qwen3 做最终重排序。

    返回**至多 `top_k` 条**。子查询至多 `MAX_SUB_QUESTIONS` 个，那是召回的轮数；
    证据条数由调用方的 `top_k` 决定，两件事分开限制（见 `fit_to_budget`）。
    """
    total_started = perf_counter()
    split_started = perf_counter()
    clauses = split_clauses(question) or [question]
    sub_questions = merge_sub_questions(clauses)
    split_seconds = perf_counter() - split_started

    recall_started = perf_counter()
    candidate_map: dict[int, tuple[Chunk, float]] = {}
    per_query_k = max(top_k, min(candidate_k, len(chunks)))
    question_vectors = embed_many(sub_questions)
    for sub_question, question_vector in zip(sub_questions, question_vectors):
        for chunk, score in hybrid_retrieve(
            sub_question, chunks, top_k=per_query_k, question_vector=question_vector
        ):
            previous = candidate_map.get(chunk.index)
            if previous is None or score > previous[1]:
                candidate_map[chunk.index] = (chunk, score)
    candidates = sorted(candidate_map.values(), key=lambda item: item[1], reverse=True)
    recall_seconds = perf_counter() - recall_started
    rerank_started = perf_counter()
    if len(sub_questions) > 1:
        selected = select_for_subquestions(sub_questions, candidates)
        relevance = {chunk.index: score for chunk, score in candidates}
        # 选择器可以给合并查询返回多条必要证据；它们都参与相关性裁决，
        # 不能把超过查询数的部分当作可按位置丢弃的余量。
        results = fit_to_budget(
            selected, len(selected), relevance, top_k,
            coverage=getattr(selected, "coverage", None),
        )
        if len(clauses) > len(sub_questions):
            results = cover_merged_clauses(
                results, clauses, candidates, chunks, top_k,
                coverage=getattr(selected, "coverage", None),
            )
    else:
        results = rerank(question, candidates, top_k=top_k)
    if not results:
        results = candidates[:top_k]
    if trace is not None:
        trace.update({
            "clauses": len(clauses),
            "sub_questions": len(sub_questions),
            "candidates": len(candidates),
            "split_seconds": split_seconds,
            "recall_seconds": recall_seconds,
            "rerank_seconds": perf_counter() - rerank_started,
            "retrieval_total_seconds": perf_counter() - total_started,
        })
    return results


# 一个问题至多拆成几个检索子查询。子查询数就是召回的轮数——每轮一次向量化、一次
# 混合检索——问题越长拆得越多，计算与上下文就越膨胀、耗时越难预测。超出时**合并**，
# 不丢弃：每个原始分句都还在某个子查询里（`merge_sub_questions`）。
MAX_SUB_QUESTIONS = 3

# 快路径判定"BM25 足够自信"的两条线：首名分数，以及首名对次名的倍数。
BM25_CONFIDENT_SCORE = 3.0
BM25_CONFIDENT_RATIO = 1.5


def bm25_confident(ranked: list[tuple[Chunk, float]]) -> bool:
    """BM25 的首名是否明显领先——够不够把它当成"这句问的就是这一段"。"""
    first = ranked[0][1] if ranked else 0
    second = ranked[1][1] if len(ranked) > 1 else 0
    return first >= BM25_CONFIDENT_SCORE and (
        second == 0 or first / second >= BM25_CONFIDENT_RATIO
    )


def decompose_question(question: str) -> list[str]:
    """拆分复合问题，得到**至多 `MAX_SUB_QUESTIONS` 个**检索子查询。"""
    return merge_sub_questions(split_clauses(question))


def split_clauses(question: str) -> list[str]:
    """拆分标点分隔的复合问题和常见比较型问题；不设上限，上限由合并负责。"""
    parts = [
        part.strip(" ，,。")
        for part in re.split(r"[？?；;]+", question)
        if part.strip(" ，,。")
    ]
    if len(parts) > 1:
        # “需要谁审批”一类省略主语的追问必须继承前一问上下文，
        # 否则会只按“审批”召回转正、报销等无关制度。
        contextualized = [parts[0]]
        for part in parts[1:]:
            if re.match(r"^(需要|由谁|谁|如何|怎么|是否|能否)", part):
                contextualized.append(f"{parts[0]}；{part}")
            else:
                contextualized.append(part)
        parts = contextualized
    if len(parts) == 1:
        text = re.sub(r"^(请|帮我)?(比较|对比)", "", parts[0]).strip()
        match = re.match(r"(.+?)(?:和|与|及)(.+?)(?:有什么区别|的区别|区别|制度)?$", text)
        if match:
            left, right = match.group(1).strip(), match.group(2).strip()
            return [f"{left}的相关规定", f"{right}的相关规定"]
    return parts


def _shared_words(left: str, right: str) -> int:
    """两个相邻子查询共有多少个实词（两字以上的词）。"""
    def words(text: str) -> set[str]:
        return {token for token in lexical_tokens(text) if len(token) >= 2}
    return len(words(left) & words(right))


def _join_sub_questions(left: str, right: str) -> str:
    """合并两个子查询，去掉重复的分句：追问继承的前缀会在两边各出现一次。"""
    segments: list[str] = []
    for segment in left.split("；") + right.split("；"):
        if segment and segment not in segments:
            segments.append(segment)
    return "；".join(segments)


def merge_sub_questions(parts: list[str], limit: int = MAX_SUB_QUESTIONS) -> list[str]:
    """把子查询合并到至多 `limit` 个，原始分句一个都不丢。

    只合并**相邻**的两个：分句的先后就是提问的先后，追问也总紧跟在它追问的那句后面。
    每次合并共有实词最多的那一对——`年假多少天` 与 `年假多少天；谁审批` 说的是同一件
    事，先合它们。都不共有时合并后最短的那一对，免得一个子查询吞掉大半个问题；
    再相同就取靠前的，结果与哈希种子无关。
    """
    merged = list(parts)
    while len(merged) > limit:
        best = max(
            range(len(merged) - 1),
            key=lambda i: (
                _shared_words(merged[i], merged[i + 1]),
                -len(_join_sub_questions(merged[i], merged[i + 1])),
                -i,
            ),
        )
        merged[best : best + 2] = [_join_sub_questions(merged[best], merged[best + 1])]
    return merged


def fit_to_budget(
    selected: list[tuple[Chunk, float]],
    owned: int,
    relevance: dict[int, float],
    top_k: int,
    *,
    coverage: dict[int, set[int]] | None = None,
) -> list[tuple[Chunk, float]]:
    """把证据压到至多 `top_k` 条。

    有明确的分句归属时，先选能覆盖最多未覆盖分句的证据，同增益再比较相关性。
    同一问题的重复依据不能挤掉另一问题的唯一依据。预算不足时仍严格守住上限；
    没有归属信息的旧调用保留下面的有界相关性选择。

    `selected` 的前 `owned` 条是**按子问题顺序**排的——第 i 条对应第 i 个子问题，
    这不是相关性顺序。直接 `[:top_k]` 砍掉的就是最后几问的依据，而复合问题最容易
    漏答的恰恰是后面几问。所以：

    1. 先让出 `owned` 之后的部分——那是超出"每问一条"的余量；
    2. 还超，就去掉**相关性最低**的那一问的依据，不是排在最后的那一问。

    留下的仍按原先后排列，引用编号的次序不因此打乱。
    """
    if len(selected) <= top_k:
        return selected
    if coverage:
        pending = list(range(len(selected)))
        chosen: list[int] = []
        covered: set[int] = set()
        while pending and len(chosen) < top_k:
            best = max(pending, key=lambda i: (
                len(coverage.get(selected[i][0].index, set()) - covered),
                relevance.get(selected[i][0].index, 0.0), -i,
            ))
            pending.remove(best)
            chosen.append(best)
            covered.update(coverage.get(selected[best][0].index, set()))
        return [selected[i] for i in sorted(chosen)]
    covering, surplus = selected[:owned], selected[owned:]
    if len(covering) <= top_k:
        return covering + surplus[: top_k - len(covering)]
    order = sorted(
        range(len(covering)),
        key=lambda i: (-relevance.get(covering[i][0].index, 0.0), i),
    )
    return [covering[i] for i in sorted(order[:top_k])]


def cover_merged_clauses(
    results: list[tuple[Chunk, float]],
    clauses: list[str],
    candidates: list[tuple[Chunk, float]],
    chunks: list[Chunk],
    top_k: int,
    *,
    coverage: dict[int, set[int]] | None = None,
) -> list[tuple[Chunk, float]]:
    """合并后逐个检查原始分句，补上召回池中的强匹配证据。

    只接纳通过既有 BM25 置信线、且全库首名已在召回池中的片段。
    有空位时沿用按匹配分补齐的顺序；满额时也让这些片段参与预算分配，
    不能因为选择器已经占满名额，就丢掉合并前另一分句的明确匹配。

    满额时保留选择器的语义归属，同时加入逐句词面匹配信号。两类信号分开
    计数：词面首名不等于语义上确实回答了该分句，不能用它抹掉选择器的归属。
    仍按覆盖增益、相关性、原始顺序裁决，至多 top_k 条；这是有界启发式，
    不保证任意问题达到真实语义的最优覆盖。弱匹配和未召回片段均不参与补齐。
    """
    pool = {chunk.index: score for chunk, score in candidates}
    present = {chunk.index for chunk, _ in results}
    wanted: list[tuple[float, int, Chunk]] = []
    signals = {index: set(owners) for index, owners in (coverage or {}).items()}
    for position, clause in enumerate(clauses):
        ranked = bm25_rank(clause, chunks)
        if not bm25_confident(ranked):
            continue
        best, score = ranked[0]
        if best.index not in pool:
            continue
        # Positive IDs belong to the model's flattened clauses. Negative IDs
        # describe lexical matches on the original clauses, a separate signal.
        signals.setdefault(best.index, set()).add(-position - 1)
        if best.index not in present:
            wanted.append((score, position, best))
    filled = list(results)
    for _score, _position, chunk in sorted(wanted, key=lambda item: (-item[0], item[1])):
        if chunk.index not in {item.index for item, _ in filled}:
            filled.append((chunk, pool[chunk.index]))
    if len(results) < top_k:
        return filled[:top_k]
    return fit_to_budget(filled, len(filled), pool, top_k, coverage=signals)

# 分数低于最佳命中这一比例的结果，是为凑满 top_k 而返回的，不是因为它匹配。
# 把它们送进提示词只会稀释真正的依据，并给模型多一段可以误引的文本。
PADDING_SCORE_RATIO = 0.2


def drop_padding_results(
    ranked: list[tuple[Chunk, float]]
) -> list[tuple[Chunk, float]]:
    """去掉只为填满名额而返回的尾部结果。

    仅在检索器已给出真实相关性分数时可用。最佳命中永远保留：本函数减少噪声，
    不负责判断"无依据"，那仍由证据策略和拒答链决定。
    """
    if not ranked:
        return ranked
    floor = ranked[0][1] * PADDING_SCORE_RATIO
    kept = [ranked[0]]
    kept.extend(item for item in ranked[1:] if item[1] > 0 and item[1] >= floor)
    return kept


def retrieve_fast(question: str, chunks: list[Chunk], top_k: int = 4, trace: dict | None = None) -> list[tuple[Chunk, float]]:
    """BM25明显领先时跳过Embedding与Reranker，否则使用完整检索链。"""
    clauses = split_clauses(question)
    parts = merge_sub_questions(clauses)
    if len(parts) > 1:
        started = perf_counter()
        selected: list[tuple[Chunk, float]] = []
        relevance: dict[int, float] = {}
        coverage: dict[int, set[int]] = {}
        confident = True
        for part_index, part in enumerate(parts, 1):
            ranked = bm25_rank(part, chunks)
            if not bm25_confident(ranked):
                confident = False
                break
            top, first = ranked[0]
            relevance[top.index] = max(relevance.get(top.index, 0.0), first)
            coverage.setdefault(top.index, set()).add(part_index)
            if top.index not in {chunk.index for chunk, _ in selected}:
                selected.append((top, first))
        if confident and len(clauses) > len(parts):
            # 合并后的总分可被其中一个分句主导；不能据此断言其余分句都有依据。
            # 这里只核对覆盖，不新增向量化、混合召回或模型调用。任一原始分句不够
            # 明确，或它的最佳片段不在选择中，就交给已有的语义选择链一起判断。
            present = {chunk.index for chunk, _ in selected}
            coverage.clear()
            for clause_index, clause in enumerate(clauses, 1):
                ranked = bm25_rank(clause, chunks)
                if not bm25_confident(ranked) or ranked[0][0].index not in present:
                    confident = False
                    break
                coverage.setdefault(ranked[0][0].index, set()).add(clause_index)
        if confident and selected:
            # 按分句顺序 append 的，同样不能直接截：超出时去掉 BM25 分最低的那一问。
            kept = fit_to_budget(selected, len(selected), relevance, top_k, coverage=coverage)
            results = [(chunk, 1.0 / rank) for rank, (chunk, _) in enumerate(kept, start=1)]
            if trace is not None:
                elapsed = perf_counter() - started
                trace.update({
                    "retrieval_path": "bm25_multi_fast", "sub_questions": len(parts),
                    "candidates": len(selected), "split_seconds": 0.0,
                    "recall_seconds": elapsed, "rerank_seconds": 0.0,
                    "retrieval_total_seconds": elapsed,
                })
            return results
    else:
        started = perf_counter()
        bm25_results = bm25_rank(question, chunks)
        if bm25_confident(bm25_results):
            kept = drop_padding_results(bm25_results[:top_k])
            results = [(chunk, 1.0 / rank) for rank, (chunk, _) in enumerate(kept, start=1)]
            if trace is not None:
                elapsed = perf_counter() - started
                trace.update({
                    "retrieval_path": "bm25_fast", "sub_questions": 1,
                    "candidates": len(bm25_results), "split_seconds": 0.0,
                    "recall_seconds": elapsed, "rerank_seconds": 0.0,
                    "retrieval_total_seconds": elapsed,
                    "padding_dropped": len(bm25_results[:top_k]) - len(kept),
                })
            return results
    if trace is not None:
        trace["retrieval_path"] = "hybrid_rerank"
    return retrieve_with_rerank(question, chunks, top_k=top_k, trace=trace)


def answer(question: str, results: list[tuple[Chunk, float]], history: list[dict]) -> str:
    messages = build_answer_messages(question, results, history)
    # 这一支即使没有要求结构化输出，模型仍可能返回一个合法的 {"answer": ...}
    # 信封。原来只剥 think 标签，于是整个信封被当成正文发给用户并落库。
    # 统一走 extract_answer_text：是信封就取字段，不是就按纯文本处理。
    # 取 raw_content：清理顺序由 extract_answer_text 决定，provider 不先去壳。
    return extract_answer_text(llm_provider.get_provider().chat(messages).raw_content)


ANSWER_SCHEMA = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
}


def _json_answer(content: str) -> str | None:
    """`{"answer": "..."}` 里的正文；不是这个形状就返回 None。"""
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(parsed, dict) and isinstance(parsed.get("answer"), str):
        return parsed["answer"]
    return None


def extract_answer_text(content: str) -> str:
    """把模型返回的内容取成可交付的正文。清理顺序在这里，只在这里。

    三种外形要汇合到同一个出口，否则总有一条支路把没清理过的东西发给用户：

    1. 合法的 `{"answer": "..."}` 信封 —— 取字段；
    2. `<think>…</think>` 外壳 —— 去壳；
    3. **外壳套着信封** —— 去壳之后**必须再看一次**是不是信封。

    第 3 种是验收复现的那一个：原来只试一次 JSON 解析，失败就去壳直接返回，
    刚露出来的 `{"answer":…}` 就整个当正文发出去并落库了。

    顺序是先原样试信封、再去壳试信封。这个方向不能反：answer 字段里合法地写着
    `<think>` 字样时，先去壳会把正文本身改坏。

    只处理上面这几种形态。任意 Markdown/HTML 外层不在范围内，也不该在这里兜底——
    解析不出来的结构化输出仍然走原有的格式错误契约。

    首尾空白在这里归一。落库前再 `.strip()` 是不够的：流式接口在那之前就已经把
    正文发给用户了，两条接口的可见正文会差一个换行。
    """
    for candidate in (content, strip_think_tags(content)):
        body = _json_answer(candidate)
        if body is not None:
            return body.strip()
    return strip_think_tags(content).strip()


def strip_think_tags(content: str) -> str:
    """不同 Qwen3/Ollama 版本可能返回完整或残缺的 think 标签。"""
    if "</think>" in content:
        return content.rsplit("</think>", 1)[-1]
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
    return re.sub(r"<think>.*", "", content, flags=re.DOTALL)


def _evidence_recheck(question: str, results: list[tuple[Chunk, float]]) -> str | None:
    """既有的那一次证据复查。返回复查正文，解析失败返回 None。

    普通接口与流式接口**共用这一个函数**，两条链路才会做出同一个决定。

    **看全部证据，不只看第一条。** 原来只把 `results[0]` 交给复查，于是首轮答案
    要是靠第二、三条证据（典型情形：政策片段排在前面、System 的库存行排在后面）
    答对的，复查会因为看不到那条证据而"确实没有"，把一个本来正确的答案抹成拒答。
    编号与首轮提示保持一致（`[来源1..n]`），引用才不会错位。

    给全部证据之后，提示词也必须收紧。实测：多条证据加一句复合问题时，小模型会把
    **问句原样抄回来**——`restates_question` 拦得住，但一次有效的复查就浪费了。
    明确要求"直接写结论句、不要复述问题"之后，同一道题的复查能给出带正确 SKU 和
    两个事实的完整答案。这只是换了提示词，调用次数不变。
    """
    context = "\n\n".join(
        f"[来源{index}]\n{chunk.text}" for index, (chunk, _) in enumerate(results, start=1)
    )
    retry_messages = [
        {
            "role": "system",
            "content": (
                "你是证据提取器。从片段中提取问题的答案。"
                "**直接写出结论句，不要复述问题、不要说明步骤。**"
                "每个结论后标注它依据的[来源编号]。"
                "片段确实没有相关信息时，只回答：根据现有资料无法确定。"
            ),
        },
        {"role": "user", "content": f"问题：{question}\n\n{context}"},
    ]
    retry = llm_provider.get_provider().chat(retry_messages, response_format=ANSWER_SCHEMA, temperature=0)
    try:
        return extract_answer_text(retry.raw_content)
    except (KeyError, TypeError):
        return None


def decide_delivery(
    question: str,
    results: list[tuple[Chunk, float]],
    first_answer: str,
    *,
    allow_recheck: bool = True,
) -> str:
    """从首轮答案得出最终交付正文。两条接口共用的唯一决策点。

    共用的不只是 `validate_answer`：**生成后的复查、正文提取和交付判定整条链**
    都在这里。第一轮返修只共用了校验函数，结果普通接口靠复查纠正成了正确答案，
    流式接口没有复查、直接拒答，两边保存的正文不同——共用校验并不等于共用决策。

    `allow_recheck=False` 用于首轮已经用掉第二次生成调用的情形（结构化解析失败
    后的纯文本回退），保证单题生成调用上限仍是 2。

    首尾空白也在这里归一，因为这是**发出任何 delta 之前**两条接口最后一个共同
    入口。普通接口原来在自己那一支 `.strip()`，流式把带空白的 `parsed_answer`
    直接交出来，于是同一个 answer 字段在两边可见正文差一个换行；API 消费层事后
    `.strip()` 只能救落库，救不了已经发出去的正文。
    """
    result = first_answer.strip()
    if allow_recheck and results and needs_evidence_recheck(result, results, question):
        # 首轮不可交付时的回退值。判据是"首轮本身能不能交付"，而不是"看起来像不像
        # 拒答"：`根据现有资料无法确定奖金金额。……每年享有99天……` 含拒答短语，却
        # 夹带着编造的数字，不能当作回退值留下来。
        fallback = (
            result
            if validate_answer(result, results, question)[0]
            else UNGROUNDED_ANSWER_MESSAGE
        )
        retry_result = _evidence_recheck(question, results)
        # 小模型偶发只复述问题。复查结果必须仍是明确拒答，或给出带来源标记的答案；
        # 否则保留首轮的回退值，避免证据复查反而制造无依据回答。
        result = (
            retry_result
            if (retry_result is not None
                and is_valid_evidence_retry(retry_result, question)
                and validate_answer(retry_result, results, question)[0])
            else fallback
        )
    ok, _reason = validate_answer(result, results, question)
    return result if ok else UNGROUNDED_ANSWER_MESSAGE


def answer_structured(question: str, results: list[tuple[Chunk, float]], history: list[dict]) -> str:
    """使用结构化输出减少思考文本和生成长度。"""
    messages = build_answer_messages(question, results, history)
    content = llm_provider.get_provider().chat(messages, response_format=ANSWER_SCHEMA, temperature=0).raw_content
    try:
        first = json.loads(content)["answer"].strip()
        structured = True
    except (json.JSONDecodeError, KeyError, TypeError):
        # 结构化输出解析失败，退回纯文本生成。这一支**不能**提前 return：
        # 提前返回就等于给交付校验开了一个后门。它与结构化分支汇合到
        # decide_delivery 这一个出口。
        first = answer(question, results, history)
        structured = False

    # 回退分支自己已经用掉第二次生成调用，不再叠加复查。
    return decide_delivery(question, results, first, allow_recheck=structured)


def is_refusal(text: str) -> bool:
    return any(marker in text for marker in REFUSAL_MARKERS)


# 拒答主题的名词表。**这是正向识别的全部依据**：一个片段要当主题，必须由这些名词
# （可用「的/和/与/及/或/、/等」连接）拼成，拼不出来就不是主题。
#
# 上一轮这里是 `[^标点]{0,12}`——任意 12 个字，再排除数量和几个连接词。字数少和
# "没命中已知坏词"都不能证明它是名词主题，于是 `年假带薪`、`员工有年假` 这些短事实
# 被当成主题吞掉了。加词到排除表里改不了这一点：依据本身得换成正向的。
#
# 代价是这张表认不出来的主题会失去免引用资格——那是保守方向，写在报告里。
_TOPIC_NOUNS = (
    "退货窗口", "换货窗口", "退货", "换货", "签收", "物流", "售后", "品类", "促销", "人工客服",
    "奖金金额", "奖金", "年假天数", "带薪年假", "年假", "补贴标准", "补贴",
    "加班费", "加班时长", "调休", "报销上限", "报销标准", "报销", "差旅",
    "津贴", "薪资", "工资", "考勤", "审批", "库存", "订单", "工单", "发票",
    "金额", "天数", "时长", "期限", "时限", "上限", "下限", "标准", "比例",
    "条件", "流程", "制度", "规定", "条款", "细则", "范围", "对象", "口径",
    "正式员工", "员工", "公司", "部门", "岗位",
    "这个问题", "该问题", "此问题", "问题", "情况", "内容", "细节", "记录",
    "信息", "数据", "材料", "资料", "原文", "文档", "答案", "结论",
)
_TOPIC_GLUE = ("的", "和", "与", "及", "或", "、", "等")

# 名词短语：零个或多个「名词＋可选连接词」。空也算（主题可以没有）。
_TOPIC_PHRASE = (
    r"(?:(?:" + "|".join(sorted(_TOPIC_NOUNS, key=len, reverse=True)) + r")"
    r"(?:" + "|".join(_TOPIC_GLUE) + r")?)*"
)

# 一句拒答长什么样，写全在这里。免引用的资格由**整段被这个结构解释干净**给出，
# 而不是"段里出现过拒答词"。四个部分，只有拒答谓语是必需的：
#
#     [根据现有资料][也/暂时][奖金金额] 无法确定 [奖金金额] [，。；]
#      └ 证据范围 ┘ └ 副词 ┘ └ topic ┘  └ 谓语 ┘ └ about ┘
#
# `topic`/`about` 现在都必须是 `_TOPIC_PHRASE` —— 名词拼出来的，不是"任意短文本"。
# 任何拼不出来的文字都会让这一段匹配失败，整段随之失去资格，所以事实排在拒答前面
# 还是后面、带不带数字、有没有连接词，都拦得住。
#
# `(?<![不未非无])` 检查谓语极性：`不是无法确定……` 是在否定这个拒答谓语，那不是拒答。
_REFUSAL_CLAUSE = re.compile(
    r"[\s，,、；;]*"
    r"(?:根据|依据|按照|按)?(?:现有|目前|当前)?(?:资料|原文|文档|记录|材料|信息)?[中里内]?[，,]?"
    r"(?:也|又|同样|均|都|暂时|目前|当前|这边|同时)?"
    r"(?P<topic>" + _TOPIC_PHRASE + r")"
    r"(?<![不未非无是])"
    r"(?:" + "|".join(REFUSAL_MARKERS) + r")"
    r"(?P<about>" + _TOPIC_PHRASE + r")"
    r"[\s，,、；;。！？!?]*"
)


def is_pure_refusal(text: str) -> bool:
    """整段都在拒答吗？这是"可以不给引用"的资格。

    前两轮都是**反向**判的：先假定整段在拒答，再找理由推翻它——上上轮找的是
    "有没有跨句号的事实"，上一轮加了"拒答词之前有没有数量"。反向判定永远漏一种写法：

        奖金金额无法确定而正式员工入职满一年后每年享有5天带薪年假。   （数量在拒答之后）
        正式员工享有带薪年假这点已确定只是奖金金额无法确定。         （事实压根没有数字）

    所以改成**正向**：整段必须能被 `_REFUSAL_CLAUSE` 一段一段地解释干净，
    有一个字没被解释就没有资格。事实在拒答前面、后面，带不带数字，都一样拦得住——
    它们都不属于任何一条拒答子句。

    `根据现有资料无法确定奖金金额，也无法确定补贴标准。` 两条子句都能解释干净，
    仍然是纯拒答，逗号不影响。

    识别不了的拒答写法会落到"需要引用"那一边——保守，但方向是安全的。
    """
    remaining = text.strip()
    if not remaining:
        return False
    position = 0
    while position < len(remaining):
        match = _REFUSAL_CLAUSE.match(remaining, position)
        if not match or match.end() == position:
            return False
        position = match.end()
    return True


def is_valid_evidence_retry(answer_text: str, question: str) -> bool:
    if is_refusal(answer_text):
        return True
    normalize = lambda value: re.sub(r"[\s，。！？；：,.!?;:]", "", value).lower()
    if not answer_text.strip() or normalize(answer_text) == normalize(question):
        return False
    # 复查现在能看到全部证据，引用编号自然不止 1 号。
    return bool(re.search(r"\[来源\s*\d+", answer_text))


# ---------------------------------------------------------------------------
# 答案校验：普通回答与流式回答共用同一套判定，两条接口才可能给出一致结果。
# 全部为纯函数，不调用模型、不访问网络。
# ---------------------------------------------------------------------------

# 与 evaluate_answerability 的取法一致：先取出方括号内的全部内容，再抽数字，
# 这样 "[来源1]"、"[来源 1]" 和 "[来源 1、2]" 都能解析。
CITATION_PATTERN = re.compile(r"\[\s*来源\s*([^\]]*)\]")

# 比较答案与问题时忽略的标点与空白。
_ANSWER_NOISE = re.compile(r"[\s，。！？；：、,.!?;:]")

# 无法交付时给出的拒答文案。必须与 chat_orchestration.MESSAGE_NO_ANSWER 一致，
# 否则同一种"没有依据"会因为来自生成层还是策略层而出现两种措辞。
# rag.py 不能反向依赖 chat_orchestration，所以这里是副本，由测试守住一致性。
UNGROUNDED_ANSWER_MESSAGE = "根据现有资料无法确定。"

_CN_DIGITS = {
    "零": "0", "〇": "0", "一": "1", "两": "2", "二": "2", "三": "3", "四": "4",
    "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
}

# 只统计带量词的数字。裸数字没有语义：来源里出现过 "1"，不代表 "1 倍" 有依据。
_MEASURE_WORDS = (
    "倍", "个工作日", "工作日", "个小时", "小时", "分钟", "个月", "天", "日",
    "月", "年", "周", "次", "元", "件", "箱", "档", "份", "%",
)
_UNIT_ALIASES = {"个工作日": "工作日", "个小时": "小时"}

# 豁免词只有两类，因为只有这两类能把一个数值移出「事实断言」的位置：
#
# - 否定：`不是 99 天` 推翻这个数；
# - 条件：`如果按你说的 99 天计算……` 把它设为待核实的前提。
#
# **归属不在其列。** `你说的 99 天` 只说明这个数出自提问者，完全不妨碍下一句自己
# 也这么断言——`你说的99天就是公司规定的年假额度`、`公司规定的年假确实是你说的
# 99天` 都在肯定 99 天。上一轮把归属当成豁免依据，于是"没找到背书词"变成了默认
# 放行；验收复现的四个反例里有三个是这么漏掉的。归属现在降级为轻modifier：它不
# 引入新对象，所以 `不是你说的99天`、`如果按你说的99天计算` 这些**真的**由否定或
# 条件撑住的说法照旧成立，但它自己什么也豁免不了。
#
# 条件同样不能单独豁免。`如果入职满一年，则每年有 99 天` 里的「如果」修饰的是资格
# 条件，99 天是条件成立后的**断言**。`则`/`那么` 是断言分隔符（见
# `_ASSERTION_SPLIT`），数值与条件词落在同一段，才说明它在条件的范围内。
_DENIAL_CUES = (
    "不是", "并非", "并不", "而不是", "没有", "未提", "未规定", "并未",
    "不存在", "不符", "无法确定", "查不到", "找不到",
)
_CONDITION_CUES = ("如果", "若", "假设", "假如", "倘若", "要是")

# 归属：谁说的。见上——只是轻modifier，不是豁免依据。
_ATTRIBUTION_MODIFIERS = (
    "按你说的", "按您说的", "按你提供的", "按您提供的", "按你", "按您",
    "你说的", "您说的", "你提到的", "您提到的", "你提的", "您提的",
    "你问的", "您问的", "如你所说", "如您所说", "你猜的", "您猜的",
)

# 允许出现在「豁免词 → 数值」之间的轻modifier。它们限定数值本身，不给豁免词提供
# 新的对象，所以 `不是每年99天`、`如果按你说的99天计算` 里的关系仍然成立。
# **这不是一个可以越加越长的词表**：每加一个词，就是在说"它不引入新对象"。
# 按长度降序匹配，`所说的` 才不会被 `说的` 先切开。
_LIGHT_MODIFIERS = tuple(sorted(_ATTRIBUTION_MODIFIERS + (
    "每年", "每月", "每天", "每周", "每人", "每位", "每个", "全年", "年度",
    "一共", "总共", "共计",
    "所说的", "所提的", "提到的", "说的", "说得", "提的", "讲的",
    # `未提` 已经是否定词，`原文未提及99天`/`没有提到99天` 却因为中间隔着 `及`、
    # `提到` 被判成管不到——那不是保守，是自相矛盾。补上这几个，让既有的否定词
    # 真的能用。它们都以数值本身为宾语，不引入新对象。
    "提到", "提及", "到", "及", "说法",
    # `假设按99天计算` 里的 `按` 以数值本身为宾语，和 `按你说的` 是同一个介词，
    # 只是没带出处。上一轮把它记成已知误拒，这里补上。
    "按照", "按", "照",
    "的", "是", "有", "这", "那", "个", "其",
), key=len, reverse=True))

# 背书：引用了用户的数值，随后又认可它，就不再是引用，而是自己也这么断言。
# `不正确`/`未确认` 里的 `正确`/`确认` 不算背书，所以要排除紧邻的否定字。
_ENDORSEMENT_PATTERN = re.compile(
    r"(?<![不未非])(?:确实|的确|属实|没错|无误|正是|才对|正确|准确|确认)"
)

# 驳斥：**对这个说法本身**的否定，而不是对句子里别的东西的否定。
# 这条线必须画清楚——`99天并不正确` 是在推翻这个数，`99天没有问题` 是在肯定它。
# 两句里 `不`/`没有` 都紧挨着数值，靠相邻分不出来，要看否定的是什么谓语。
#
# **命中一个前缀不等于整个谓语是驳斥。** 上一轮只写了词表，于是
# `不对第三方公开`（不向第三方披露）里的 `不对`、`有误工补贴`（存在补贴）里的
# `有误` 都被当成了驳斥，两句其实都在肯定那个无依据的数——这是上一轮的回归。
# 靠给 `不对` 再排除几个宾语、给 `有误` 排除一个 `工` 字是补不完的：问题不在漏了
# 哪个词，而在**没有检查匹配的尾部**。
#
# 所以驳斥必须以句读或句末收尾（允许中间夹一两个语气词）。这条边界是可说明的：
# 匹配之后要么什么都没有了，要么只剩标点——只要还接着别的字，就说明这几个字是更长
# 的词或另一个谓语的一部分，不能据此豁免。
_REFUTATION_CORE = (
    r"并?不(?:正确|准确|属实|成立|符合事实|是这样|对)"
    r"|并非(?:如此|事实|属实)"
    r"|(?:说法)?有误|是错的|站不住脚|不足为据"
)
_REFUTATION_PATTERN = re.compile(_REFUTATION_CORE)

# 语气词可以跟在驳斥后面（`不对吧`），它们不改变谓语已经说完这件事。
_REFUTATION_PARTICLES = "吧呢啊呀了的"

# 真正的句读。**空白不在其中**：`不对 第三方公开` 里的空格后面还是同一个表达，
# 空白只能出现在真正的句读*之前*（`不对 ，原文写的是5天` 仍然算说完了）。
#
# **开括号也不在其中。** 上一轮把 `[（(【` 一并列成句读，于是
# `不对（外部员工）公开` 在开括号处就算"说完了"——可那对括号装的是「不公开的对象」，
# 后面还有谓语 `公开`，整句根本没否定 99 天。开括号要看括号里装了什么、以及**闭括号
# 之后谓语还继不继续**，见 `_refutation_ends_cleanly`。
_CLAUSE_PUNCTUATION = "，,。；;：:！？!?、\n）)］]】》」』"

# 开括号与它的闭括号。引用括号单独识别：`不对[来源1]。` 里的方括号是引用，不是插入语。
_BRACKET_PAIRS = {
    "（": "）", "(": ")", "［": "］", "[": "]", "【": "】",
    "《": "》", "「": "」", "『": "』",
}
_CITATION_OPEN = re.compile(r"[\[［]\s*来源")


def _refutation_ends_cleanly(text: str, index: int) -> bool:
    """驳斥表达在**原文全文**的 `index` 处，是不是真的说完了？

    这里必须用全文坐标。上一轮把边界判断交给了正则的 `$`，而正则拿到的是
    `text[end:following]`——一个被**下一个数量**截断的窗口。于是
    `公司规定的99天不对5天内入职的员工公开` 传进去的只有 `不对`，`$` 当然成立，
    可原文后面还有 `5天内入职的员工公开`。下一个数量只该限制背书归属的扫描范围，
    不能凭空制造一个句末。

    所以：跳过语气词，再跳过空白，然后要么到了**全文**末尾，要么遇到真正的句读。
    空白自己不算结束——它后面若还是同一个表达的宾语或词尾，就不算说完。
    """
    while index < len(text) and text[index] in _REFUTATION_PARTICLES:
        index += 1
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text):
        return True
    character = text[index]
    if character in _BRACKET_PAIRS:
        # 引用把谓语收住：`你说的99天不对[来源1]。`
        if _CITATION_OPEN.match(text, index):
            return True
        closing = text.find(_BRACKET_PAIRS[character], index + 1)
        if closing < 0:
            return False  # 括号没闭合，判断不了，就不豁免
        # 括号本身说明不了什么，**闭括号之后**才说明得了：还接着谓语
        # （`（外部员工）公开`）就是插进来的宾语，谓语没说完；接着句读或到头
        # （`（原文写的是5天）。`）才是说完之后补的解释。
        return _refutation_ends_cleanly(text, closing + 1)
    return character in _CLAUSE_PUNCTUATION

# 保留原名：模块内其它注释与外部复现脚本按这个名字理解「豁免词」的全集。
# 归属不再是豁免依据，因此不在其中。
HYPOTHESIS_CUES = _DENIAL_CUES + _CONDITION_CUES

# 数词体：**包含本模块并不转换的写法**（`万`、`亿`、`点`）。它们列在这里正是为了把
# 数词整个捕获下来——字符类停在 `千`，`一万两千件` 就只匹配到 `两千件`，于是一个
# 模型编造的数字变成了「证据里有 2000 件」的样子。截断出来的数字比识别不出来危险
# 得多：它看起来完全合法。数字、小数点和中文数词共用一个字符类，`1万2千件` 这种
# 混写同样只会被当成一个完整数词。
_NUMERAL_BODY = r"[\d.零〇一二两三四五六七八九十百千万亿兆点]+"

_NUMBER_WITH_UNIT = re.compile(
    r"(" + _NUMERAL_BODY + r")\s*(" + "|".join(_MEASURE_WORDS) + r")"
)


def citation_indices(text: str) -> list[int]:
    """答案里出现的全部来源编号，按出现顺序。"""
    found: list[int] = []
    for raw in CITATION_PATTERN.findall(text):
        found.extend(int(digits) for digits in re.findall(r"\d+", raw))
    return found


def invalid_citation_indices(text: str, source_count: int) -> list[int]:
    """越界的来源编号。编号指向不存在的证据，就是编造出来的引用。"""
    return sorted({n for n in citation_indices(text) if n < 1 or n > source_count})


# 「我打算怎么找答案」的说法。注意这里**不是**把 `首先` 之类的词列成禁词——
# `申请流程是：首先提交表单，然后主管审批。` 是一段正确的步骤说明，必须照常交付。
# 判据是第一人称/情态的**调查动作**：说话人在描述自己要去读、去找、去核对，
# 而不是在陈述读到了什么。
_PROCESS_INTENT_PATTERN = re.compile(
    r"(?:我|需要|应该|接下来|下一步|首先)[^。！？!?\n]{0,8}"
    r"(?:阅读|查阅|查找|查看|检索|核对|确认|检查|梳理|分析)"
    r"|让我[^。！？!?\n]{0,6}(?:看|读|查)"
    r"|(?:看|检查|确认)(?:一下)?是否(?:明确)?(?:提到|包含|写|规定)"
    r"|需要从\[?来源"
)


# 关于「怎么写这个回答」的自述。这些说法谈的是应答任务本身，不是资料里的事实，
# 一段交给用户看的答案里不该出现。**和上面那条不是一回事**：
# `describes_only_process` 抓的是"只说要去查、没给结果"；这条抓的是"结果也给了，
# 但把整段思考过程一起发出来了"——真实模型在三通道复合问题上复现过，正确答案埋在
# 一大段 `我将逐一检查…我需要确保回答符合要求…这样正好3句话` 里发出并落库。
#
# 词表刻意选得窄：它们必须是**谈论作答任务**的说法。业务步骤说明（`首先提交表单，
# 然后主管审批`）一个都不命中，制度原文也不会这么说话。
_DELIBERATION_MARKERS = (
    "最终答案", "我将逐一", "我需要确保", "根据用户问题", "用户问的是",
    "用户要求", "这样正好", "句话以内", "需要精简", "符合要求：",
    "我可以这样回答", "让我重新", "重新组织一下",
)


def delivers_deliberation(answer_text: str) -> bool:
    """正文里夹着"我要怎么写这个回答"的自述。

    有结果不等于可交付：读者要的是答案，不是把模型的草稿纸一起收下。
    """
    return any(marker in answer_text for marker in _DELIBERATION_MARKERS)


def describes_only_process(answer_text: str, results: list[tuple[Chunk, float]]) -> bool:
    """这段话只说了「我要去查」，没有说查到了什么。

    真实模型复现出来的形态：`根据问题，需要从[来源1]中查找陪护假的期限。首先，我
    需要仔细阅读资料，看是否明确提到了该假期及天数。` 它带着合法引用，不复述问题、
    没有越界编号、也没有无依据数量，前面每一项都拦不住它——可它既没有回答，也没有
    明确拒答，用户拿到的是一段自述。

    只有在**没有给出任何结果**时才算：带拒答短语的是拒答（可交付），给出了数量的是
    答案（哪怕前面有步骤铺垫），都不在此列。
    """
    if not results or not answer_text.strip():
        return False
    if is_refusal(answer_text):
        return False
    if quantity_mentions(answer_text):
        return False
    return bool(_PROCESS_INTENT_PATTERN.search(answer_text))


def restates_question(answer_text: str, question: str) -> bool:
    """答案是否只是把问题重说了一遍。

    这是小模型在证据不足时的一种失败方式：输出里不含任何拒答词，所以原来的
    拒答复查不会触发，问题原样返回给用户，看起来却像是一次正常回答。
    """
    answer = _ANSWER_NOISE.sub("", answer_text).lower()
    asked = _ANSWER_NOISE.sub("", question).lower()
    if not answer:
        return True
    if answer == asked:
        return True
    # 整句被问题包含，说明它没有添加任何问题里没有的内容。长度下限避免把
    # "5天" 这种正好出现在问句里的短答案误判成复述。
    return len(answer) >= 8 and answer in asked


_CN_PLACES = {"十": 10, "百": 100, "千": 1000}


def parse_numeral(token: str) -> str | None:
    """数词的数值（十进制字符串）；**不支持的写法返回 None**。

    返回 None 不是失败，是本模块能给出的唯一诚实答案：`一万两千`、`二点五`、
    `二〇二五` 都是完整数词，只是这里不转换它们。此时调用方必须把整个数词当作
    不可判定处理，走既有的不可交付流程——绝不能退而求其次，从里面截一段能解析的
    后缀（`一万两千` → `两千` → 2000）冒充一个有依据的数值。

    支持范围：阿拉伯整数与小数；中文 0-9999 的位值写法（`四十二`、`二百三十四`、
    `一百零五`、`两千`）。逐字替换是错的——`四十二` 会变成 `4十2`，于是「库存为
    四十二件」这种**正确**答案因为对不上来源里的 `42 件` 被判成无依据数量。

    明确不支持并因此返回 None 的写法：
    - `万`/`亿`/`兆` 量级与 `点` 小数；
    - `二〇二五`、`五六` 这类没有位值字的连写数字串（位置记数或"五六天"的约数，
      两种读法都不能当成一个确定值）；
    - 位值不递减的乱序串（`十百`）。
    """
    if not token:
        return None
    if token[0].isdigit():
        return token if re.fullmatch(r"\d+(?:\.\d+)?", token) else None

    total, section = 0, 0
    seen_value = False
    previous_digit: str | None = None
    last_scale = float("inf")
    for char in token:
        if char in _CN_DIGITS:
            # `一百零五` 里 `零` 是占位符，后面再跟一个数字字是合法的；
            # `二〇二五` 里 `二` 后面直接跟 `〇`，那是位置记数，不是位值写法。
            if previous_digit is not None and previous_digit not in "零〇":
                return None
            section = int(_CN_DIGITS[char])
            seen_value, previous_digit = True, char
        elif char in _CN_PLACES:
            scale = _CN_PLACES[char]
            if scale >= last_scale:
                return None  # `十百`：解析不出来就不解析。
            # `十五` 的十前面没有数字，值为 1；`四十二` 的十前面有 4。
            total += (section or 1) * scale
            section, seen_value, previous_digit = 0, True, None
            last_scale = scale
        else:
            return None  # `万`、`点`、以及任何其它字符：不猜。
    if not seen_value:
        return None
    return str(total + section)


# 标识的尾部：可选一个字母、至少两位数字，**再把后面粘着的字母数字一并吃掉**。
# 最后那段是关键——`SKU-C300A` 若只取到 `SKU-C300`，多出来的 `A` 就被悄悄丢掉，
# 一个不同的编号被当成了已知实体。要求两位以上数字，`sku 7箱` 这种就不会被当成标识。
_ID_TAIL = r"[A-Za-z]?\d{2,6}[A-Za-z0-9]*"

# **显式**业务标识：字母前缀 + 硬分隔符 + 尾部。家族未知也算数——`SPU-C300` 写得
# 明明白白是个编号，证据里没有它就必须拦下。这一式不认空格，所以 `GB18030`、
# `ISO 8601` 这类普通文字不会被当成标识。
_EXPLICIT_IDENTIFIER = re.compile(rf"[A-Za-z]{{2,6}}[-_]{_ID_TAIL}")

# 前缀（家族）只从显式标识里学，用来放宽**已知家族**的书写形式。
_IDENTIFIER_FAMILY = re.compile(rf"([A-Za-z]{{2,6}})[-_]{_ID_TAIL}")

# 归一化时删掉的分隔符。空格也在内——`SKU C300` 与 `SKU-C300` 是同一个东西。
_IDENTIFIER_SEPARATORS = re.compile(r"[-_\s]")


def _normalize_identifier(token: str) -> str:
    """大小写与分隔符归一。**位数与尾巴不归一**——那是另一个实体。"""
    return _IDENTIFIER_SEPARATORS.sub("", token).lower()


def _family_pattern(families: set[str]) -> re.Pattern[str] | None:
    """已知家族的宽松写法：分隔符可以是空格（含多个）、`-`、`_`，也可以没有。

    这**只是允许合法书写变体**，不是"整段答案只扫这些家族"。上一版把它当成了
    唯一的扫描式，于是 `SPU-C300` 这种未知家族的显式编号整个被忽略——放宽写法
    反而放过了另一个实体。显式标识另有 `_EXPLICIT_IDENTIFIER` 兜底。
    """
    if not families:
        return None
    alternatives = "|".join(re.escape(name) for name in sorted(families, key=len, reverse=True))
    return re.compile(rf"(?:{alternatives})[-_ ]{{0,3}}{_ID_TAIL}", re.IGNORECASE)


def _identifiers_in(text: str, family: re.Pattern[str] | None) -> set[str]:
    """文本里的业务标识：显式写法一律算，已知家族再加宽松写法。"""
    found = set(_EXPLICIT_IDENTIFIER.findall(text))
    if family is not None:
        found |= set(family.findall(text))
    return found


def unsupported_identifiers(
    answer_text: str, results: list[tuple[Chunk, float]], question: str
) -> list[str]:
    """答案里出现、而证据与问句都没有的业务标识。

    真实模型复现出来的形态：System 给的是 `sku-c300`，答案写成 `sku-c30`。数量对、
    引用编号也对，冻结评测器两项都会通过——可它说的已经是另一个商品了。数量校验
    看不见这种错误：`7 箱` 本身是有依据的。

    比对的是**完整标识**：大小写、空白与分隔符可以归一（`SKU-A100`/`sku a100`/
    `SKU  C300`/`skuc300` 都算同一个），但少一位、多一位、改一位、多个尾字母，
    以及换一个家族（`SPU-C300`），都是另一个实体。
    """
    if not results:
        return []
    sources = [chunk.text for chunk, _ in results] + [question]
    families = {
        name.lower()
        for source in sources
        for name in _IDENTIFIER_FAMILY.findall(source)
    }
    family = _family_pattern(families)
    grounded = {
        _normalize_identifier(token)
        for source in sources
        for token in _identifiers_in(source, family)
    }
    return sorted(
        {
            token
            for token in _identifiers_in(answer_text, family)
            if _normalize_identifier(token) not in grounded
        }
    )


def _cn_number_to_arabic(token: str) -> str:
    """`parse_numeral` 的旧接口：不支持的写法原样返回。

    保留是为了第一轮的复现脚本仍然可运行。**新代码不要用它**：把不支持的数词
    原样返回，调用方就分不清「这是数字 42」和「这串我没看懂」，而这两者正是
    第三轮返修要区分开的东西。判定一律走 `QuantityMention.key`。
    """
    return parse_numeral(token) or token


@dataclass(frozen=True)
class QuantityMention:
    """答案或证据里的一次「数字＋量词」出现，连同它的位置。

    位置是关键：修饰关系要落到**这一次出现**上，先做成集合就再也问不出
    「这个『不是』修饰的是哪个数」了。
    """

    unit: str
    raw: str
    start: int
    end: int
    value: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        """用于和证据比对的身份。

        不支持的数词以 `?原文` 作为身份，只可能和证据里**一模一样**的数词相等，
        绝不会等于任何被本模块算出来的数字。
        """
        return (self.value if self.value is not None else f"?{self.raw}", self.unit)


def quantity_mentions(text: str) -> list[QuantityMention]:
    """文本中每一次「数字＋量词」的出现，按位置顺序，不去重。"""
    return [
        QuantityMention(
            unit=_UNIT_ALIASES.get(match.group(2), match.group(2)),
            raw=match.group(1),
            start=match.start(),
            end=match.end(),
            value=parse_numeral(match.group(1)),
        )
        for match in _NUMBER_WITH_UNIT.finditer(text)
    ]


def quantities(text: str) -> set[tuple[str, str]]:
    """文本中"数字＋量词"的集合。证据侧按集合比对没有问题——需要逐次判定的是
    答案侧，那里用 `quantity_mentions()`。"""
    return {mention.key for mention in quantity_mentions(text)}


_ANSWER_CLAUSE_SPLIT = re.compile("[，。；！？,;!?、\n]")

# 断言分隔符：标点**加上**能独立开启一个新断言的连接词。有没有逗号不能决定一段话
# 里有几个断言——`每年99天且没有特殊限制` 一个标点都没有，却是两个断言，前一个
# 无依据、后一个与数值无关。`而是`/`但是`/`并且` 必须排在 `而`/`但`/`且` 之前，
# 否则会被更短的分支先切开。`则`/`那么` 结束条件句的作用范围。
_ASSERTION_SPLIT = re.compile(
    "[，。；！？,;!?、:：\n]"
    "|而是|但是|不过|然而|可是|并且|同时|另外|此外|那么|但|却|且|则"
)


def ungrounded_quantities(
    answer_text: str, results: list[tuple[Chunk, float]], question: str
) -> list[tuple[str, str]]:
    """答案断言了、而证据不支持的数量。

    量词是关键。资料里写 "按 1:1 转为调休" 时，字符 "1" 是有的，"1 倍" 却没有；
    把它当成加班费倍数就是拿一个数字去支撑另一个结论。

    **问句里的数字不是依据。** 用户问「每年有 99 天年假吗？」，模型答「每年享有
    99 天带薪年假」，如果把问句数值并入支持集合，用户的猜测就成了确认自己的依据。

    **判定落在每一次出现上，而不是把范围缩小一点再照旧看整段。** 上一轮按标点切
    分之后仍然放行了六种错误答案，因为段内还是「有任意一个有依据的同量词数字，或
    任意一个否定/假设词，就替段里所有数字免责」。缩小字符串范围代替不了「这个修饰
    到底在修饰哪个数」。现在的规则只有两条：

    1. 断言边界由标点**和连接词**共同决定（`_ASSERTION_SPLIT`）。有没有逗号不是
       安全与否的依据——`每年99天且没有特殊限制` 没有标点，仍是两个断言。
    2. 豁免必须由**这一次出现之前、同一断言之内**的否定词或归属词给出，且被豁免的
       数值必须是提问者自己说出来的。「段里别处有个对的数字」不再是豁免理由。

    因此：

    - `不是99天而是5天` —— 99 在否定里，5 有依据，可交付；
    - `不是5天而是99天` —— 用词相同、位置不同，99 是被肯定的断言，拦截；
    - `每年99天且可以先休5天` —— 另一个断言里有依据的 `5 天` 不替 99 免责；
    - `并不是所有人都有99天但正式员工每年确实有99天` —— 同一个数字前否后肯，
      两次分别判定，后一次拦截。

    同一个数字先被否定、后又被肯定时，**后一次肯定仍会被判出**：逐次判定，
    绝不先对一段文字做 `set` 再问修饰关系——去重之后就再也分不清是哪一次出现了。
    """
    if not results:
        return []
    grounded = quantities(" ".join(chunk.text for chunk, _ in results))
    from_question = quantities(question)

    # 提问者给出的数字有两种身份，必须分开：
    #
    #   `单笔 2500 元的报销，除了直属主管还需要谁审批？` —— 2500 是**他自己的情景**，
    #       答案复述它（`单笔2500元的报销需要部门负责人审批`）不是在说资料写了 2500；
    #   `每年有 99 天年假吗？请核对原文` —— 99 是**要他核对的说法**，答案把它当事实
    #       断言，正是要拦的。
    #
    # 判据**绑定到这一次出现挂在谁身上**，不是"整个问句有没有核对词"。上一版按整句
    # 切换，于是 `我在公司工作了99天` 的 99 可以被答成 `每年享有99天年假`，
    # `我的报销单金额是2500元` 的 2500 可以被答成 `门槛是2500元`——同一个数字换了对象，
    # 就是在替资料发言。见 `_is_premise_restatement`。
    question_mentions = quantity_mentions(question)
    mentions = quantity_mentions(answer_text)
    starts = _assertion_starts(answer_text)

    flagged: list[tuple[str, str]] = []
    for position, mention in enumerate(mentions):
        if mention.key in grounded:
            continue
        segment_start = _assertion_start_before(starts, mention.start)
        # 复述提问者本次的前提：问句那一次是给定的、答案这一次说的是同一个属性、
        # 而且不是在替资料发言，三条都成立才算。
        if mention.key in from_question and _is_premise_restatement(
            answer_text, question, mention, segment_start, question_mentions
        ):
            continue
        # 否则只有提问者自己给出的数字才可能被豁免，而且必须由**这一次出现**周围
        # 可识别的结构把它放进引用或否定的位置。背书的作用范围到下一个数值为止：
        # `不是99天。5天才对。` 里的 `才对` 认可的是 5，不是 99。
        following = mentions[position + 1].start if position + 1 < len(mentions) else len(answer_text)
        if mention.key in from_question and _is_quoted_or_denied(
            answer_text,
            segment_start,
            mention.start,
            mention.end,
            following,
        ):
            continue
        flagged.append(mention.key)
    return sorted(set(flagged))


# 「这个数是资料规定的值」的说法。数值落在这种位置上，就是在替资料发言，必须有依据
# ——哪怕提问者自己也说过这个数。
_POLICY_VALUE_MARKERS = (
    "门槛", "上限", "下限", "限额", "额度", "标准是", "标准为",
    "享有", "可享", "可休", "不超过", "最多", "至少", "规定为", "规定是",
)

# 问句在要求核对**这一处**说法。作用范围是该数值所在的分句，不是整个问句：
# `这笔报销费用为2500元，是否需要部门负责人审批？` 里的 `是否` 问的是审批，
# 不是要核对 2500。
_VERIFICATION_CUES = (
    "吗", "么", "是否", "是不是", "对不对", "对吗", "有没有", "准不准",
    "核对", "确认", "查证", "属实", "真的", "是这样",
)

# 上下文窗口：只看数值紧邻的一小段，够认出它挂在谁身上，不至于把整句话都算进来。
_ROLE_WINDOW_BEFORE = 10
_ROLE_WINDOW_AFTER = 8


def _role_context(text: str, start: int, end: int, floor: int) -> str:
    """数值紧邻的一小段文字，用来看它挂在什么东西上。"""
    left = max(floor, start - _ROLE_WINDOW_BEFORE)
    return text[left:start] + text[end : end + _ROLE_WINDOW_AFTER]


# 系动词。数值紧挨着它，就不再是"参与了一件事"，而是被**等同于**某个东西：
# `公司年假为99天` 把 99 当成 `年假` 的取值；`这笔2500元是报销审批分界线` 把 2500
# 定义成一条制度界线。两者都是在替资料发言，与谁拥有这个数无关。
_COPULAS = ("等于", "即为", "为", "是")

# 指示词与人称词。它们说明"讲的是谁的"，**不是**数值挂靠的中心词，所以找中心词时跳过。
# 前几轮的教训正在于此：`这笔` 证明不了两边在说同一件事。
_ROLE_DETERMINERS = frozenset({
    "这笔", "这次", "这个", "这项", "该笔", "该次", "本次", "本笔",
    "我的", "你的", "您的", "他的", "她的", "我们", "单笔", "每笔",
})

# 一次扫完，而不是逐个 replace：`frozenset` 的迭代顺序随 PYTHONHASHSEED 变，
# 逐个删除时"先删哪一个"可能改变结果，判据就不可复现了。
_ROLE_DETERMINER_RE = re.compile("|".join(sorted(_ROLE_DETERMINERS)))


def _copular_role(text: str, start: int, end: int, floor: int) -> tuple[str | None, str]:
    """这一次出现是不是站在系动词旁边？

    返回 `("value", 左边被赋值的那部分)`——`X为99天`，99 是 X 的取值；
    或 `("defined", 右边它被等同于的那部分)`——`2500元是分界线`，2500 被定义成什么；
    都不是就返回 `(None, 左边那段)`。
    """
    before = text[floor:start].rstrip()
    for copula in _COPULAS:
        if before.endswith(copula):
            return "value", before[: -len(copula)]
    after = text[end:].lstrip()
    for copula in _COPULAS:
        if after.startswith(copula):
            return "defined", after[len(copula):]
    return None, before


def _content_words(span: str) -> list[str]:
    """按出现顺序的实词，去掉指示词与人称词。"""
    return [
        token
        for token in lexical_tokens(span)
        if len(token) >= 2 and token not in _ROLE_DETERMINERS
    ]


def _governing_head(text: str, start: int, end: int, floor: int) -> str | None:
    """这一次出现挂靠的**中心词**——只取一个，不取一片。

    取一个而不是一片，是这一轮的关键。取一片时
    `我在公司工作了99天` 的属性集是 `{公司, 工作}`、`公司年假为99天` 的是
    `{公司, 年假}`，它们靠 `公司` 相交——可 `公司` 在两句里都只是个状语/领属，
    **一个与属性无关的共有词就让豁免生效了**。中心词是紧挨着数值的那一个：
    前者是 `工作`，后者是 `年假`，对不上。
    """
    role, span = _copular_role(text, start, end, floor)
    if role == "defined":
        following = _content_words(span)
        return following[0] if following else None
    preceding = _content_words(span)
    if preceding:
        return preceding[-1]
    # 左边只有指示词（`这笔2500元报销…`），中心词在右边。
    following = _content_words(text[end : end + _ROLE_WINDOW_AFTER])
    return following[0] if following else None


def _attribute_core(span: str) -> str:
    """属性名本身——去掉说明"这是谁的"的指示词与人称词。

    `我的报销金额` 与 `你的报销金额` 讲的是同一个属性，`我的`/`你的` 只说明讲的是谁的；
    去掉它们之后剩下的才是可以两边对齐的东西。
    """
    return _ROLE_DETERMINER_RE.sub("", span).replace("的", "").strip()


def _attribute_words(context: str) -> set[str]:
    """上下文里的实词，用来看数值挂在什么属性上。

    这一侧仍用集合：问句那一边允许"中心词落在它相邻的任意一个实词上"，
    因为 `我的报销金额是2500元` 与 `这笔2500元报销需要…审批` 说的确实是同一笔，
    只是一个以 `金额` 为中心、一个以 `报销` 为中心。收紧的是**答案**那一侧。
    """
    return {token for token in _content_words(context)}


def _is_bare_verification(clause: str) -> bool:
    """这个分句除了"这话对不对"以外，还问了别的东西吗？

    `是真的吗` / `对不对` / `是这样吗` —— 把核对词拿掉就什么都不剩，它问的只能是
    前面那句话本身，所以前面那个数**是待核实的说法**；
    `是否需要部门负责人审批` —— 把 `是否` 拿掉还剩 `需要部门负责人审批`，
    它问的是审批，前面那个数仍是他给定的前提。
    """
    rest = clause
    for cue in _VERIFICATION_CUES:
        rest = rest.replace(cue, "")
    return not _content_words(rest)


def _question_occurrence_is_given(question: str, mention: QuantityMention) -> bool:
    """问句里这一次出现，是已给定的实例前提，还是待核实的数量主张？

    按**该数值所在的分句**判断，不按整个问句：
    `我的年假是99天吗？` —— `吗` 就贴在这个数上，它是待核实的说法；
    `这笔报销费用为2500元，是否需要部门负责人审批？` —— `是否` 在下一个分句，
    问的是审批要不要，2500 仍是他给定的前提。

    但核对词在下一个分句**不一定**就是在问别的事：`我的年假是99天，是真的吗？`
    的 `是真的吗` 自己没有内容，它指回的正是前一句。所以本句之后要一直看到
    第一个有内容的分句为止——在那之前出现的空核对句，问的就是这个数。
    """
    clauses: list[tuple[int, str]] = []
    position = 0
    for clause in _ASSERTION_SPLIT.split(question):
        start = question.find(clause, position)
        if start < 0:
            continue
        position = start + len(clause)
        clauses.append((start, clause))

    for index, (start, clause) in enumerate(clauses):
        if not start <= mention.start < start + len(clause):
            continue
        if any(cue in clause for cue in _VERIFICATION_CUES):
            return False
        for _later_start, later in clauses[index + 1 :]:
            if not later.strip():
                continue
            if any(cue in later for cue in _VERIFICATION_CUES) and _is_bare_verification(
                later
            ):
                return False
            break
        return True
    return False


def _is_premise_restatement(
    answer_text: str,
    question: str,
    mention: QuantityMention,
    segment_start: int,
    question_mentions: list[QuantityMention],
) -> bool:
    """这一次出现，是在复述提问者的前提，还是在替资料报一个值？

    提问者说的数字有两种去处，**必须按它挂在谁身上分开**，不能按整个问句一刀切：

        我在公司工作了99天，请介绍正式员工年假政策。
            → `正式员工每年享有99天带薪年假`  99 从"我干了多久"变成了"每年多少假"，
              是在替资料发言，必须有依据；
        我的报销单金额是2500元，请说明审批规则。
            → `公司规定报销审批门槛是2500元`  2500 从"我这单多少钱"变成了"政策门槛"；
        单笔2500元的报销，除了直属主管还需要谁审批？
            → `单笔2500元的报销需要部门负责人审批`  还是同一笔报销，是复述前提。

    这需要**三件信息**，缺一条都证明不了绑定，前几版各缺了一部分：

    1. **问句里这一次出现是不是已给定的前提**（`_question_occurrence_is_given`）。
       第一版按整个问句有没有核对词切换，于是 `我在公司工作了99天` 的 99 可以被
       随便复用；
    2. **答案里这一次出现是不是在替资料发言**（`_POLICY_VALUE_MARKERS`）。
       `我的`/`这笔` 只说明在讲谁，不能让一句规则断言免责；
    3. **两边说的是不是同一件事**。第二版根本没拿到问句；第三版拿到了，却是拿
       两段文字的实词求交集——`我在公司工作了99天` 与 `公司年假为99天` 共有一个
       `公司`，交集非空就豁免了，可 `公司` 在两句里都只是状语，它证明不了任何事。

    第 3 条现在按**这个数挂在谁身上**判，分三种位置：

    - `N是X`（`这笔2500元是报销审批分界线`）—— 数值被**定义成** X。那是在陈述
      制度，一律不豁免；`这笔` 只说明讲的是谁的，改变不了这一点。
    - `X为N`（`公司年假为99天`）—— 数值是属性 X 的**取值**。要算复述，问句必须
      也把同一个数当成同一个属性的取值（`我的报销金额是2500元` ->
      `你的报销金额是2500元`，去掉人称后都是 `报销金额`）。问句只是把它当成某个
      动作的量（`工作了99天`），答案却说成 `年假` 的取值，就是换了对象。
    - 其余位置 —— 紧挨数值的那**一个**中心词（`_governing_head`）必须出现在问句
      那一次的相邻实词里。取一个而不是一片，正是为了不让 `公司` 这种状语顶上。

    三条都成立才豁免。证明不了就不豁免，落回既有的复查／拒答——代价是换了说法复述
    前提可能被保守拦下，再由复查挽回；这一条记在报告里。
    """
    context = _role_context(answer_text, mention.start, mention.end, segment_start)
    if any(marker in context for marker in _POLICY_VALUE_MARKERS):
        return False

    role, span = _copular_role(answer_text, mention.start, mention.end, segment_start)

    # 数值被**定义成**某个东西（`这笔2500元是报销审批分界线`）——那是在陈述制度，
    # 不是在复述"我这笔多少钱"。谁拥有这个数不影响这个判断：`这笔` 只说明讲的是谁的。
    if role == "defined":
        return False

    answer_head: str | None = None
    if role is None:
        answer_head = _governing_head(
            answer_text, mention.start, mention.end, segment_start
        )
        if answer_head is None:
            return False

    for other in question_mentions:
        if other.key != mention.key:
            continue
        if not _question_occurrence_is_given(question, other):
            continue

        if role == "value":
            # 答案把这个数**等同于**左边那个属性（`公司年假为99天`）。要算复述，
            # 问句必须也把同一个数等同于同一个属性。问句只是把它当成某个动作的量
            # （`工作了99天`）时，答案就是把它挪到了别的东西头上——那是在陈述制度。
            question_floor = max(0, other.start - _ROLE_WINDOW_BEFORE)
            question_role, question_span = _copular_role(
                question, other.start, other.end, question_floor
            )
            if question_role != "value":
                continue
            answer_core = _attribute_core(span)
            question_core = _attribute_core(question_span)
            if not answer_core or not question_core:
                continue
            if answer_core in question_core or question_core in answer_core:
                return True
            continue

        asked = _role_context(question, other.start, other.end, 0)
        # 答案的**中心词**必须出现在问句那一次的相邻实词里。共有一个状语或领属词
        # （`公司`）不算——它证明不了两边在说同一件事。
        if answer_head in _attribute_words(asked):
            return True
    return False


def _assertion_starts(text: str) -> list[int]:
    """每个断言在原文中的起始下标。

    用位置而不是切好的字符串，是因为背书的作用范围要跨断言看到下一个数值为止，
    而修饰关系只在本断言内成立——两件事的范围不同，切成字符串就表达不了。
    """
    return [0] + [match.end() for match in _ASSERTION_SPLIT.finditer(text)]


def _assertion_start_before(starts: list[int], index: int) -> int:
    result = 0
    for start in starts:
        if start > index:
            break
        result = start
    return result


def _is_quoted_or_denied(
    text: str, segment_start: int, start: int, end: int, following: int
) -> bool:
    """这一次数值出现，是否**确实**被否定、或被设为待核实的假设？

    默认是"不豁免"。上一轮反过来：找到一个沾边的词、又没在有限范围里找到背书，
    就放行——于是这四句都通过了，而它们全都在肯定 99 天：

    - `公司规定的年假确实是你说的99天` —— 肯定在数值**前面**；
    - `你说的99天就是公司规定的年假额度` —— 等同判断，没命中背书词；
    - `正式员工并不是没有99天带薪年假` —— 否定词本身被否定；
    - `你说的99天和原文的5天相比，前者才对` —— 背书指向前一个数值。

    前两句的共同点是**归属**：`你说的` 只说明这个数出自提问者，一点也不妨碍句子
    自己再肯定它。所以归属被降级成轻modifier（见 `_ATTRIBUTION_MODIFIERS`），不再
    是豁免依据——这一处改动同时解决了 1、2、4，不必去数背书词落在数值的哪一侧。

    留下的豁免只有两种完整结构：

    1. **否定**：一个否定词管得到这个数（`_governs`），且它自己**没有**被另一个
       否定词否定（`_denies`）。`并不是没有99天` 是双重否定，等于肯定。
    2. **假设**：一个条件词管得到这个数。`则`/`那么` 是断言分隔符，所以条件词能与
       数值同段，只可能是数值落在条件本身里。

    否定也允许紧跟在数值**后面**（`你说的99天并不正确`）：中间只隔着轻modifier 才算，
    所以 `99天带薪年假没有特殊限制` 里那个 `没有` 够不着（中间隔着"带薪年假"）。

    嵌套否定、跨数量指代这些拿不准的结构一律不豁免，落到既有的复查/拒答流程。
    """
    # 背书从这个数值之后一直看到**下一个数值**为止，可以跨断言：
    # `按你说的99天，属实。` 的认可在下一个分句里，仍然是认可这个数。到下一个数值
    # 就停，否则 `不是99天。5天才对。` 里认可 5 的 `才对` 会反过来把 99 判成断言。
    if _ENDORSEMENT_PATTERN.search(text[end:following]):
        return False

    prefix = text[segment_start:start]
    for cue in _CONDITION_CUES:
        if _reaches_forward(prefix, cue):
            return True
    for position in _cue_positions(prefix, _DENIAL_CUES):
        if _governs(prefix[position[1] :]) and not _denies(prefix, position[0]):
            return True

    # 否定也可以跟在数值后面，但**紧邻不等于在否定它**。上一轮这里只看数值到否定词
    # 之间是不是空的，于是 `99天没有问题`、`99天没有额外限制` 里那个否定「问题」和
    # 「限制」的 `没有` 被当成了否定 99 天——两句其实都在肯定可以休 99 天，这是上一轮
    # 引入的回归。现在要求整个谓语是**对这个说法本身的驳斥**（`并不正确`、`不对`、
    # `有误`…），不是随便一个否定词；`没有问题` 否定的是别的东西，够不到这个数。
    # 搜索范围仍到下一个数量为止（那是背书归属的范围），但**结束位置在全文坐标上
    # 核对**——窗口末尾不是句末。
    trailing = text[end:following]
    for match in _REFUTATION_PATTERN.finditer(trailing):
        if _governs(trailing[: match.start()]) and _refutation_ends_cleanly(
            text, end + match.end()
        ):
            return True
    return False


def _cue_positions(text: str, cues: tuple[str, ...]) -> list[tuple[int, int]]:
    """`cues` 在 `text` 里每一次出现的 (起点, 终点)，按起点排序。"""
    found: list[tuple[int, int]] = []
    for cue in cues:
        position = text.find(cue)
        while position >= 0:
            found.append((position, position + len(cue)))
            position = text.find(cue, position + 1)
    return sorted(found)


def _reaches_forward(prefix: str, cue: str) -> bool:
    """`cue` 在 `prefix` 里有没有一次出现，能一路管到 `prefix` 的末尾（数值处）。"""
    return any(_governs(prefix[end:]) for _start, end in _cue_positions(prefix, (cue,)))


def _denies(text: str, index: int) -> bool:
    """`text[index:]` 处的那个否定词，自己是不是也被否定了？

    `并不是没有99天带薪年假` 是双重否定——它在**肯定** 99 天。只看一层：三重否定
    这种拿不准的嵌套不豁免，宁可多问一次。
    """
    return any(
        end <= index and _governs(text[end:index])
        for _start, end in _cue_positions(text, _DENIAL_CUES)
    )


def _governs(span: str) -> bool:
    """修饰词和数值之间只隔着不引入新对象的东西吗？

    `span` 是修饰词末尾到数值开头之间的原文。空的当然算；`每年`、`说的`、`的`
    这类也算——它们不给修饰词提供新的对象。反过来，只要残留下任何别的字，或者中间
    还有**另一个数量**（`如果入职满一年就……99天` 里的"一年"），就说明修饰词已经
    有自己的对象了，管不到这个数。
    """
    residue = "".join(span.split())
    if _NUMBER_WITH_UNIT.search(residue):
        return False
    for token in _LIGHT_MODIFIERS:
        residue = residue.replace(token, "")
    return not residue


def validate_answer(
    answer_text: str, results: list[tuple[Chunk, float]], question: str
) -> tuple[bool, str]:
    """答案是否可以按原样交付。返回 (是否通过, 原因码)。

    纯判定，不改写答案：改写会把一个没有依据的结论装扮成有依据的样子。
    调用方在失败时改为拒答。

    覆盖范围有限，说清楚免得被当成更强的保证：本函数只判定五件可判定的事——
    空答案、复述问题、引用编号越界、有证据却无引用、以及证据和问题都没有的数量。
    **它不判断结论与所引证据在语义上是否相符**，那需要人工或独立验收逐条核对。
    """
    if not answer_text or not answer_text.strip():
        return False, "empty_answer"
    if restates_question(answer_text, question):
        return False, "restates_question"
    if invalid_citation_indices(answer_text, len(results)):
        return False, "citation_out_of_range"
    refusal = is_refusal(answer_text)
    # 免掉引用要求的是**纯拒答**，不是"出现过拒答短语"。`is_refusal()` 在整段做子串
    # 匹配，于是 `根据现有资料无法确定奖金金额。正式员工……每年享有5天带薪年假。`
    # 里那句没有来源标注的事实断言，跟着前半句一起被放行了。拒了一件事不等于
    # 后面那句也不用给依据。
    if results and not is_pure_refusal(answer_text) and not citation_indices(answer_text):
        # 给了证据、不是拒答、却一个来源都没标，说明这段文字没有落到证据上。
        # 实测到的形态是模型把"我将逐一检查资料"这类分析过程当成答案发出来：
        # 它不复述问题、不含数字、也没有越界编号，前面三项都拦不住。
        return False, "no_citation"
    # 拒答短语**不能**跳过事实核对。原来命中 `is_refusal()` 就提前返回，于是
    # `根据现有资料无法确定奖金金额。正式员工每年享有99天带薪年假。[来源1]`
    # 整段发出并落库——`ungrounded_quantities()` 明明已经查出这里的 99 天。
    # "含有拒答短语"不等于"整段没有事实断言"：拒了一件事，同时编造另一件事，
    # 是两件事。纯拒答仍然可交付，也仍然不要求引用（上一项已经放行）。
    if ungrounded_quantities(answer_text, results, question):
        return False, "ungrounded_quantity"
    # 实体身份：数量对、引用对，说的却是另一个商品。
    if unsupported_identifiers(answer_text, results, question):
        return False, "unsupported_identifier"
    # 把整段思考过程一起发了出来。放在过程描述**之前**：一段自述往往两条都命中，
    # 而"连草稿一起发了"是更具体的诊断，原因码应当说出这一点。
    if delivers_deliberation(answer_text):
        return False, "delivered_deliberation"
    # 只说了「我要去查」，没说查到什么。拒答和有数量的答案都已经先放行了。
    if describes_only_process(answer_text, results):
        return False, "incomplete_process"
    return True, "refusal" if refusal else "ok"


def needs_evidence_recheck(
    answer_text: str, results: list[tuple[Chunk, float]], question: str
) -> bool:
    """是否值得用既有的那一次证据复查再试一遍。

    触发条件比原来的"看起来像拒答"更宽：复述问题和无依据的数量同样说明首轮
    没有真正读证据。复查次数不变，仍是至多一次。
    """
    if not results:
        return False
    if is_refusal(answer_text):
        return True
    return not validate_answer(answer_text, results, question)[0]


def build_answer_messages(question: str, results: list[tuple[Chunk, float]], history: list[dict]) -> list[dict]:
    context = "\n\n".join(
        f"[来源 {i}：{chunk.source}，片段 {chunk.index}]\n{chunk.text}"
        for i, (chunk, _) in enumerate(results, start=1)
    )
    recent = history[-4:]
    messages = [
        {
            "role": "system",
            "content": (
                "你是严谨的企业知识库助手。请直接阅读用户消息中“资料”部分并回答“问题”。"
                "只能使用资料里的事实，不得使用外部知识。若资料明确包含答案，必须作答；"
                "只有资料确实没有相关信息时才说“根据现有资料无法确定”。"
                "最终答案控制在3句话以内，不要展示分析过程、推理步骤或自我检查。"
                "在每个关键结论后使用[来源1]这样的编号标注依据。 /no_think"
            ),
        },
        *recent,
        {"role": "user", "content": f"以下是检索到的资料：\n\n{context}\n\n请回答问题：{question}\n/no_think"},
    ]
    return messages


# 曾经这里有一个"逐段放行、只扣留未闭合来源标注"的窗口（`CITATION_HOLD_LIMIT`）。
# 它已被删除：那套做法只能挡住越界编号，挡不住复述问题、缺引用和无依据数量——
# 那三类要看完整答案才能判定，等判定出来正文已经发出，导致两条接口在同一份
# 脚本化输出下最终正文与落库结果不一致。现在统一为"整段校验通过后再发"。


def answer_stream(question: str, results: list[tuple[Chunk, float]], history: list[dict]):
    """流式解析结构化 JSON，**校验通过后**才向调用方输出最终答案字段。

    **输出前校验（整段缓冲）**。JSON 信封仍然逐块解析——转义、代理对、中途错误、
    缺少结束标记这些传输层问题照旧在读到的那一刻就抛出——但可见正文在整段答案解析
    完成并通过 `validate_answer` 之前**一个字都不发**。判定失败时发出的是拒答文案，
    与普通接口逐字相同。

    为什么必须这样：计划要求同一证据、同一模型输出下，两条接口的最终正文、拒答决定
    和持久化结果一致。边发边验做不到这一点——`no_citation`、复述问题、无依据数量都
    要看完整答案才能判定，等判定出来，错误正文已经在读者屏幕上了，收不回来。
    之前的实现走的就是「先发后验、失败改发 error」，在固定响应下稳定复现出两接口
    正文与落库结果不一致，属于未达标。

    **空证据没有例外。** 曾经保留「没有证据就逐段放行」的分支，理由是无证据时
    没有会被推翻的断言；但越界引用和复述问题在空证据下同样会发生，验收已复现
    「普通入口拒答、流式先发错误正文再报错」。该例外已按裁定取消。

    最终正文由 `decide_delivery` 决定——与 `answer_structured` 共用同一条
    生成后复查、正文提取与交付判定链，两条接口才会给出同一个答案。

    **代价（如实记录）**：首段可见正文不再是「模型吐出第一个字」，而是「整段生成
    完成」。实测影响见 `docs/M10_REVISION_1_REPORT.md`。这是为一致性付出的确定代价，
    不是可以两全的取舍。

    传输层错误仍然抛异常（调用方按错误处理、不落库）；交付校验失败**不抛异常**，
    而是返回拒答正文，让调用方按一次正常回答落库——与普通接口同样处理。
    """
    messages = build_answer_messages(question, results, history)
    schema = {
        "type": "object",
        "required": ["answer"],
        "properties": {"answer": {"type": "string"}},
    }
    full_json = ""
    prefix_buffer = ""
    answer_started = False
    answer_finished = False
    escaping = False
    unicode_digits: str | None = None
    pending_high_surrogate: int | None = None
    streamed_answer: list[str] = []

    # 传输层（NDJSON/SSE、error、done 标记）由 provider 处理，这里只拿到文本增量。
    for content in llm_provider.get_provider().chat_stream(messages, response_format=schema, temperature=0):
        full_json += content

        if not answer_started:
            prefix_buffer += content
            match = re.search(r'"answer"\s*:\s*"', prefix_buffer)
            if not match:
                continue
            answer_started = True
            content = prefix_buffer[match.end():]
            prefix_buffer = ""

        visible: list[str] = []
        for char in content:
            if answer_finished:
                break
            if unicode_digits is not None:
                unicode_digits += char
                if len(unicode_digits) == 4:
                    codepoint = int(unicode_digits, 16)
                    unicode_digits = None
                    escaping = False
                    if 0xD800 <= codepoint <= 0xDBFF:
                        pending_high_surrogate = codepoint
                    elif 0xDC00 <= codepoint <= 0xDFFF and pending_high_surrogate is not None:
                        combined = 0x10000 + ((pending_high_surrogate - 0xD800) << 10) + (codepoint - 0xDC00)
                        visible.append(chr(combined))
                        pending_high_surrogate = None
                    else:
                        if pending_high_surrogate is not None:
                            raise RuntimeError("结构化答案包含无效 Unicode 转义")
                        visible.append(chr(codepoint))
                continue
            if escaping:
                if char == "u":
                    unicode_digits = ""
                    continue
                escapes = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
                if char not in escapes:
                    raise RuntimeError("结构化答案包含无效转义")
                visible.append(escapes[char])
                escaping = False
                continue
            if char == "\\":
                escaping = True
            elif char == '"':
                answer_finished = True
            else:
                visible.append(char)

        if visible:
            # 只累积，不发出。空证据同样缓冲：越界引用和复述问题在没有证据时
            # 一样会发生，"提前发正文"的例外已被验收裁定取消。
            streamed_answer.append("".join(visible))

    if not answer_started or not answer_finished:
        raise RuntimeError("Ollama 未返回完整的结构化答案")
    try:
        parsed_answer = json.loads(full_json)["answer"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("Ollama 返回了无效的结构化答案") from exc
    if "".join(streamed_answer) != parsed_answer:
        raise RuntimeError("流式答案校验失败")

    # 输出前决策。与 answer_structured 共用 decide_delivery：**生成后的复查、
    # 正文提取、交付判定整条链**都是同一套，不只是共用校验函数。第一轮返修只共用
    # 了校验，结果普通接口靠复查纠正成了正确答案、流式没有复查直接拒答，
    # 两边保存的正文不同。复查在流式这边同样是至多一次，单题上限仍是 2。
    #
    # 注意这一步在流读完之后：流式连接已经结束，复查是另一次独立调用。
    yield decide_delivery(question, results, parsed_answer)
