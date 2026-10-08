"""Byte-for-byte M2 equivalence against the pre-refactor product.

The fixture was recorded before any M2 product change, in a clean worktree of
main 42e96de (`git worktree add ..\\ka-golden 42e96de`, this file copied in,
`python -X utf8 -m tests.test_aftersales_golden --record`; 45 scenarios,
sha256 41d761955e2e5e970ad4e83383bf8346ec298f1fcad5addbfdb703eb71f5d214).
This runner executes the original scripted tests unchanged, fixes session
UUIDs at their source (only `aftersales_service.service.uuid`, never the
process-global uuid4) and records every model request and HTTP payload
(including error responses). No timestamps, ids, model calls or response
fields are normalized away.
"""

from __future__ import annotations

import hashlib
import io
import json
import types
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from aftersales_service import service as service_module
from tests import test_aftersales_grounding, test_aftersales_service


FIXTURE = Path(__file__).parent / "fixtures" / "m2" / "aftersales-main-golden.json"
BASELINE_REVISION = "42e96de"


def scripted_cases():
    """All scenario test methods, excluding non-scripted boundary/unit tests."""
    loader = unittest.TestLoader()
    for module in (test_aftersales_service, test_aftersales_grounding):
        for name in sorted(vars(module)):
            cls = getattr(module, name)
            if (isinstance(cls, type) and issubclass(cls, test_aftersales_service.ProductTestCase)
                    and cls.__module__ == module.__name__):
                yield from loader.loadTestsFromTestCase(cls)


def record_scenarios() -> bytes:
    records = []
    original_init = test_aftersales_service.ScriptedProvider.__init__
    original_request = TestClient.request
    for case in scripted_cases():
        providers = []
        http = []
        counter = 0

        def session_uuid():
            nonlocal counter
            counter += 1
            digest = hashlib.sha256((case.id() + ":" + str(counter)).encode()).digest()
            return uuid.UUID(bytes=digest[:16])

        def provider_init(provider):
            original_init(provider)
            providers.append(provider)

        def request(client, method, url, *args, **kwargs):
            response = original_request(client, method, url, *args, **kwargs)
            http.append({"method": method, "path": str(url),
                         "request": kwargs.get("json"), "status": response.status_code,
                         "response": response.json()})
            return response

        with ExitStack() as stack:
            # Replace this module's uuid reference, never the process-global uuid module.
            stack.enter_context(mock.patch.object(service_module, "uuid",
                                                   types.SimpleNamespace(uuid4=session_uuid)))
            stack.enter_context(mock.patch.object(test_aftersales_service.ScriptedProvider,
                                                   "__init__", provider_init))
            stack.enter_context(mock.patch.object(TestClient, "request", request))
            result = unittest.TextTestRunner(stream=io.StringIO()).run(case)
            if not result.wasSuccessful():
                raise AssertionError(case.id() + " failed:\n" + "\n".join(
                    detail for _, detail in result.failures + result.errors))
        records.append({"scenario": case.id(), "http": http,
                        "provider_requests": [provider.requests for provider in providers]})
    payload = {"schema": 1, "baseline_revision": BASELINE_REVISION, "scenarios": records}
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True,
                       allow_nan=False) + "\n").encode("utf-8")


class GoldenEquivalenceTests(unittest.TestCase):
    def test_all_original_scripted_scenarios_match_main_byte_for_byte(self):
        self.assertEqual(record_scenarios(), FIXTURE.read_bytes())


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["--record"]:
        if FIXTURE.exists():
            raise SystemExit("Refusing to overwrite the pre-refactor golden fixture")
        data = record_scenarios()
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_bytes(data)
        print(str(len(json.loads(data)["scenarios"])) + " golden scenarios recorded")
    else:
        unittest.main()
