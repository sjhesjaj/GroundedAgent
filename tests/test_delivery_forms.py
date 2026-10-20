"""M10 rework 3, package B: one cleaning order, checked where the user reads.

The point is the *combination*, not the helpers. `<think>` handling passed on
its own. Envelope extraction passed on its own. Put the shell around the
envelope and the whole `{"answer": ...}` string went to the user and into the
database, because the code tried JSON once, failed, stripped the shell and
returned - never looking at the envelope it had just uncovered.

Whitespace is the same kind of bug one step later: the plain path stripped in
its own branch, the stream handed the padded text straight out, and the API
layer's own `.strip()` before persisting made the two look equal in storage
while the deltas the reader actually saw differed.

So every case here is asserted at three points - the plain return value, the
**raw** delta join, and the stored body - and the delta join is never stripped
before comparison.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import rag
from rag import Chunk, extract_answer_text
from storage import SQLiteStorage

CLIENT_A = "client-a"

LEAVE_CHUNK = Chunk(
    text="## 请假制度\n正式员工入职满一年后，每年享有 5 天带薪年假。",
    source="sample_company_rules.md",
    index=1,
)
BODY = "正式员工每年享有 5 天带薪年假。[来源1]"


def envelope(text: str) -> str:
    return json.dumps({"answer": text}, ensure_ascii=False)


class ExtractAnswerTextTests(unittest.TestCase):
    """The cleaning order, as a pure function."""

    def test_plain_text_is_returned_as_is(self):
        self.assertEqual(extract_answer_text(BODY), BODY)

    def test_a_bare_envelope_is_unwrapped(self):
        self.assertEqual(extract_answer_text(envelope(BODY)), BODY)

    def test_a_think_shell_around_plain_text_is_removed(self):
        self.assertEqual(extract_answer_text(f"<think>检查资料</think>{BODY}"), BODY)

    def test_a_think_shell_around_an_envelope_is_removed_then_unwrapped(self):
        """The regression: one pass was not enough.

        Stripping the shell reveals an envelope, and something has to look
        again after it is revealed.
        """
        content = f"<think>检查资料</think>{envelope(BODY)}"
        self.assertEqual(extract_answer_text(content), BODY)
        self.assertNotIn('"answer"', extract_answer_text(content))

    def test_padding_inside_the_answer_field_is_normalised(self):
        self.assertEqual(extract_answer_text(envelope(f" \n{BODY}\n ")), BODY)

    def test_an_unterminated_think_shell_is_still_removed(self):
        self.assertEqual(extract_answer_text(f"<think>思考中{BODY}"), "")

    def test_an_envelope_whose_answer_mentions_think_survives_intact(self):
        """Why the raw text is tried as JSON *first*.

        If the shell were stripped before the first parse, an answer that
        legitimately contains the characters `<think>` would be mangled.
        """
        text = "文档里写着 <think> 这个标签。[来源1]"
        self.assertEqual(extract_answer_text(envelope(text)), text)


class Reply:
    """A non-streaming reply carrying `text` verbatim."""

    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"message": {"content": self.text}}


class Stream:
    """A streaming reply carrying `text` verbatim, split into chunks."""

    def __init__(self, text: str, chunk_size: int = 3):
        self.lines = [
            json.dumps({"message": {"content": text[i : i + chunk_size]}})
            for i in range(0, len(text), chunk_size)
        ]
        self.lines.append(json.dumps({"message": {"content": ""}, "done": True}))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self, decode_unicode=False):
        assert decode_unicode
        return iter(self.lines)


class DeliveryFormApiTests(unittest.TestCase):
    """Each accepted form, through `/api/chat` and `/api/chat/stream`."""

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.original_storage = api.storage
        api.storage = SQLiteStorage(Path(self.temp_directory.name) / "test.db")
        with api.state_lock:
            api.chunks.clear()
            api.chunks.append(LEAVE_CHUNK)
            api.conversation_locks.clear()
        self.client = TestClient(api.create_app(v1_routes=True))
        patch(
            "orchestration.document_adapter.retrieve_fast",
            side_effect=lambda question, chunks, top_k=4, trace=None: (
                [(chunks[0], 3.5)] if chunks else []
            ),
        ).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        with api.state_lock:
            api.chunks.clear()
            api.conversation_locks.clear()
        api.storage = self.original_storage
        self.temp_directory.cleanup()

    QUESTION = "年假有多少天"

    def payload(self, session_id):
        return {
            "question": self.QUESTION,
            "session_id": session_id,
            "client_id": CLIENT_A,
            "mode": "orchestrated",
        }

    @staticmethod
    def _sse(response):
        deltas, terminal, event = [], None, None
        for raw in response.text.splitlines():
            line = raw.strip()
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
                if event in {"done", "error"}:
                    terminal = event
            elif line.startswith("data:") and event == "delta":
                deltas.append(json.loads(line[5:].strip()).get("content", ""))
        return "".join(deltas), terminal

    def plain(self, session_id, scripted):
        with patch.object(
            rag.requests, "post", side_effect=[Reply(t) for t in scripted]
        ) as post:
            response = self.client.post("/api/chat", json=self.payload(session_id))
        return response.json()["answer"], post.call_count

    # -- forms the stream can carry: a well-formed envelope ------------------

    ENVELOPE_FORMS = (
        ("bare envelope", envelope(BODY)),
        ("answer field padded with spaces and newlines", envelope(f" \n{BODY}\n ")),
    )

    def test_both_interfaces_agree_on_every_envelope_form(self):
        for index, (label, content) in enumerate(self.ENVELOPE_FORMS):
            with self.subTest(form=label):
                plain_session, stream_session = f"f-p-{index}", f"f-s-{index}"
                body, plain_calls = self.plain(plain_session, [content])
                with patch.object(
                    rag.requests, "post", side_effect=[Stream(content)]
                ) as stream_post:
                    streamed = self.client.post(
                        "/api/chat/stream", json=self.payload(stream_session)
                    )
                shown, terminal = self._sse(streamed)

                self.assertEqual(body, BODY)
                # Raw delta join, not stripped: the difference this catches is
                # exactly a leading newline that `.strip()` would have hidden.
                self.assertEqual(shown, BODY)
                self.assertEqual(terminal, "done")
                self.assertNotIn('"answer"', shown)
                self.assertEqual(plain_calls, 1)
                self.assertEqual(stream_post.call_count, 1)
                self.assertEqual(
                    api.storage.get_messages(plain_session, CLIENT_A)[-1]["content"], BODY
                )
                self.assertEqual(
                    api.storage.get_messages(stream_session, CLIENT_A)[-1]["content"], BODY
                )

    # -- forms that reach delivery through the plain-text fallback -----------

    FALLBACK_FORMS = (
        ("plain text", BODY),
        ("think shell around plain text", f"<think>检查资料</think>{BODY}"),
        ("think shell around an envelope", f"<think>检查资料</think>{envelope(BODY)}"),
        ("envelope from the fallback call", envelope(BODY)),
    )

    def test_the_fallback_never_leaks_an_envelope_or_a_shell(self):
        """First reply is unparseable, so the plain-text fallback runs.

        Whatever shape that second reply takes, the delivered body is the
        answer - never the envelope, never the shell.
        """
        for index, (label, second) in enumerate(self.FALLBACK_FORMS):
            with self.subTest(form=label):
                session = f"fb-{index}"
                body, calls = self.plain(session, ["not json at all", second])
                self.assertEqual(body, BODY)
                self.assertNotIn('"answer"', body)
                self.assertNotIn("<think>", body)
                # The fallback already spent the second call; nothing may make
                # a third one.
                self.assertEqual(calls, 2)
                self.assertEqual(
                    api.storage.get_messages(session, CLIENT_A)[-1]["content"], BODY
                )

    def test_the_stream_keeps_its_format_error_contract(self):
        """A shell or bare prose in the *stream* is still a format error.

        This asymmetry is deliberate and pre-existing: the plain path may fall
        back to a second generation, the stream may not silently repair a
        malformed envelope. The contract being kept is that it errors and
        stores nothing - not that it quietly delivers something.
        """
        for index, content in enumerate((
            BODY,
            f"<think>检查资料</think>{envelope(BODY)}",
        )):
            with self.subTest(content=content[:20]):
                session = f"fe-{index}"
                with patch.object(rag.requests, "post", side_effect=[Stream(content)]):
                    streamed = self.client.post(
                        "/api/chat/stream", json=self.payload(session)
                    )
                shown, terminal = self._sse(streamed)
                self.assertEqual(terminal, "error")
                self.assertNotIn('"answer"', shown)
                self.assertEqual(api.storage.get_messages(session, CLIENT_A), [])

    def test_a_transport_failure_still_errors_and_stores_nothing(self):
        """Retained regression: transport errors are not delivery verdicts."""

        def explode(*_args, **_kwargs):
            raise rag.requests.RequestException("connection reset")

        with patch.object(rag.requests, "post", side_effect=explode):
            streamed = self.client.post("/api/chat/stream", json=self.payload("fe-t"))
        _shown, terminal = self._sse(streamed)
        self.assertEqual(terminal, "error")
        self.assertEqual(api.storage.get_messages("fe-t", CLIENT_A), [])


if __name__ == "__main__":
    unittest.main()
