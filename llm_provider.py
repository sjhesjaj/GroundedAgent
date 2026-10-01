"""One chat interface over local Ollama and OpenAI-compatible APIs (DeepSeek).

Business code asks `get_provider()` for a provider and gets back an
`LLMResponse`: the final text, token usage and latency, in the same shape
whichever model answered. Model differences stay in this module:

- Qwen3 on Ollama may leave `<think>` text in `content` or return it in
  `message.thinking`; DeepSeek returns `reasoning_content`. Both end up in
  `LLMResponse.reasoning`, never in `content`.
- Ollama takes a JSON Schema in `format`; DeepSeek only offers
  `{"type": "json_object"}` and requires the word "json" in the prompt. The
  DeepSeek provider therefore appends a format instruction derived from the
  schema. That is the only way an effective prompt differs from the logical one,
  and each such change is listed in `LLMResponse.prompt_adaptations`.
- Ollama reports `prompt_eval_count`/`eval_count`; the OpenAI format reports
  `usage.prompt_tokens`/`usage.completion_tokens`.

`OllamaProvider` sends exactly the request bodies the code sent before this
module existed, so local Qwen behaviour is unchanged. Both providers call
`requests.post` at call time, which keeps tests that patch it working.

Network and HTTP errors are not wrapped: `requests.RequestException` reaches
the caller exactly as before, so existing fallbacks keep working.

Configuration comes from the process environment and a `.env` file (see
`.env.example`); the API key is read from the `.env` file only.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Iterator, Protocol

import requests

import agent_trace


ROOT = Path(__file__).resolve().parent
DEFAULT_DOTENV = ROOT / ".env"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_OLLAMA_CHAT_MODEL = "qwen3:4b"
DEFAULT_TIMEOUT_SECONDS = 180.0
STREAM_CONNECT_TIMEOUT_SECONDS = 10.0
PROVIDERS = ("ollama", "deepseek")


class LLMConfigError(ValueError):
    """Raised when the configured provider is missing a required setting."""


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    name: str | None
    arguments: dict
    # The provider's own call id, when it gives one (the OpenAI format always
    # does; Ollama may not). Replaying it keeps a tool result linked to the
    # exact call the model made.
    id: str | None = None
    # `function.arguments` exactly as returned, before JSON parsing, when the
    # provider sends a string (OpenAI format). `arguments` is the parsed form.
    raw_arguments: str | None = None


@dataclass(frozen=True)
class LLMResponse:
    content: str
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_seconds: float
    provider: str
    model: str
    reasoning: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    # Each entry records how the effective prompt differs from the caller's.
    prompt_adaptations: tuple[dict, ...] = ()
    # The model's text exactly as returned. `content` has inline <think> text
    # removed; callers that own their own extraction order (rag's
    # extract_answer_text parses the JSON envelope *before* stripping think
    # shells, so a literal "<think>" inside an answer survives) read this.
    raw_content: str = ""


class LLMStream:
    """Iterate for text deltas; `response` is filled in once iteration ends."""

    def __init__(self, produce: Callable[["LLMStream"], Iterator[str]]) -> None:
        self._produce = produce
        self.response: LLMResponse | None = None

    def __iter__(self) -> Iterator[str]:
        yield from self._produce(self)


class LLMProvider(Protocol):
    name: str
    model: str

    def chat(
        self,
        messages: list[dict],
        *,
        response_format: str | dict | None = None,
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...

    def chat_stream(
        self,
        messages: list[dict],
        *,
        response_format: str | dict | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMStream: ...


# --------------------------------------------------------------------------
# Shared normalisation
# --------------------------------------------------------------------------

_THINK_BLOCK = re.compile(r"<think>.*?</think>", flags=re.DOTALL)
_THINK_OPEN = re.compile(r"<think>.*", flags=re.DOTALL)


def split_think(content: str) -> tuple[str, str | None]:
    """Separate Qwen3-style `<think>` text from the final answer.

    Same rule the answer path used before: when a closing tag exists the answer
    follows the last one (some Qwen3/Ollama builds drop the opening tag);
    otherwise complete blocks and an unterminated trailing block are removed.
    """
    if "</think>" in content:
        reasoning, _, answer = content.rpartition("</think>")
        return answer, reasoning.replace("<think>", "").strip() or None
    blocks = _THINK_BLOCK.findall(content) + _THINK_OPEN.findall(_THINK_BLOCK.sub("", content))
    if not blocks:
        return content, None
    answer = _THINK_OPEN.sub("", _THINK_BLOCK.sub("", content))
    reasoning = "\n".join(re.sub(r"</?think>", "", block).strip() for block in blocks)
    return answer, reasoning or None


def _join_reasoning(*parts: str | None) -> str | None:
    present = [part for part in parts if part]
    return "\n".join(present) if present else None


# --------------------------------------------------------------------------
# Ollama
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OllamaProvider:
    base_url: str = DEFAULT_OLLAMA_BASE_URL
    model: str = DEFAULT_OLLAMA_CHAT_MODEL
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    name: str = "ollama"

    def _payload(self, messages, *, stream, response_format, tools, temperature, max_tokens) -> dict:
        # Key order and values match the pre-provider request bodies exactly.
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
        payload["stream"] = stream
        payload["think"] = False
        if response_format is not None:
            payload["format"] = response_format
        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        if options:
            payload["options"] = options
        return payload

    def chat(self, messages, *, response_format=None, tools=None, temperature=None, max_tokens=None) -> LLMResponse:
        started = perf_counter()
        response = requests.post(
            f"{self.base_url}/api/chat",
            json=self._payload(messages, stream=False, response_format=response_format,
                               tools=tools, temperature=temperature, max_tokens=max_tokens),
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        message = data["message"]
        raw_content = message.get("content") or ""
        content, inline_reasoning = split_think(raw_content)
        calls = tuple(
            ToolCall(
                name=(call.get("function") or {}).get("name"),
                arguments=(call.get("function") or {}).get("arguments") or {},
                id=call.get("id") if isinstance(call.get("id"), str) else None,
            )
            for call in message.get("tool_calls") or []
        )
        return LLMResponse(
            content=content,
            prompt_tokens=data.get("prompt_eval_count"),
            completion_tokens=data.get("eval_count"),
            latency_seconds=perf_counter() - started,
            provider=self.name,
            model=self.model,
            reasoning=_join_reasoning(message.get("thinking"), inline_reasoning),
            tool_calls=calls,
            finish_reason=data.get("done_reason"),
            raw_content=raw_content,
        )

    def chat_stream(self, messages, *, response_format=None, temperature=None, max_tokens=None) -> LLMStream:
        payload = self._payload(messages, stream=True, response_format=response_format,
                                tools=None, temperature=temperature, max_tokens=max_tokens)

        def produce(stream: LLMStream) -> Iterator[str]:
            started = perf_counter()
            thinking: list[str] = []
            with requests.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=(STREAM_CONNECT_TIMEOUT_SECONDS, self.timeout),
                stream=True,
            ) as response:
                response.raise_for_status()
                final: dict = {}
                for line in response.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise RuntimeError(f"Ollama 流式生成失败：{chunk['error']}")
                    message = chunk.get("message", {})
                    if message.get("thinking"):
                        thinking.append(message["thinking"])
                    if chunk.get("done"):
                        final = chunk
                    content = message.get("content", "")
                    if content:
                        yield content
                if not final:
                    raise RuntimeError("Ollama 流式响应意外中断")
            stream.response = LLMResponse(
                content="",  # deltas were handed to the caller; not re-buffered here
                prompt_tokens=final.get("prompt_eval_count"),
                completion_tokens=final.get("eval_count"),
                latency_seconds=perf_counter() - started,
                provider=self.name,
                model=self.model,
                reasoning="".join(thinking) or None,
                finish_reason=final.get("done_reason"),
            )

        return LLMStream(produce)


# --------------------------------------------------------------------------
# OpenAI-compatible (DeepSeek)
# --------------------------------------------------------------------------

JSON_INSTRUCTION = "请只输出一个合法的 JSON 对象，不要输出 JSON 以外的任何内容。"
SCHEMA_INSTRUCTION = "请只输出一个合法的 JSON 对象，不要输出 JSON 以外的任何内容。该 JSON 必须符合以下 JSON Schema：{schema}"


def adapt_json_prompt(messages: list[dict], response_format: str | dict | None) -> tuple[list[dict], tuple[dict, ...]]:
    """Make a JSON request valid for `json_object` mode without touching Qwen prompts.

    A schema cannot be sent to DeepSeek, so it is described in words; a bare
    "json" request only needs an instruction when the prompt lacks the word
    "json", which DeepSeek requires. The instruction is appended to the first
    system message (or becomes one), and the change is returned for the record.
    """
    if response_format is None:
        return messages, ()
    if isinstance(response_format, dict):
        instruction = SCHEMA_INSTRUCTION.format(
            schema=json.dumps(response_format, ensure_ascii=False, separators=(",", ":"))
        )
        reason = "schema_not_supported_by_json_object"
    elif any("json" in str(m.get("content", "")).lower() for m in messages):
        return messages, ()
    else:
        instruction = JSON_INSTRUCTION
        reason = "json_object_requires_word_json"
    adapted = [dict(m) for m in messages]
    system_index = next((i for i, m in enumerate(adapted) if m.get("role") == "system"), None)
    if system_index is None:
        adapted.insert(0, {"role": "system", "content": instruction})
        position = "new_system_message"
    else:
        adapted[system_index]["content"] = f"{adapted[system_index].get('content', '')}\n\n{instruction}"
        position = f"appended_to_messages[{system_index}]"
    return adapted, ({"kind": "json_format_instruction", "reason": reason,
                      "position": position, "text": instruction},)


@dataclass(frozen=True)
class OpenAICompatibleProvider:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    name: str = "deepseek"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _payload(self, messages, *, stream, response_format, tools, temperature, max_tokens):
        effective, adaptations = adapt_json_prompt(messages, response_format)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": effective,
            "stream": stream,
            # Stage 0 pins thinking off, matching `think: false` on the Qwen path.
            "thinking": {"type": "disabled"},
        }
        if response_format is not None:
            payload["response_format"] = {"type": "json_object"}
        if tools:
            payload["tools"] = tools
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload, adaptations

    def _raise_for_status(self, response) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            # The body carries the API's reason; headers (and the key) are never included.
            detail = getattr(response, "text", "")[:500]
            raise requests.HTTPError(f"{exc} | {detail}", response=response) from None

    def chat(self, messages, *, response_format=None, tools=None, temperature=None, max_tokens=None) -> LLMResponse:
        started = perf_counter()
        payload, adaptations = self._payload(messages, stream=False, response_format=response_format,
                                             tools=tools, temperature=temperature, max_tokens=max_tokens)
        response = requests.post(
            f"{self.base_url}/chat/completions",
            json=payload, headers=self._headers(), timeout=self.timeout,
        )
        self._raise_for_status(response)
        data = response.json()
        choice = data["choices"][0]
        message = choice["message"]
        raw_content = message.get("content") or ""
        content, inline_reasoning = split_think(raw_content)
        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            arguments = function.get("arguments") or {}
            raw_arguments = arguments if isinstance(arguments, str) else None
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments) if arguments.strip() else {}
                except json.JSONDecodeError:
                    arguments = {}
            call_id = call.get("id")
            calls.append(ToolCall(name=function.get("name"), arguments=arguments,
                                  id=call_id if isinstance(call_id, str) else None,
                                  raw_arguments=raw_arguments))
        usage = data.get("usage") or {}
        return LLMResponse(
            content=content,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            latency_seconds=perf_counter() - started,
            provider=self.name,
            model=data.get("model") or self.model,
            reasoning=_join_reasoning(message.get("reasoning_content"), inline_reasoning),
            tool_calls=tuple(calls),
            finish_reason=choice.get("finish_reason"),
            prompt_adaptations=adaptations,
            raw_content=raw_content,
        )

    def chat_stream(self, messages, *, response_format=None, temperature=None, max_tokens=None) -> LLMStream:
        payload, adaptations = self._payload(messages, stream=True, response_format=response_format,
                                             tools=None, temperature=temperature, max_tokens=max_tokens)

        def produce(stream: LLMStream) -> Iterator[str]:
            started = perf_counter()
            reasoning: list[str] = []
            usage: dict = {}
            finish_reason = None
            done = False
            with requests.post(
                f"{self.base_url}/chat/completions",
                json=payload, headers=self._headers(),
                timeout=(STREAM_CONNECT_TIMEOUT_SECONDS, self.timeout), stream=True,
            ) as response:
                self._raise_for_status(response)
                for line in response.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        done = True
                        break
                    chunk = json.loads(data)
                    if chunk.get("error"):
                        raise RuntimeError(f"{self.name} 流式生成失败：{chunk['error']}")
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("reasoning_content"):
                            reasoning.append(delta["reasoning_content"])
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                        if delta.get("content"):
                            yield delta["content"]
            if not done:
                raise RuntimeError(f"{self.name} 流式响应意外中断")
            stream.response = LLMResponse(
                content="",
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                latency_seconds=perf_counter() - started,
                provider=self.name,
                model=self.model,
                reasoning="".join(reasoning) or None,
                finish_reason=finish_reason,
                prompt_adaptations=adaptations,
            )

        return LLMStream(produce)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def read_dotenv(path: Path | None) -> dict[str, str]:
    """Minimal `KEY=VALUE` parser: comments, blank lines, optional quotes."""
    if path is None or not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _dotenv_path(environ) -> Path | None:
    # LLM_DOTENV="" disables the file (the unit tests do this); unset means ./.env.
    if "LLM_DOTENV" in environ:
        return Path(environ["LLM_DOTENV"]) if environ["LLM_DOTENV"] else None
    return DEFAULT_DOTENV


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    base_url: str
    model: str
    api_key: str | None = field(default=None, repr=False)
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    def public_dict(self) -> dict:
        data = asdict(self)
        data.pop("api_key")
        data["api_key_set"] = bool(self.api_key)
        return data


def load_config(provider: str | None = None, *, environ=None, dotenv_path: Path | None | str = "default") -> LLMConfig:
    """Resolve settings: process environment first, then the `.env` file.

    The API key is the exception - it is read from the `.env` file only.
    """
    environ = os.environ if environ is None else environ
    path = _dotenv_path(environ) if dotenv_path == "default" else (Path(dotenv_path) if dotenv_path else None)
    dotenv = read_dotenv(path)

    def setting(name: str) -> str | None:
        value = environ.get(name)
        if value is None or value == "":
            value = dotenv.get(name)
        return value or None

    chosen = (provider or setting("LLM_PROVIDER") or "ollama").strip().lower()
    if chosen not in PROVIDERS:
        raise LLMConfigError(f"LLM_PROVIDER 必须是 {'/'.join(PROVIDERS)} 之一，当前为 {chosen!r}")
    timeout = float(setting("LLM_TIMEOUT") or DEFAULT_TIMEOUT_SECONDS)
    if chosen == "ollama":
        return LLMConfig(
            provider="ollama",
            base_url=(setting("OLLAMA_BASE_URL") or DEFAULT_OLLAMA_BASE_URL).rstrip("/"),
            model=setting("OLLAMA_CHAT_MODEL") or DEFAULT_OLLAMA_CHAT_MODEL,
            timeout=timeout,
        )
    base_url, model = setting("DEEPSEEK_BASE_URL"), setting("DEEPSEEK_MODEL")
    api_key = dotenv.get("DEEPSEEK_API_KEY") or None
    missing = [name for name, value in (("DEEPSEEK_BASE_URL", base_url), ("DEEPSEEK_MODEL", model),
                                        ("DEEPSEEK_API_KEY", api_key)) if not value]
    if missing:
        raise LLMConfigError(f"LLM_PROVIDER=deepseek 缺少配置：{', '.join(missing)}（见 .env.example）")
    return LLMConfig(provider="deepseek", base_url=base_url.rstrip("/"), model=model,
                     api_key=api_key, timeout=timeout)


def create_provider(config: LLMConfig) -> LLMProvider:
    if config.provider == "ollama":
        return OllamaProvider(base_url=config.base_url, model=config.model, timeout=config.timeout)
    return OpenAICompatibleProvider(base_url=config.base_url, model=config.model,
                                    api_key=config.api_key or "", timeout=config.timeout)


_provider: LLMProvider | None = None


def get_provider() -> LLMProvider:
    """The configured provider, built once per process.

    Call `reset_provider()` after changing the environment (tests do) so the
    next call rebuilds it instead of reusing the old configuration.
    """
    global _provider
    if _provider is None:
        _provider = create_provider(load_config())
    # Unchanged object unless a trace run is active; then calls are also recorded.
    return agent_trace.wrap_provider(_provider)


def reset_provider() -> None:
    global _provider
    _provider = None
