"""The business observation / Evidence contract (design §6, D4)."""

import unittest

from aftersales.business_tools import BUSINESS_HANDLERS, SOURCE_FRESHNESS
from aftersales.executor import execute_tool
from aftersales.registry import build_runtime_registry
from orchestration.contracts import (
    OBSERVATION_ID_KEY,
    BusinessEvidence,
    Evidence,
    FreshnessContract,
    SourceType,
)

from tests.v2_support import (
    ORDER_A_DELIVERED,
    SKU_ZERO,
    VALID_BUSINESS_CALLS,
    at,
    make_context,
    memory_connection,
)

V1_EVIDENCE_KEYS = {
    "content", "source_type", "source", "locator", "version", "observed_at",
    "authority", "confidence", "metadata",
}
V2_KEYS = {"record_updated_at", "state_version", "freshness_contract", "source_as_of"}


def valid_evidence(**overrides) -> BusinessEvidence:
    values = dict(
        content="SKU SKU-X 的可售库存为 1。",
        source_type=SourceType.BUSINESS,
        source="aftersales-demo-db",
        locator="inventory:SKU-X#available_qty",
        observed_at="2026-11-15T10:00:00+08:00",
        authority=100,
        metadata={OBSERVATION_ID_KEY: None},
        record_updated_at="2026-11-13T08:00:00+08:00",
        state_version=3,
        freshness_contract=FreshnessContract.AUTHORITATIVE_ONLINE,
    )
    values.update(overrides)
    return BusinessEvidence(**values)


class ContractTests(unittest.TestCase):
    def test_business_evidence_is_evidence_and_extends_its_dict(self):
        evidence = valid_evidence()
        self.assertIsInstance(evidence, Evidence)
        payload = evidence.to_dict()
        self.assertEqual(set(payload), V1_EVIDENCE_KEYS | V2_KEYS)
        self.assertEqual(payload["freshness_contract"], "authoritative_online")
        self.assertEqual(payload["source_type"], "business")
        self.assertIsNone(payload["source_as_of"])

    def test_v1_evidence_shape_is_unchanged(self):
        plain = Evidence(content="c", source_type=SourceType.DOCUMENT, source="s", authority=80)
        self.assertEqual(set(plain.to_dict()), V1_EVIDENCE_KEYS)

    def test_invalid_business_evidence_is_rejected(self):
        bad = {
            "not_business": dict(source_type=SourceType.SYSTEM),
            "no_locator": dict(locator=None),
            "no_observed_at": dict(observed_at=None),
            "naive_observed_at": dict(observed_at="2026-11-15T10:00:00"),
            "garbled_observed_at": dict(observed_at="yesterday"),
            "naive_record_updated_at": dict(record_updated_at="2026-11-13T08:00:00"),
            "version_is_bool": dict(state_version=True),
            "version_is_text": dict(state_version="3"),
            "version_is_zero": dict(state_version=0),
            "freshness_is_text": dict(freshness_contract="authoritative_online"),
            "online_with_as_of": dict(source_as_of="2026-11-15T09:00:00+08:00"),
            "snapshot_without_as_of": dict(freshness_contract=FreshnessContract.SNAPSHOT),
            "no_observation_slot": dict(metadata={}),
        }
        for name, overrides in bad.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    valid_evidence(**overrides)

    def test_record_updated_at_may_be_absent_and_snapshot_is_only_reserved(self):
        self.assertIsNone(valid_evidence(record_updated_at=None).record_updated_at)
        snapshot = valid_evidence(
            freshness_contract=FreshnessContract.SNAPSHOT,
            source_as_of="2026-11-15T09:00:00+08:00",
        )
        self.assertEqual(snapshot.source_as_of, "2026-11-15T09:00:00+08:00")
        # No Stage 4 source uses it.
        self.assertIs(SOURCE_FRESHNESS, FreshnessContract.AUTHORITATIVE_ONLINE)


class ObservationFieldTests(unittest.TestCase):
    """observed_at / record_updated_at / state_version answer different questions."""

    def setUp(self):
        self.connection = memory_connection()
        self.addCleanup(self.connection.close)

    def stored(self, table, key_column, key, column):
        return self.connection.execute(
            "SELECT " + column + " FROM " + table + " WHERE " + key_column + " = ?", (key,)
        ).fetchone()[0]

    def test_observed_at_is_the_clock_and_record_fields_come_from_the_record(self):
        """V2 semantics superseding V1 tests.test_system_provider.EvidenceMappingTests.test_observed_at_comes_from_the_record"""
        instant = at(2026, 11, 15, 10)
        context = make_context(self.connection, now=instant)
        result = BUSINESS_HANDLERS["get_inventory"](context, {"sku": SKU_ZERO})
        (evidence,) = result.evidence
        self.assertEqual(evidence.observed_at, instant.isoformat())
        self.assertEqual(
            evidence.record_updated_at, self.stored("inventory", "sku", SKU_ZERO, "updated_at")
        )
        self.assertEqual(evidence.state_version, self.stored("inventory", "sku", SKU_ZERO, "version"))
        # The record is older than the read: that alone says nothing about staleness.
        self.assertNotEqual(evidence.observed_at, evidence.record_updated_at)

    def test_each_record_contributes_its_own_version_and_update_time(self):
        context = make_context(self.connection)
        result = BUSINESS_HANDLERS["get_order"](context, {"order_id": ORDER_A_DELIVERED})
        order = [e for e in result.evidence if e.metadata["entity"] == "order"]
        items = [e for e in result.evidence if e.metadata["entity"] == "order_item"]
        self.assertTrue(order and items)
        self.assertEqual({e.state_version for e in order},
                         {self.stored("orders", "order_id", ORDER_A_DELIVERED, "version")})
        for item in items:
            record_id = item.metadata["record_id"]
            self.assertEqual(item.state_version,
                             self.stored("order_items", "order_item_id", record_id, "version"))
            self.assertEqual(item.record_updated_at,
                             self.stored("order_items", "order_item_id", record_id, "updated_at"))

    def test_every_business_observation_meets_the_contract(self):
        registry = build_runtime_registry()
        instant = at(2031, 3, 4, 5)
        context = make_context(self.connection, now=instant)
        for tool, arguments in VALID_BUSINESS_CALLS:
            result = execute_tool(registry, context, tool, arguments, observation_id="obs-" + tool)
            for evidence in result.evidence:
                with self.subTest(tool=tool, locator=evidence.locator):
                    self.assertIsInstance(evidence, BusinessEvidence)
                    self.assertEqual(evidence.observed_at, instant.isoformat())
                    self.assertIs(evidence.freshness_contract, FreshnessContract.AUTHORITATIVE_ONLINE)
                    self.assertIsNone(evidence.source_as_of)
                    self.assertEqual(evidence.metadata[OBSERVATION_ID_KEY], "obs-" + tool)
                    entity, rest = evidence.locator.split(":", 1)
                    record_id, field = rest.split("#", 1)
                    self.assertEqual(entity, evidence.metadata["entity"])
                    self.assertEqual(record_id, evidence.metadata["record_id"])
                    self.assertEqual(field, evidence.metadata["field"])


if __name__ == "__main__":
    unittest.main()
