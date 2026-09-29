"""Prompt text for the three Wiki maintenance stages.

Kept apart from `compiler.py` so the wording can be revised without touching the
control flow that validates the model's answer - and so a test can read the
prompt a stage produced without running a model.

Three things every stage is told, because each one closes a way the compiled
Wiki could quietly stop being traceable:

1. cite only the span ids you were given - an invented id is an untraceable
   claim wearing a citation;
2. copy numbers, never compute or round them - the source document is
   authoritative for exact figures, and a recomputed one is a new fact;
3. do not invent ids or versions - those are the program's to assign, so that
   two runs over the same input address the same page.

Pure strings. No model, no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence

from orchestration.wiki_schema import WikiPage

from .models import SourceSpan

PLAN_PREVIEW_LIMIT = 140

JSON_ONLY = "只输出一个 JSON 对象，不要输出解释、注释或代码块标记。"

DOCUMENT_DECISION_SYSTEM = f"""你是电商售后知识库的文档分诊员。
判断一份新上传的文档是否应该编入 Wiki。

规则：
- 与退货、换货、售后限制、促销规则或人工处理流程有关的文档选择 update。
- 与售后无关或无实质规则内容的文档选择 ignore；售后草稿和临时促销政策仍应编入草稿供审阅。
- 如果这份新文档明确取代了某些已有文档，把它们的 document_id 放进
  supersedes_document_ids；没有就留空数组。
- supersedes_document_ids 只能填写「当前已收录的 document_id」中列出的 id，
  不能重复，也不能包含这份文档自己的 document_id。

输出格式：
{{"action": "update", "reason": "简短理由", "supersedes_document_ids": []}}

{JSON_ONLY}"""

TOPIC_PLAN_SYSTEM = f"""你是电商售后知识库的 Wiki 主题规划员。
给定全部原文片段（Source Span），决定应该有哪些 Wiki 页面，以及每页使用哪些片段。

规则：
- 一个主题一页。同一主题的片段即使来自不同文档也要合并到同一页。
- 一份文档可以拆成多页。
- source_span_ids 只能填写下面列出的 span_id，不得编造。
- 下面列出的每一个片段都必须被分配到某一页，不能遗漏。
- 每个片段只能出现在一个页面里，不能重复分配。
- 每一页至少一个片段。
- 把高度相关的主题合并到同一页；但不要为了减少页数，把不相关的售后规则硬凑在一起。
- 页数没有上限，由内容本身决定。
- 如果已有页面覆盖同一主题，把它的 page_id 填进 existing_page_id 以便复用；
  existing_page_id 只能填上面「已有页面」中列出的 page_id，否则填 null。
  不要自己发明 page_id。

输出格式：
{{"pages": [{{"topic": "主题名", "existing_page_id": null, "source_span_ids": ["span-..."]}}]}}

{JSON_ONLY}"""

PAGE_COMPILATION_SYSTEM = f"""你是电商售后知识库的 Wiki 页面编写员。
本次会一次给你若干个页面，你必须为**每一个**页面都写出内容。

批次规则：
- 返回的 pages 数量必须与给出的页面数量完全一致，不能少写、重复或多写。
- 每个页面必须原样带回它的 index 和 topic，用于对应，不得改写。
- 每个页面只能引用**自己**那一节列出的 span_id，不能引用同批其他页面的片段。

单页规则：
- title 是主题名；summary 用一到两句话概括本页。
- aliases 是用户可能使用的别名，可以为空数组。
- claims 是可独立引用的条目，每条一句话，覆盖片段中的关键规定。
- 每条 claim 的 source_span_ids 只能填写下面列出的 span_id，至少一个。
- summary 和 claim 中出现的阿拉伯数字必须原样来自所引用的片段，
  不得改写、换算、四舍五入或推算。中文数字同理照抄。
- 不要输出 claim_id、page_id 或 version，这些由程序生成。
- 若某条 claim 与已有页面中的某条含义相同，把该 claim_id 填进 existing_claim_id
  以便复用；否则填 null。

输出格式：
{{"pages": [
  {{"index": 1, "topic": "原样带回的主题名", "title": "...", "summary": "...",
   "aliases": ["..."],
   "claims": [{{"text": "...", "source_span_ids": ["span-..."], "existing_claim_id": null}}]}}
]}}

{JSON_ONLY}"""

REPAIR_SYSTEM = f"""上一次回答被拒绝了，下面会给出被拒绝的原因。
请针对该原因修正后，重新输出同一个 JSON 对象。

- 如果原因是格式问题，只修格式，保留原有内容。
- 如果原因是内容不符合要求（例如页数超过上限、遗漏或重复片段），
  必须真正改正内容；原样重复上一次的回答会再次被拒绝。

{JSON_ONLY}"""


def _preview(text: str, limit: int = PLAN_PREVIEW_LIMIT) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "…"


def render_spans(spans: Sequence[SourceSpan], *, full_text: bool) -> str:
    """One line per span. Planning sees previews; compilation sees the text it
    must not paraphrase numbers out of."""
    lines = []
    for span in spans:
        heading = span.heading or "（无小节标题）"
        body = span.text if full_text else _preview(span.text)
        lines.append(f"- {span.span_id} | {span.source} | {heading}\n  {body}")
    return "\n".join(lines)


def render_existing_pages(pages: Sequence[WikiPage]) -> str:
    if not pages:
        return "（当前没有已有页面）"
    lines = []
    for page in pages:
        aliases = "、".join(page.aliases) if page.aliases else "无"
        lines.append(f"- {page.page_id} | {page.title} | 别名：{aliases}")
    return "\n".join(lines)


def render_existing_claims(page: WikiPage | None) -> str:
    if page is None or not page.claims:
        return "（这是一个新页面，没有可复用的 claim）"
    return "\n".join(f"- {claim.claim_id} | {claim.text}" for claim in page.claims)


def document_decision_prompt(
    *,
    filename: str,
    document_id: str,
    spans: Sequence[SourceSpan],
    active_document_ids: Sequence[str],
) -> str:
    active = "、".join(active_document_ids) if active_document_ids else "（无）"
    return (
        f"新文档：{filename}\n"
        f"document_id：{document_id}\n"
        f"片段数：{len(spans)}\n\n"
        f"当前已收录的 document_id：{active}\n\n"
        f"新文档内容：\n{render_spans(spans, full_text=False)}"
    )


def topic_plan_prompt(
    *, spans: Sequence[SourceSpan], existing_pages: Sequence[WikiPage]
) -> str:
    return (
        f"已有页面：\n{render_existing_pages(existing_pages)}\n\n"
        f"全部原文片段（共 {len(spans)} 条）：\n"
        f"{render_spans(spans, full_text=False)}"
    )


def page_compilation_prompt(
    *, topic: str, spans: Sequence[SourceSpan], existing_page: WikiPage | None
) -> str:
    """One page's section. Composed into a batch by `page_batch_prompt`."""
    return (
        f"topic：{topic}\n\n"
        f"可复用的已有 claim：\n{render_existing_claims(existing_page)}\n\n"
        f"本页的原文片段（共 {len(spans)} 条，只能引用这些）：\n"
        f"{render_spans(spans, full_text=True)}"
    )


def page_batch_prompt(
    entries: Sequence[tuple[int, str, Sequence[SourceSpan], WikiPage | None]],
) -> str:
    """Several pages in one request.

    Each section is headed by the `index` the model must carry back, so a
    reply can be matched to the page it was asked for rather than to the
    position it happened to arrive in.
    """
    sections = [
        f"===== 第 {index} 个页面 =====\n"
        f"index：{index}\n"
        + page_compilation_prompt(topic=topic, spans=spans, existing_page=existing_page)
        for index, topic, spans, existing_page in entries
    ]
    return (
        f"本批共 {len(entries)} 个页面，必须全部编写并全部返回。\n\n"
        + "\n\n".join(sections)
    )


def repair_prompt(*, original: str, invalid_output: str, error: str) -> str:
    return (
        f"原始任务：\n{original}\n\n"
        f"你上一次的输出：\n{invalid_output}\n\n"
        f"解析失败的原因：{error}"
    )
