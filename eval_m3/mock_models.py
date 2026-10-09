"""Offline stand-ins for the agent model and the judge (runner self-tests, --provider mock).

MockAgent is a crude, deterministic policy: read once (get_order when the
customer message names an order, else search_knowledge_base), then finish with
an answer that cites the first offered source. It exercises the runner's whole
path - product conversation, reads, generation, recording, judge, scoring -
without a network call; its scores mean nothing.

MockJudge answers every assertion as satisfied / not violated, with no window
statements and no history facts, unless a scripted verdict is supplied.
"""

from __future__ import annotations

import json
import re

from llm_provider import LLMResponse, ToolCall

ORDER = re.compile(r"ORD-\d{4}")


def _response(content: str = "", calls=()) -> LLMResponse:
    return LLMResponse(content=content, prompt_tokens=1000, completion_tokens=50, latency_seconds=0.01,
                       provider="mock", model="mock", tool_calls=tuple(calls),
                       finish_reason="tool_calls" if calls else "stop", raw_content=content)


class MockAgent:
    name = "mock"
    model = "mock"
    timeout = 30.0

    def chat(self, messages, *, response_format=None, tools=None, temperature=None, max_tokens=None):
        if tools:
            last_user = max(index for index, message in enumerate(messages) if message["role"] == "user")
            reads = sum(1 for message in messages[last_user:] if message["role"] == "tool")
            offered = {tool["function"]["name"] for tool in tools}
            text = str(messages[last_user].get("content") or "")
            if reads == 0:
                order = ORDER.findall(text)
                if order and "get_order" in offered:
                    name, arguments = "get_order", {"order_id": order[-1]}
                else:
                    name, arguments = "search_knowledge_base", {"query": text[-80:]}
            else:
                name, arguments = "finish", {"disposition": "answer"}
            raw = json.dumps(arguments, ensure_ascii=False)
            return _response(calls=[ToolCall(name=name, arguments=arguments, id="mock-" + name,
                                             raw_arguments=raw)])
        try:
            sources = json.loads(messages[-1]["content"]).get("sources") or []
        except (json.JSONDecodeError, AttributeError):
            sources = []
        refs = [sources[0]["ref"]] if sources else []
        return _response(json.dumps({"answer": "（模拟答复）请以查询结果为准。", "citation_refs": refs},
                                    ensure_ascii=False))


class MockJudge:
    name = "mock"
    model = "mock"
    timeout = 30.0

    def __init__(self, scripted: dict | None = None) -> None:
        self.scripted = scripted or {}
        self.requests: list[dict] = []

    def chat(self, messages, *, response_format=None, tools=None, temperature=None, max_tokens=None):
        request = json.loads(messages[-1]["content"])
        self.requests.append(request)
        override = self.scripted.get(request["customer_question"], {})
        verdict = {
            "must_include": [{"id": item["id"], "satisfied": True, "evidence": "mock"}
                             for item in request["must_include"]],
            "must_not_include": [{"id": item["id"], "violated": False, "evidence": "mock"}
                                 for item in request["must_not_include"]],
            "window_days": [], "history_reuse": {"facts": []}, **override}
        return _response(json.dumps(verdict, ensure_ascii=False))
