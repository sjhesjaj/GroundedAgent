"""Real DeepSeek call through the LLMProvider interface.

Skipped unless RUN_LIVE_TESTS=1 and `.env` holds complete DeepSeek settings
(DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, DEEPSEEK_API_KEY). The unit suite disables
`.env` for everything else, so this file names the path explicitly when enabled.
"""

import json
import os
import unittest

import llm_provider

if os.environ.get("RUN_LIVE_TESTS") != "1":
    CONFIG, SKIP_REASON = None, "Real DeepSeek tests disabled; set RUN_LIVE_TESTS=1 to enable."
else:
    try:
        CONFIG = llm_provider.load_config("deepseek", environ={}, dotenv_path=llm_provider.DEFAULT_DOTENV)
        SKIP_REASON = None
    except llm_provider.LLMConfigError as exc:
        CONFIG, SKIP_REASON = None, f"DeepSeek not configured in .env: {exc}"


@unittest.skipIf(CONFIG is None, SKIP_REASON or "")
class DeepSeekLiveTests(unittest.TestCase):
    def setUp(self):
        self.provider = llm_provider.create_provider(CONFIG)

    def test_plain_chat_returns_text_and_usage(self):
        result = self.provider.chat(
            [{"role": "user", "content": "只回答两个字：你好"}], temperature=0, max_tokens=20)
        self.assertTrue(result.content.strip())
        self.assertIsNone(result.reasoning)  # thinking is pinned off in Stage 0
        self.assertGreater(result.prompt_tokens or 0, 0)
        self.assertGreater(result.completion_tokens or 0, 0)
        self.assertGreater(result.latency_seconds, 0)

    def test_schema_request_returns_parseable_json(self):
        schema = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}
        result = self.provider.chat(
            [{"role": "system", "content": "你是助手。"}, {"role": "user", "content": "1+1等于几？"}],
            response_format=schema, temperature=0, max_tokens=100)
        self.assertIn("answer", json.loads(result.content))
        self.assertEqual(len(result.prompt_adaptations), 1)


if __name__ == "__main__":
    unittest.main()
