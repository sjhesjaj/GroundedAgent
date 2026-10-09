"""The eval_m3 runner: drives the product Conversation case by case (Phase 4).

    load_suite(name)          frozen KB-DEV or the frozen Stage 6 DEV subset,
                              hash-checked against the Phase 2 manifests
    run_case(case, ...)       one case-run through AftersalesService under m3:
                              its own temporary data directory and database,
                              its own business clock, its own initial_state
                              (and action faults), a scripted customer that
                              sends the case's turns in order
    -> CaseRun (plain dict)   per turn: the product payload, the tool results
                              the product actually read, model calls with
                              token usage and latency, database state

The model sees only what a customer would type. Labels (routes, gold,
assertions, expected outcomes) never leave this process: the harness reads a
turn's text, the persona, the clock and the initial state, nothing else.
Scoring happens afterwards (scoring.py).

KB-HOLDOUT (Phase 5 part 2) is read only by load_kb_holdout: the sealed zip's
raw bytes are hash-checked against the seal receipt and the dataset manifest,
the inner JSON is read in memory and hash-checked, and no plaintext is written
anywhere. It is scored with the KB-DEV rules (KB_SUITES).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Callable, Iterator, Mapping
from unittest import mock

from aftersales.action_db import create_stage6_database
from aftersales.action_gateway import ActionGateway
from aftersales.action_policy import S6_RISK_POLICY
from aftersales.approval import STAGE6_TRUSTED_OPERATORS
from aftersales.clock import FixedClock
from aftersales.demo import DEMO_PERSONAS
from aftersales.ids import DeterministicIdProvider
from aftersales.policy_catalog import PublishedPolicyCatalog
from aftersales_service import conversation as conversation_module
from aftersales_service import decision_policy
from aftersales_service import persistence
from aftersales_service.conversation import TurnFailed
from aftersales_service.demo_store import DEMO_ID_NAMESPACE, DemoStore, KnowledgeReadSide
from aftersales_service.service import AftersalesService
from eval_v2.stage6_runtime import ActionFaultInjector
from eval_v2.stage6_state import harness_connection, apply_initial_state, read_audit, read_state

ROOT = Path(__file__).resolve().parents[1]
KB_DEV_PATH = ROOT / "eval_m3" / "datasets" / "kb-dev.json"
DATASET_MANIFEST_PATH = ROOT / "eval_m3" / "spec" / "dataset-manifest.json"
SUBSET_MANIFEST_PATH = ROOT / "eval_m3" / "spec" / "stage6-dev-subset.json"
STAGE6_DEV_PATH = ROOT / "eval" / "v2" / "stage6-dev.json"
SEALED_DIRECTORY = ROOT / "eval_m3" / "sealed"
KB_HOLDOUT_ZIP = SEALED_DIRECTORY / "kb-holdout.zip"
KB_HOLDOUT_SEAL = SEALED_DIRECTORY / "kb-holdout-seal.json"

SUITE_KB_DEV = "kb-dev"
SUITE_STAGE6 = "stage6-subset"
SUITE_KB_HOLDOUT = "kb-holdout"
SUITES = (SUITE_KB_DEV, SUITE_STAGE6, SUITE_KB_HOLDOUT)
KB_SUITES = (SUITE_KB_DEV, SUITE_KB_HOLDOUT)   # scored with the KB-DEV rules
BEIJING = timezone(timedelta(hours=8))
EVIDENCE_TEXT_CHARS = 800


class DatasetIntegrityError(RuntimeError):
    """A frozen input does not match its manifest; nothing is run."""


# --------------------------------------------------------------------------
# Frozen inputs
# --------------------------------------------------------------------------


def normalized_sha256(path: Path) -> str:
    """UTF-8 (BOM dropped), CRLF/CR -> LF, as the Phase 2 manifests define it."""
    text = path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_sha256(value: object) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EvalTurn:
    index: int                        # 1-based position in the case
    text: str                         # what the scripted customer types
    on_clarify: tuple[str, ...] | None = None   # Stage 6: sent only to answer these slots
    labels: Mapping[str, object] = field(default_factory=dict)   # scoring only


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    suite: str
    type: str
    virtual_now: datetime
    persona_id: str
    initial_state: Mapping[str, object] | None
    action_faults: tuple = ()
    operator_script: tuple = ()
    turns: tuple[EvalTurn, ...] = ()
    labels: Mapping[str, object] = field(default_factory=dict)   # scoring only


def _json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _require_unsealed(path: Path) -> None:
    resolved = Path(path).resolve()
    if resolved == SEALED_DIRECTORY.resolve() or SEALED_DIRECTORY.resolve() in resolved.parents:
        raise DatasetIntegrityError("the sealed holdout is opened only in Phase 5")


def _stage6_cases() -> dict[str, dict]:
    subset = _json(SUBSET_MANIFEST_PATH)
    wanted = subset["source"]["normalized_utf8_lf_sha256"]
    if normalized_sha256(STAGE6_DEV_PATH) != wanted:
        raise DatasetIntegrityError("eval/v2/stage6-dev.json differs from the frozen subset manifest")
    return {case["case_id"]: case for case in _json(STAGE6_DEV_PATH)}


def _subset_entries() -> dict[str, dict]:
    subset = _json(SUBSET_MANIFEST_PATH)
    manifest = _json(DATASET_MANIFEST_PATH)["stage6_dev_subset"]
    if normalized_sha256(SUBSET_MANIFEST_PATH) != manifest["normalized_utf8_lf_sha256"]:
        raise DatasetIntegrityError("the Stage 6 subset manifest differs from the dataset manifest")
    if len(subset["case_ids"]) != subset["selection_count"] or len(subset["cases"]) != subset["selection_count"]:
        raise DatasetIntegrityError("the Stage 6 subset manifest is incomplete")
    return {entry["case_id"]: entry for entry in subset["cases"]}


def _verified_initial_state(stage6: Mapping[str, dict], entries: Mapping[str, dict], case_id: str) -> dict:
    case = stage6[case_id]
    entry = entries.get(case_id)
    if entry is not None and canonical_sha256(case["initial_state"]) != entry["initial_state_canonical_json_sha256"]:
        raise DatasetIntegrityError(case_id + " initial_state differs from its frozen hash")
    return case["initial_state"]


def load_kb_dev(path: Path = KB_DEV_PATH) -> list[EvalCase]:
    _require_unsealed(path)
    manifest = next(item for item in _json(DATASET_MANIFEST_PATH)["datasets"] if item["name"] == "KB-DEV")
    if Path(path).resolve() != KB_DEV_PATH.resolve():
        raise DatasetIntegrityError("KB-DEV is read only from its frozen path")
    if normalized_sha256(path) != manifest["normalized_utf8_lf_sha256"]:
        raise DatasetIntegrityError("KB-DEV differs from the frozen dataset manifest")
    raw = _json(path)
    if len(raw) != manifest["case_count"]:
        raise DatasetIntegrityError("KB-DEV case count differs from the manifest")
    return _kb_cases(raw, SUITE_KB_DEV)


def load_kb_holdout() -> list[EvalCase]:
    """Phase 5 part 2 only: the sealed holdout, hash-checked, read in memory."""
    manifest = next(item for item in _json(DATASET_MANIFEST_PATH)["datasets"] if item["name"] == "KB-HOLDOUT")
    seal = _json(KB_HOLDOUT_SEAL)
    data = KB_HOLDOUT_ZIP.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != manifest["raw_sha256"] or digest != seal["sealed_artifact"]["sha256"]:
        raise DatasetIntegrityError("the sealed KB-HOLDOUT zip differs from its seal")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        inner = archive.read(seal["inner_dataset"]["filename"])
    if hashlib.sha256(inner).hexdigest() != seal["inner_dataset"]["raw_json_sha256"]:
        raise DatasetIntegrityError("the KB-HOLDOUT JSON differs from its seal")
    raw = json.loads(inner.decode("utf-8-sig"))
    if len(raw) != manifest["case_count"] or sum(len(item["turns"]) for item in raw) != manifest["turn_count"]:
        raise DatasetIntegrityError("KB-HOLDOUT case or turn count differs from the manifest")
    return _kb_cases(raw, SUITE_KB_HOLDOUT)


def _kb_cases(raw: list, suite: str) -> list[EvalCase]:
    stage6, entries = _stage6_cases(), _subset_entries()
    cases = []
    for item in raw:
        reference = item["initial_state_ref"]
        state = None
        if reference is not None:
            if reference.get("path") != "eval/v2/stage6-dev.json":
                raise DatasetIntegrityError(item["id"] + " initial_state_ref names another file")
            state = _verified_initial_state(stage6, entries, reference["case_id"])
            if state["trusted_context"]["persona_id"] != item["persona_id"]:
                raise DatasetIntegrityError(item["id"] + " persona differs from its initial_state")
        turns = tuple(EvalTurn(index=index, text=turn["question"],
                               labels={key: value for key, value in turn.items() if key != "question"})
                      for index, turn in enumerate(item["turns"], 1))
        cases.append(EvalCase(
            case_id=item["id"], suite=suite, type=item["type"],
            virtual_now=datetime.fromisoformat(item["virtual_now"]), persona_id=item["persona_id"],
            initial_state=state, action_faults=tuple((state or {}).get("action_faults", ())),
            operator_script=tuple(item["operator_script"]), turns=turns,
            labels={"injection_kind": item["injection_kind"]}))
    return cases


def load_stage6_subset() -> list[EvalCase]:
    stage6, entries = _stage6_cases(), _subset_entries()
    cases = []
    for case_id, entry in entries.items():
        case = stage6[case_id]
        if case["virtual_now"] != entry["virtual_now"]:
            raise DatasetIntegrityError(case_id + " virtual_now differs from the subset manifest")
        state = _verified_initial_state(stage6, entries, case_id)
        if state.get("faults"):
            raise DatasetIntegrityError(case_id + " has read faults; the product path cannot inject them")
        turns = tuple(EvalTurn(index=index, text=turn["text"],
                               on_clarify=tuple(turn["on_clarify"]) if "on_clarify" in turn else None)
                      for index, turn in enumerate(case["user_turns"], 1))
        cases.append(EvalCase(
            case_id=case_id, suite=SUITE_STAGE6, type=case["scenario"],
            virtual_now=datetime.fromisoformat(case["virtual_now"]),
            persona_id=state["trusted_context"]["persona_id"], initial_state=state,
            action_faults=tuple(state.get("action_faults", ())),
            operator_script=tuple(case["operator_script"]), turns=turns,
            labels={key: case[key] for key in ("archetype", "expected_capabilities", "expected_evidence",
                                               "expected_answerability", "expected_action",
                                               "expected_final_state")}))
    return cases


def load_suite(name: str) -> list[EvalCase]:
    if name == SUITE_KB_DEV:
        return load_kb_dev()
    if name == SUITE_STAGE6:
        return load_stage6_subset()
    if name == SUITE_KB_HOLDOUT:
        return load_kb_holdout()
    raise ValueError("unknown suite: " + str(name))


# --------------------------------------------------------------------------
# Model calls: usage, cache hits and latency per call
# --------------------------------------------------------------------------


_usage_capture = threading.local()


def _capturing_post(original):
    def post(url, *args, **kwargs):
        response = original(url, *args, **kwargs)
        sink = getattr(_usage_capture, "sink", None)
        if sink is not None and str(url).endswith("/chat/completions"):
            try:
                sink.update(response.json().get("usage") or {})
            except Exception:  # usage is diagnostic only; the provider reports its own errors
                pass
        return response
    return post


class RecordingProvider:
    """Forwards to a provider; records kind, usage (incl. DeepSeek cache hits) and latency."""

    def __init__(self, inner: object, *, role: str) -> None:
        # No timeout of its own: the product reads the transport timeout through `_inner`.
        self._inner = inner
        self.role = role
        self.name = getattr(inner, "name", None)
        self.model = getattr(inner, "model", None)
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):
        import llm_provider

        record = {"role": self.role, "kind": "decision" if kwargs.get("tools") else "generation",
                  "at": datetime.now(BEIJING).isoformat(timespec="seconds"),
                  "max_tokens": kwargs.get("max_tokens")}
        usage: dict = {}
        started = perf_counter()
        _usage_capture.sink = usage
        try:
            with mock.patch.object(llm_provider.requests, "post", _capturing_post(llm_provider.requests.post)):
                response = self._inner.chat(messages, **kwargs)
        except Exception as error:
            record.update({"error": type(error).__name__, "seconds": round(perf_counter() - started, 3)})
            self.calls.append(record)
            raise
        finally:
            _usage_capture.sink = None
        record.update({
            "seconds": round(perf_counter() - started, 3),
            "prompt_tokens": getattr(response, "prompt_tokens", None),
            "completion_tokens": getattr(response, "completion_tokens", None),
            "cache_hit_tokens": usage.get("prompt_cache_hit_tokens"),
            "cache_miss_tokens": usage.get("prompt_cache_miss_tokens"),
            "finish_reason": getattr(response, "finish_reason", None),
        })
        self.calls.append(record)
        return response


# --------------------------------------------------------------------------
# One case's product environment
# --------------------------------------------------------------------------


class CaseStore(DemoStore):
    """The product DemoStore at the case's business time, with the case's action faults.

    Everything else - database file, read side, capabilities, operator - is the
    product's own. Faults go through the ActionGateway's existing hooks, exactly
    as the frozen Stage 6 harness wires them.
    """

    def __init__(self, db_path: Path, *, virtual_now: datetime, action_faults: tuple) -> None:
        super().__init__(db_path)
        self.business_time = virtual_now
        self.clock = FixedClock(virtual_now)
        catalog = PublishedPolicyCatalog()
        hooks: dict = {}
        self.injector = None
        if action_faults:
            self.injector = ActionFaultInjector(list(action_faults))
            catalog = self.injector.wrap_catalog(catalog)
            hooks = {"fault_hooks": self.injector, "guard_read_hook": self.injector,
                     "decision_observer": self.injector.observe}
        self.gateway = ActionGateway(
            self.db_path, clock=self.clock, id_provider=DeterministicIdProvider(DEMO_ID_NAMESPACE),
            catalog=catalog, capabilities=self.capabilities, risk_policy=S6_RISK_POLICY,
            operators=STAGE6_TRUSTED_OPERATORS, **hooks)


def _evidence_view(evidence) -> dict:
    metadata = {key: value for key, value in dict(evidence.metadata).items()
                if isinstance(value, (str, int, float, bool)) or value is None}
    return {"source": evidence.source, "locator": evidence.locator, "version": evidence.version,
            "metadata": metadata, "content": evidence.content[:EVIDENCE_TEXT_CHARS]}


class ReadRecorder:
    """Keeps every tool result the product read, by turn, for scoring and the judge."""

    def __init__(self) -> None:
        self.turn: int | None = None
        self.reads: dict[int, list[dict]] = {}

    def read_side_class(self):
        recorder = self

        class RecordingReadSide(KnowledgeReadSide):
            def execute(self, tool_name, arguments, *, observation_id):
                result = super().execute(tool_name, arguments, observation_id=observation_id)
                recorder.reads.setdefault(recorder.turn, []).append({
                    "observation_id": observation_id, "tool_name": tool_name,
                    "arguments": dict(arguments), "status": result.status.value,
                    "error_code": result.error_code,
                    "evidence": [_evidence_view(item) for item in result.evidence]})
                return result

        return RecordingReadSide


def _read_database(db_path: Path) -> tuple[dict, list]:
    connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return read_state(connection), list(read_audit(connection))
    finally:
        connection.close()


@contextmanager
def case_environment(case: EvalCase, recorder: ReadRecorder, *,
                     knowledge_base_factory: Callable[[], object] | None = None) -> Iterator[None]:
    """Patches that exist only while this case runs: seed, clock/faults, read recording, m3."""

    def seed(db_path: Path) -> None:
        if Path(db_path).exists():
            raise FileExistsError("a case database is seeded exactly once")
        create_stage6_database(db_path)
        if case.initial_state is not None:
            connection = harness_connection(db_path)
            try:
                apply_initial_state(connection, case.initial_state)
            finally:
                connection.close()

    def store(db_path: Path) -> CaseStore:
        return CaseStore(db_path, virtual_now=case.virtual_now, action_faults=case.action_faults)

    patches = [
        mock.patch.object(persistence, "seed_demo_database", seed),
        mock.patch.object(persistence, "DemoStore", store),
        mock.patch.object(conversation_module, "KnowledgeReadSide", recorder.read_side_class()),
        mock.patch.dict(os.environ, {decision_policy.DECISION_POLICY_ENV: decision_policy.POLICY_M3}),
    ]
    if knowledge_base_factory is not None:
        patches.append(mock.patch.object(conversation_module, "shared_knowledge_base", knowledge_base_factory))
    for patch in patches:
        patch.start()
    try:
        yield
    finally:
        for patch in reversed(patches):
            patch.stop()


def _clarification_answers(turn: EvalTurn, previous: dict | None) -> bool:
    """Stage 5/6 rule: a scripted clarification answer is sent only if it covers every requested slot."""
    if previous is None or previous.get("status") != "NEEDS_CLARIFICATION":
        return False
    requested = set((previous.get("clarification") or {}).get("slots") or ())
    return bool(requested) and requested <= set(turn.on_clarify or ())


def run_case(case: EvalCase, provider: RecordingProvider, *, data_root: Path,
             knowledge_base_factory: Callable[[], object] | None = None) -> dict:
    """One case-run. Infrastructure errors are recorded, never retried here."""
    recorder = ReadRecorder()
    persona = DEMO_PERSONAS[case.persona_id]
    data_dir = Path(data_root) / case.case_id
    turns: list[dict] = []
    run = {"case_id": case.case_id, "suite": case.suite, "type": case.type,
           "virtual_now": case.virtual_now.isoformat(), "persona_id": case.persona_id,
           "customer_id": persona.customer_id, "turns": turns, "operator_events": [],
           "infra_error": None}
    started = perf_counter()
    with case_environment(case, recorder, knowledge_base_factory=knowledge_base_factory):
        service = AftersalesService(lambda: provider, data_dir=data_dir)
        try:
            session_id = service.create_session(case.persona_id)["session_id"]
            db_path = service.store.db_path
            run["request_id"] = conversation_module.REQUEST_ID_PREFIX + session_id
            baseline, _ = _read_database(db_path)
            run["baseline_state"] = baseline
            previous = None
            for turn in case.turns:
                if turn.on_clarify is not None and not _clarification_answers(turn, previous):
                    turns.append({"turn": turn.index, "question": turn.text, "delivered": False})
                    continue
                recorder.turn = turn.index
                first_call = len(provider.calls)
                turn_started = perf_counter()
                record = {"turn": turn.index, "question": turn.text, "delivered": True}
                try:
                    payload = service.submit(session_id, turn.text)
                except TurnFailed as error:
                    cause = error.__cause__
                    record.update({"error": error.code, "error_trace": error.trace,
                                   "error_cause": None if cause is None else
                                   (type(cause).__name__ + ": " + str(cause))[:300]})
                    run["infra_error"] = error.code
                except Exception as error:  # a harness or product defect, kept for the report
                    record.update({"error": type(error).__name__})
                    run["infra_error"] = type(error).__name__
                else:
                    record.update({key: payload.get(key) for key in
                                   ("status", "reply", "clarification", "citations", "action", "trace",
                                    "pending_action_id")})
                    previous = payload
                record["seconds"] = round(perf_counter() - turn_started, 3)
                record["provider_calls"] = provider.calls[first_call:]
                record["reads"] = recorder.reads.get(turn.index, [])
                state, _ = _read_database(db_path)
                record["state_after"] = state
                turns.append(record)
                if run["infra_error"] is not None:
                    break
            for event in case.operator_script:
                run["operator_events"].append(_operator_event(service, session_id, event))
            final, audit = _read_database(db_path)
            run["final_state"], run["audit"] = final, audit
            injector = getattr(service.store, "injector", None)
            run["action_fault_records"] = [] if injector is None else [item.to_dict() for item in injector.records]
        finally:
            service.close()
    run["seconds"] = round(perf_counter() - started, 3)
    return run


def _operator_event(service: AftersalesService, session_id: str, event: Mapping) -> dict:
    """approve / reject the session's latest pending action; anything else is not supported here."""
    op = event.get("op")
    if op not in ("approve", "reject"):
        return {"op": op, "error": "unsupported_operator_op"}
    pending = service.session_view(session_id).get("pending_action_id")
    if pending is None:
        return {"op": op, "error": "no_pending_action"}
    try:
        payload = service.decide(session_id, pending, "APPROVE" if op == "approve" else "REJECT")
    except Exception as error:
        return {"op": op, "error": getattr(error, "code", type(error).__name__)}
    return {"op": op, "pending_action_id": pending, "reply": payload.get("reply"),
            "action": payload.get("action")}


def new_run_directory(prefix: str = "eval-m3-") -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True)
