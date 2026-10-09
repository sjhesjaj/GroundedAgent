"""M3 Phase 0: the 45 golden scenarios under m3-decision/1 against the stage6 fixture.

docs/v2/m3-policy-rag.md, "Scripted equivalence": the scripted scenarios of the
M2 golden fixture run again with AFTERSALES_DECISION_POLICY=m3. Their HTTP
payloads must equal the stage6 fixture's EXCLUDING trace.model_calls, which
carries offered_functions (now with search_knowledge_base). Model requests
differ by design (prompt, tools) and are not compared. Every scenario's own
assertions must still pass. Phase 0 has no m3 customer wording, so no
expected wording differences exist yet.

The golden runner itself (tests/test_aftersales_golden.py) is reused unchanged;
only the decision policy switch and an embedder-free knowledge base are set.
"""

from __future__ import annotations

import copy
import json
import os
import unittest
from unittest import mock

from aftersales_service import conversation as conversation_module
from aftersales_service import decision_policy as dp
from aftersales_service import knowledge_base as kb
from tests import test_aftersales_golden as golden


def without_model_calls(http: list[dict]) -> list[dict]:
    stripped = copy.deepcopy(http)
    for exchange in stripped:
        response = exchange["response"]
        if isinstance(response, dict) and isinstance(response.get("trace"), dict):
            response["trace"].pop("model_calls", None)
    return stripped


def record_under_m3() -> dict:
    with mock.patch.dict(os.environ, {dp.DECISION_POLICY_ENV: dp.POLICY_M3}), \
            mock.patch.object(conversation_module, "shared_knowledge_base",
                              return_value=kb.KnowledgeBase(kb.load_corpus())):
        return json.loads(golden.record_scenarios())


class M3ScriptedEquivalenceTests(unittest.TestCase):
    def test_golden_scenarios_under_m3_equal_stage6_except_model_calls(self):
        fixture = json.loads(golden.FIXTURE.read_text(encoding="utf-8"))["scenarios"]
        recorded = record_under_m3()["scenarios"]
        self.assertEqual([item["scenario"] for item in recorded], [item["scenario"] for item in fixture])
        self.assertEqual(len(recorded), 45)
        for stage6, m3 in zip(fixture, recorded):
            with self.subTest(scenario=m3["scenario"]):
                self.assertEqual(without_model_calls(m3["http"]), without_model_calls(stage6["http"]))
                # The model was asked the same number of times, with the m3 tools and prompt.
                self.assertEqual([len(requests) for requests in m3["provider_requests"]],
                                 [len(requests) for requests in stage6["provider_requests"]])
                for requests in m3["provider_requests"]:
                    for request in requests:
                        if request["tools"] is not None:
                            self.assertTrue(request["messages"][0]["content"].startswith(
                                dp.M3_SYSTEM_PROMPT))


if __name__ == "__main__":
    unittest.main()
