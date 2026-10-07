from __future__ import annotations

import json
from threading import Lock
from time import perf_counter
from typing import Literal

from fastapi import FastAPI, File, HTTPException, Query, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import agent_trace
import chat_orchestration
import wiki_runtime
from chat_orchestration import MODE_LEGACY, MODE_ORCHESTRATED

from agent import decide_action, list_sources, summarize_knowledge_base
from rag import (
    Chunk,
    answer_stream,
    answer_structured,
    build_index,
    check_ollama,
    read_file,
    reindex_chunks,
    retrieve_fast,
    split_text,
)
from storage import SQLiteStorage
from wiki_maintenance import derive_document_id
from aftersales_service.routes import router as aftersales_router


app = FastAPI(title="GroundedAgent After-Sales API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# GroundedAgent V2 M0: the after-sales product runtime (/api/aftersales/*).
app.include_router(aftersales_router)

storage = SQLiteStorage()
chunks: list[Chunk] = storage.load_chunks()
state_lock = Lock()
knowledge_mutation_lock = Lock()
conversation_locks: dict[str, Lock] = {}
knowledge_version = 0
#: `knowledge_version` at which each document was last uploaded. A Wiki job may
#: only retire a document it is at least as new as; anything uploaded since is
#: the user's newer intent and outranks a decision made before it existed.
document_upload_versions: dict[str, int] = {}


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    client_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    # Opt-in. Existing clients omit it and keep today's behavior exactly.
    mode: Literal["legacy", "orchestrated"] = MODE_LEGACY


class ConversationCreate(BaseModel):
    title: str = Field(default="新对话", max_length=60)
    client_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


def serialize_source(chunk: Chunk, score: float, rank: int) -> dict:
    heading = next(
        (line.removeprefix("## ") for line in chunk.text.splitlines() if line.startswith("## ")),
        "未命名章节",
    )
    return {
        "rank": rank,
        "source": chunk.source,
        "heading": heading,
        "chunk_index": chunk.index,
        "score": round(score, 4),
        "content": chunk.text,
    }


def sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def state_snapshot(
    conversation_id: str,
    client_id: str,
    *,
    require_chunks: bool = True,
) -> tuple[list[Chunk], list[dict[str, str]], int]:
    """Snapshot state for one request.

    `require_chunks=True` is the legacy path and keeps its error priority: with
    an empty knowledge base it returns early, before any ownership check, and
    the caller raises 409.

    `require_chunks=False` is used by the orchestrated mode, where a Wiki-only
    or inventory-only question does not depend on any document upload. It always
    validates ownership, reads history, and captures the version.
    """
    with state_lock:
        local_chunks = list(chunks)
        local_version = knowledge_version
        if require_chunks and not local_chunks:
            return local_chunks, [], local_version
        try:
            storage.ensure_conversation(conversation_id, client_id)
        except PermissionError as exc:
            raise HTTPException(404, "会话不存在或无权访问") from exc
        history = storage.get_recent_context(conversation_id, client_id, limit=4)
    return local_chunks, history, local_version


def commit_exchange(
    snapshot_version: int,
    payload: ChatRequest,
    reply: str,
    sources: list[dict],
    trace: dict,
) -> None:
    """Persist an answer only if it was generated from the current knowledge base."""
    with state_lock:
        if snapshot_version != knowledge_version:
            raise HTTPException(409, "知识库已更新，本次回答未保存，请重新提问")
        storage.commit_exchange(
            payload.session_id,
            payload.client_id,
            payload.question,
            reply,
            sources,
            trace,
        )


def acquire_conversation(conversation_id: str) -> Lock:
    with state_lock:
        conversation_lock = conversation_locks.setdefault(conversation_id, Lock())
    if not conversation_lock.acquire(blocking=False):
        raise HTTPException(409, "该会话正在生成回答，请稍后重试")
    return conversation_lock


@app.get("/api/health")
def health() -> dict:
    ok, detail = check_ollama()
    with state_lock:
        chunk_count = len(chunks)
    return {
        "status": "ok",
        "ollama_connected": ok,
        "ollama_detail": detail,
        "chunk_count": chunk_count,
    }


@app.get("/api/conversations")
def list_conversations(
    client_id: str = Query(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
) -> dict:
    return {"items": storage.list_conversations(client_id)}


@app.post("/api/conversations", status_code=201)
def create_conversation(payload: ConversationCreate) -> dict:
    return storage.create_conversation(payload.client_id, payload.title)


@app.get("/api/conversations/{conversation_id}/messages")
def conversation_messages(
    conversation_id: str,
    client_id: str = Query(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
) -> dict:
    try:
        return {"items": storage.get_messages(conversation_id, client_id)}
    except KeyError as exc:
        raise HTTPException(404, "会话不存在") from exc


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: str,
    client_id: str = Query(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
) -> dict:
    conversation_lock = acquire_conversation(conversation_id)
    try:
        if not storage.delete_conversation(conversation_id, client_id):
            raise HTTPException(404, "会话不存在")
        return {"status": "deleted"}
    finally:
        conversation_lock.release()


def retire_superseded_documents(
    document_ids: list[str], compiled_from_version: int | None
) -> tuple[str, ...]:
    """Drop the chunks of documents a published Wiki build superseded.

    Called by the Wiki worker once a build is live. It is the other half of the
    upsert: a document replacing *itself* is handled at upload, and a document
    replaced by a *different* file is handled here - otherwise the Wiki would
    retire a policy that `document_search` could still quote.

    Narrow on purpose. Only the ids the build actually superseded are
    considered, never the Wiki's active set: a document the Wiki merely ignored
    is still perfectly good source text, and filtering by "what the Wiki holds"
    would delete it. A document uploaded since the job started is newer than
    anything the job knows and is left alone.

    Takes `knowledge_mutation_lock` first, then `state_lock` - the same order
    the upload path uses. An upload holds the mutation lock across its whole
    body, including the embedding pass that runs outside `state_lock`; without
    waiting for it, a retirement could land between the upload's snapshot of the
    surviving chunks and its commit, and the commit would write the retired
    document straight back.

    Embeddings are never recomputed - surviving chunks are carried across
    untouched, and only their positions are renumbered.
    """
    global knowledge_version
    with knowledge_mutation_lock, state_lock:
        retirable = {
            document_id
            for document_id in document_ids
            if compiled_from_version is None
            or document_upload_versions.get(document_id, -1) <= compiled_from_version
        }
        if not retirable:
            return ()
        kept = [
            Chunk(
                text=chunk.text,
                source=chunk.source,
                index=chunk.index,
                embedding=chunk.embedding,
            )
            for chunk in chunks
            if derive_document_id(chunk.source) not in retirable
        ]
        if len(kept) == len(chunks):
            return ()
        reindex_chunks(kept)
        # Chunks only: this is a background consequence of an upload the user
        # already made, not a new one, so it must not clear their conversations.
        storage.replace_chunks(kept)
        chunks[:] = kept
        for document_id in retirable:
            document_upload_versions.pop(document_id, None)
        knowledge_version += 1
        return tuple(sorted(retirable))


def _upload_name(file: UploadFile) -> str:
    return file.filename or "document"


def _reject_duplicate_documents(names: list[str]) -> None:
    """One batch may name each document at most once."""
    by_document: dict[str, list[str]] = {}
    for name in names:
        by_document.setdefault(derive_document_id(name), []).append(name)
    repeated = sorted(
        group[0] for group in by_document.values() if len(group) > 1
    )
    if repeated:
        raise HTTPException(
            400,
            "同一批次不能包含重复文档：" + "、".join(repeated) + "。请分批上传或重命名后重试。",
        )


@app.post("/api/knowledge/upload")
async def upload_knowledge(files: list[UploadFile] = File(...)) -> dict:
    global knowledge_version
    if not knowledge_mutation_lock.acquire(blocking=False):
        raise HTTPException(409, "知识库正在更新，请稍后重试")
    try:
        # Rejected before anything is read, embedded, stored or queued. Two
        # files with one document identity would leave retrieval holding both
        # versions while the Wiki compiled only one, and there is no safe
        # tie-break: keeping either would silently discard content the user just
        # uploaded, which is worse than refusing the batch.
        _reject_duplicate_documents([_upload_name(file) for file in files])

        parsed: list[Chunk] = []
        names: list[str] = []
        documents: list[tuple[str, str]] = []
        for file in files:
            name = _upload_name(file)
            if name.lower().rsplit(".", 1)[-1] not in {"pdf", "txt", "md"}:
                raise HTTPException(400, f"不支持的文件类型：{name}")
            raw = await file.read()
            text = await run_in_threadpool(read_file, name, raw)
            parsed.extend(split_text(text, name))
            names.append(name)
            # The Wiki compiles from the raw text, not from the retrieval
            # chunks: chunk boundaries move whenever a document is edited.
            documents.append((name, text))
        if not parsed:
            raise HTTPException(400, "文件中没有可索引的文本")

        # Upsert by document identity, not wholesale replacement. The Wiki
        # accumulates documents, so a retrieval index that replaced everything
        # would leave the Wiki summarising a document whose source text
        # `document_search` could no longer find. `derive_document_id` is the
        # Wiki's own notion of "the same document", reused here so the two
        # cannot disagree about what a re-upload replaces.
        #
        # This half handles a document replacing *itself*. A document replaced
        # by a *different* file is retired by `retire_superseded_documents`
        # once the Wiki build that decided it goes live.
        incoming_ids = {derive_document_id(name) for name in names}
        with state_lock:
            # Copied so a failed commit cannot leave live chunks renumbered.
            kept = [
                Chunk(
                    text=chunk.text,
                    source=chunk.source,
                    index=chunk.index,
                    embedding=chunk.embedding,
                )
                for chunk in chunks
                if derive_document_id(chunk.source) not in incoming_ids
            ]

        stats: dict = {}
        # Only the incoming chunks are embedded. A document nobody touched keeps
        # the vectors it already has, so re-uploading one file does not re-pay
        # for the whole knowledge base.
        indexed_new = await run_in_threadpool(build_index, parsed, stats)

        merged = kept + list(indexed_new)
        # split_text 会为每个文件从 1 编号；合并后改为全局唯一编号。
        reindex_chunks(merged)
        with state_lock:
            storage.replace_knowledge(merged)
            chunks[:] = merged
            knowledge_version += 1
            for document_id in incoming_ids:
                document_upload_versions[document_id] = knowledge_version
            compiled_from_version = knowledge_version
        # Queued, not awaited: retrieval is ready now, and Wiki compilation
        # takes model time the uploader should not sit through. Progress is
        # polled from /api/wiki/status.
        wiki_job_id = wiki_runtime.RUNTIME.submit(
            documents,
            knowledge_version=compiled_from_version,
            retire_documents=retire_superseded_documents,
        )
        return {
            "files": names,
            **stats,
            # `chunks` stays what it has always meant to the client: the size of
            # the whole knowledge base. `uploaded_chunks` is what this upload
            # contributed, which is what `stats` was counting.
            "chunks": len(merged),
            "uploaded_chunks": len(indexed_new),
            "wiki_job_id": wiki_job_id,
        }
    finally:
        knowledge_mutation_lock.release()


@app.get("/api/wiki/status")
def wiki_status(job_id: str | None = Query(default=None)) -> dict:
    """Progress of a Wiki job; the most recent one when no id is given."""
    return wiki_runtime.RUNTIME.status(job_id)


@app.delete("/api/knowledge")
def clear_knowledge() -> dict:
    global knowledge_version
    if not knowledge_mutation_lock.acquire(blocking=False):
        raise HTTPException(409, "知识库正在更新，请稍后重试")
    try:
        with state_lock:
            storage.clear_knowledge()
            chunks.clear()
            document_upload_versions.clear()
            knowledge_version += 1
        # Void pending jobs and take the compiled Wiki offline: it was derived
        # from documents that no longer exist. Snapshots and builds stay on
        # disk, and the static sample answers again.
        wiki_runtime.RUNTIME.clear()
        return {"status": "cleared"}
    finally:
        knowledge_mutation_lock.release()


def start_trace_run(payload: ChatRequest, entrypoint: str, *, streaming: bool):
    """A trace run stored next to the conversations (tests swap `storage`)."""
    return agent_trace.start_run(
        storage.path, kind="api", entrypoint=entrypoint, mode=payload.mode,
        streaming=streaming, question=payload.question, session_id=payload.session_id,
        client_id=payload.client_id, knowledge_version=knowledge_version,
    )


def legacy_tool_call(tool_name: str, arguments: dict, call) -> str:
    """Run one text-returning legacy tool inside a tool_call span."""
    with agent_trace.span("tool_call", tool_name, input={"arguments": arguments}) as span:
        reply = call()
        if span:
            span.output = {"result": reply}
    return reply


def traced_commit(snapshot_version, payload, reply, sources, trace) -> None:
    with agent_trace.span("commit", "commit_exchange",
                          input={"knowledge_version": snapshot_version, "sources": len(sources)}):
        commit_exchange(snapshot_version, payload, reply, sources, trace)


@app.post("/api/chat")
def chat(payload: ChatRequest, response: Response) -> dict:
    run = start_trace_run(payload, "/api/chat", streaming=False)
    if run.run_id:
        response.headers["X-Run-Id"] = run.run_id
    with run:
        conversation_lock = acquire_conversation(payload.session_id)
        try:
            if payload.mode == MODE_ORCHESTRATED:
                return orchestrated_chat(payload)

            local_chunks, local_history, snapshot_version = state_snapshot(
                payload.session_id,
                payload.client_id,
            )
            if not local_chunks:
                raise HTTPException(409, "请先上传文档并建立知识库")

            started = perf_counter()
            with agent_trace.span("router", "decide_action", input={
                "question": payload.question, "history_turns": len(local_history),
            }) as span:
                decision = decide_action(payload.question, local_history)
                if span:
                    span.output = decision
            tool_name = decision.get("tool", "direct")
            results: list[tuple[Chunk, float]] = []
            trace = {"agent_seconds": decision["seconds"], "tool": tool_name}

            if decision["type"] == "direct":
                reply = decision["content"]
            elif tool_name == "list_knowledge_sources":
                reply = legacy_tool_call(tool_name, decision.get("arguments") or {},
                                         lambda: list_sources(local_chunks))
            elif tool_name == "summarize_knowledge_base":
                reply = legacy_tool_call(tool_name, decision.get("arguments") or {},
                                         lambda: summarize_knowledge_base(local_chunks))
            else:
                query = decision.get("arguments", {}).get("query") or payload.question
                with agent_trace.span("tool_call", "search_knowledge_base", input={
                    "arguments": {"query": query}, "chunks": len(local_chunks),
                }) as span:
                    results = retrieve_fast(query, local_chunks, trace=trace)
                    if span:
                        span.output = {
                            "results": [serialize_source(c, s, i) for i, (c, s) in enumerate(results, 1)],
                            "retrieval_trace": dict(trace),
                        }
                agent_trace.record("evidence", "retrieved_sources", output={
                    "sources": [{"source": c.source, "chunk_index": c.index, "score": s} for c, s in results],
                })
                answer_started = perf_counter()
                with agent_trace.span("generation", "answer_structured", input=agent_trace.generation_input(
                        payload.question, results, local_history)) as span:
                    reply = answer_structured(payload.question, results, local_history)
                    if span:
                        span.output = {"answer": reply}
                trace["answer_seconds"] = perf_counter() - answer_started

            trace["total_seconds"] = perf_counter() - started
            sources = [serialize_source(c, s, i) for i, (c, s) in enumerate(results, 1)]
            traced_commit(snapshot_version, payload, reply, sources, trace)
            return {"answer": reply, "trace": trace, "sources": sources}
        finally:
            conversation_lock.release()


def orchestrated_chat(payload: ChatRequest) -> dict:
    """The M1-M6A chain behind /api/chat. The caller holds the conversation lock."""
    local_chunks, local_history, snapshot_version = state_snapshot(
        payload.session_id,
        payload.client_id,
        require_chunks=False,
    )
    started = perf_counter()
    prepared = chat_orchestration.prepare(payload.question, local_chunks)
    trace = chat_orchestration.build_trace(prepared)

    if prepared.needs_generation:
        answer_started = perf_counter()
        with agent_trace.span("generation", "answer_structured", input=agent_trace.generation_input(
                payload.question, prepared.results_for_answer, local_history)) as span:
            reply = answer_structured(
                payload.question, prepared.results_for_answer, local_history
            )
            if span:
                span.output = {"answer": reply}
        trace["answer_seconds"] = perf_counter() - answer_started
    else:
        reply = prepared.fixed_answer

    trace["total_seconds"] = perf_counter() - started
    traced_commit(snapshot_version, payload, reply, prepared.sources, trace)
    return {
        "answer": reply,
        "trace": trace,
        "sources": prepared.sources,
        "route": prepared.route,
        "steps": prepared.steps,
    }


def orchestrated_stream(payload: ChatRequest, conversation_lock: Lock):
    """SSE for the orchestrated chain. Owns releasing the conversation lock."""
    local_chunks, local_history, snapshot_version = state_snapshot(
        payload.session_id,
        payload.client_id,
        require_chunks=False,
    )

    def generate():
        started = perf_counter()
        try:
            yield sse_event("status", {"phase": "routing", "message": "Agent 正在规划信息通道"})
            prepared = chat_orchestration.prepare(payload.question, local_chunks)
            trace = chat_orchestration.build_trace(prepared)
            yield sse_event("sources", {"sources": prepared.sources})

            if prepared.needs_generation:
                yield sse_event(
                    "status", {"phase": "generating", "message": "正在基于证据生成答案"}
                )
                answer_started = perf_counter()
                reply_parts: list[str] = []
                with agent_trace.span("generation", "answer_stream", input=agent_trace.generation_input(
                        payload.question, prepared.results_for_answer, local_history)) as span:
                    for part in answer_stream(
                        payload.question, prepared.results_for_answer, local_history
                    ):
                        reply_parts.append(part)
                        yield sse_event("delta", {"content": part})
                    trace["answer_seconds"] = perf_counter() - answer_started
                    reply = "".join(reply_parts).strip()
                    if span:
                        span.output = {"answer": reply}
                    if not reply:
                        raise RuntimeError("模型未返回可显示的答案")
            else:
                reply = prepared.fixed_answer
                yield sse_event("delta", {"content": reply})

            trace["total_seconds"] = perf_counter() - started
            traced_commit(snapshot_version, payload, reply, prepared.sources, trace)
            yield sse_event("done", {"trace": trace})
        except Exception as exc:
            # Fixed payload: str(exc) is never interpolated, nothing is
            # persisted, and no done event is emitted.
            agent_trace.fail_current(exc, sse_error_event=True)
            yield sse_event("error", chat_orchestration.STREAM_ERROR_EVENT)
        finally:
            conversation_lock.release()

    return generate


def stream_headers(run) -> dict:
    headers = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}
    if run.run_id:
        headers["X-Run-Id"] = run.run_id
    return headers


@app.post("/api/chat/stream")
def chat_stream(payload: ChatRequest) -> StreamingResponse:
    run = start_trace_run(payload, "/api/chat/stream", streaming=True)
    try:
        conversation_lock = acquire_conversation(payload.session_id)
    except BaseException as exc:
        run.abort(exc)
        raise
    try:
        if payload.mode == MODE_ORCHESTRATED:
            generate = orchestrated_stream(payload, conversation_lock)
            return StreamingResponse(
                run.iterate(generate()),
                media_type="text/event-stream",
                headers=stream_headers(run),
            )

        local_chunks, local_history, snapshot_version = state_snapshot(
            payload.session_id,
            payload.client_id,
        )
        if not local_chunks:
            raise HTTPException(409, "请先上传文档并建立知识库")
    except Exception as exc:
        conversation_lock.release()
        run.abort(exc)
        raise

    def generate():
        started = perf_counter()
        reply_parts: list[str] = []
        results: list[tuple[Chunk, float]] = []
        sources: list[dict] = []
        try:
            yield sse_event("status", {"phase": "routing", "message": "Agent 正在判断问题类型"})
            with agent_trace.span("router", "decide_action", input={
                "question": payload.question, "history_turns": len(local_history),
            }) as span:
                decision = decide_action(payload.question, local_history)
                if span:
                    span.output = decision
            tool_name = decision.get("tool", "direct")
            trace = {"agent_seconds": decision["seconds"], "tool": tool_name}

            if decision["type"] == "direct":
                reply_parts.append(decision["content"])
                yield sse_event("delta", {"content": decision["content"]})
            elif tool_name == "list_knowledge_sources":
                reply = legacy_tool_call(tool_name, decision.get("arguments") or {},
                                         lambda: list_sources(local_chunks))
                reply_parts.append(reply)
                yield sse_event("delta", {"content": reply})
            elif tool_name == "summarize_knowledge_base":
                yield sse_event("status", {"phase": "generating", "message": "正在总结知识库"})
                reply = legacy_tool_call(tool_name, decision.get("arguments") or {},
                                         lambda: summarize_knowledge_base(local_chunks))
                reply_parts.append(reply)
                yield sse_event("delta", {"content": reply})
            else:
                yield sse_event("status", {"phase": "retrieving", "message": "正在检索并重排相关证据"})
                query = decision.get("arguments", {}).get("query") or payload.question
                with agent_trace.span("tool_call", "search_knowledge_base", input={
                    "arguments": {"query": query}, "chunks": len(local_chunks),
                }) as span:
                    results = retrieve_fast(query, local_chunks, trace=trace)
                    sources = [serialize_source(c, s, i) for i, (c, s) in enumerate(results, 1)]
                    if span:
                        span.output = {"results": sources, "retrieval_trace": dict(trace)}
                agent_trace.record("evidence", "retrieved_sources", output={
                    "sources": [{"source": c.source, "chunk_index": c.index, "score": s} for c, s in results],
                })
                yield sse_event("sources", {"sources": sources})
                yield sse_event("status", {"phase": "generating", "message": "正在基于证据生成答案"})
                answer_started = perf_counter()
                with agent_trace.span("generation", "answer_stream", input=agent_trace.generation_input(
                        payload.question, results, local_history)) as span:
                    for part in answer_stream(payload.question, results, local_history):
                        reply_parts.append(part)
                        yield sse_event("delta", {"content": part})
                    if span:
                        span.output = {"answer": "".join(reply_parts).strip()}
                trace["answer_seconds"] = perf_counter() - answer_started

            reply = "".join(reply_parts).strip()
            if not reply:
                raise RuntimeError("模型未返回可显示的答案")
            trace["total_seconds"] = perf_counter() - started
            traced_commit(snapshot_version, payload, reply, sources, trace)
            yield sse_event("done", {"trace": trace})
        except Exception as exc:
            agent_trace.fail_current(exc, sse_error_event=True)
            yield sse_event(
                "error",
                {"code": "AGENT_STREAM_ERROR", "message": f"Agent 运行失败：{exc}"},
            )
        finally:
            conversation_lock.release()

    return StreamingResponse(
        run.iterate(generate()),
        media_type="text/event-stream",
        headers=stream_headers(run),
    )
