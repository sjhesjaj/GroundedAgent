"""Structured policy params, effective window, and category scope (Stage 4.2)."""

import unittest
from datetime import datetime, timedelta, timezone

from aftersales.policy import (
    WINDOW_PARAM_KEYS,
    CountingRule,
    PolicyRuleType,
    StartEvent,
    WindowParams,
    is_policy_in_effect,
    parse_utc_offset,
    parse_window_params,
    policy_applies_to_category,
    policy_ref,
    window_params,
)

from tests.v2_support import WINDOW_PARAMS_7D, at, window_policy


def params(**overrides):
    values = dict(WINDOW_PARAMS_7D)
    values.update(overrides)
    return values


class WindowParamsSchemaTests(unittest.TestCase):
    def test_valid_params_parse_to_typed_values(self):
        parsed = parse_window_params(WINDOW_PARAMS_7D)
        self.assertEqual(
            parsed,
            WindowParams(
                window_days=7,
                start_event=StartEvent.DELIVERED,
                counting_rule=CountingRule.NATURAL_DAYS_FROM_NEXT_DAY,
                utc_offset="+08:00",
            ),
        )
        self.assertEqual(parsed.tzinfo.utcoffset(None), timedelta(hours=8))
        self.assertEqual(set(WINDOW_PARAM_KEYS), set(WINDOW_PARAMS_7D))

    def test_the_closed_vocabularies(self):
        self.assertEqual({m.value for m in StartEvent}, {"delivered"})
        self.assertEqual({m.value for m in CountingRule}, {"natural_days_from_next_day"})

    def test_invalid_params_fail_loudly(self):
        bad = {
            "not_mapping": [("window_days", 7)],
            "missing_window_days": {k: v for k, v in WINDOW_PARAMS_7D.items() if k != "window_days"},
            "missing_counting_rule": {k: v for k, v in WINDOW_PARAMS_7D.items() if k != "counting_rule"},
            "missing_utc_offset": {k: v for k, v in WINDOW_PARAMS_7D.items() if k != "utc_offset"},
            "missing_start_event": {k: v for k, v in WINDOW_PARAMS_7D.items() if k != "start_event"},
            "misspelled_key": {**params(), "windowDays": 7},
            "extra_key": params(note="签收次日起算"),
            "non_string_key": {**params(), 1: "x"},
            "days_zero": params(window_days=0),
            "days_negative": params(window_days=-7),
            "days_bool": params(window_days=True),
            "days_text": params(window_days="7"),
            "days_float": params(window_days=7.0),
            "start_shipped": params(start_event="shipped"),
            "start_lookalike": params(start_event="Delivered"),
            "start_chinese": params(start_event="签收"),
            "counting_free_text": params(counting_rule="签收次日起算7个自然日"),
            "counting_lookalike": params(counting_rule="natural_days"),
            "offset_name": params(utc_offset="Asia/Shanghai"),
            "offset_compact": params(utc_offset="+0800"),
            "offset_out_of_range": params(utc_offset="+15:00"),
            "offset_bad_minutes": params(utc_offset="+08:60"),
            "offset_not_string": params(utc_offset=8),
        }
        for name, value in bad.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    parse_window_params(value)

    def test_utc_offsets(self):
        self.assertEqual(parse_utc_offset("x", "-05:30").utcoffset(None), -timedelta(hours=5, minutes=30))
        self.assertEqual(parse_utc_offset("x", "+00:00").utcoffset(None), timedelta(0))
        self.assertEqual(parse_utc_offset("x", "+14:00").utcoffset(None), timedelta(hours=14))


class PolicyRecordParamsTests(unittest.TestCase):
    def test_window_rules_validate_params_at_construction(self):
        for rule_type in (PolicyRuleType.RETURN_WINDOW, PolicyRuleType.EXCHANGE_WINDOW):
            with self.subTest(rule_type=rule_type.value):
                self.assertEqual(window_params(window_policy(rule_type=rule_type)).window_days, 7)
                with self.assertRaises(ValueError):
                    window_policy(rule_type=rule_type, params={"window_days": 7, "start_event": "delivered"})
                with self.assertRaises(ValueError):
                    window_policy(rule_type=rule_type, params=params(window_days="七"))

    def test_other_rule_types_are_not_window_rules(self):
        record = window_policy(rule_type=PolicyRuleType.NON_RETURNABLE, params={}, scope=("生鲜",))
        with self.assertRaises(ValueError):
            window_params(record)

    def test_params_are_copied_and_read_only(self):
        source = params()
        record = window_policy(params=source)
        source["window_days"] = 30
        self.assertEqual(record.params["window_days"], 7)
        with self.assertRaises(TypeError):
            record.params["window_days"] = 30  # type: ignore[index]
        self.assertEqual(window_params(record).window_days, 7)

    def test_policy_ref_is_stable_and_names_version_and_build(self):
        record = window_policy()
        self.assertEqual(policy_ref(record), "policy:P-RETURN-7D@1#build-1")
        self.assertEqual(policy_ref(record), policy_ref(window_policy()))
        self.assertNotEqual(policy_ref(record), policy_ref(window_policy(version="2")))
        self.assertNotEqual(policy_ref(record), policy_ref(window_policy(build_id="build-2")))


class EffectiveWindowTests(unittest.TestCase):
    """[effective_from, effective_to): from inclusive, to exclusive."""

    def setUp(self):
        self.promo = window_policy(
            policy_id="P-RETURN-15D-PROMO",
            params=params(window_days=15),
            effective_from="2026-11-01T00:00:00+08:00",
            effective_to="2026-12-01T00:00:00+08:00",
        )

    def test_from_is_inclusive(self):
        self.assertFalse(is_policy_in_effect(self.promo, at(2026, 10, 31, 23, 59)))
        self.assertTrue(is_policy_in_effect(self.promo, at(2026, 11, 1, 0, 0)))

    def test_to_is_exclusive(self):
        self.assertTrue(is_policy_in_effect(self.promo, at(2026, 11, 30, 23, 59)))
        self.assertFalse(is_policy_in_effect(self.promo, at(2026, 12, 1, 0, 0)))
        self.assertFalse(is_policy_in_effect(self.promo, at(2031, 1, 1)))

    def test_boundary_instants_compare_as_absolute_time(self):
        # 2026-11-30T16:00Z is 2026-12-01T00:00+08:00: already out of effect.
        self.assertFalse(
            is_policy_in_effect(self.promo, datetime(2026, 11, 30, 16, 0, tzinfo=timezone.utc))
        )
        self.assertTrue(
            is_policy_in_effect(self.promo, datetime(2026, 11, 30, 15, 59, tzinfo=timezone.utc))
        )
        # 2026-10-31T16:00Z is 2026-11-01T00:00+08:00: already in effect.
        self.assertTrue(
            is_policy_in_effect(self.promo, datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc))
        )

    def test_successive_versions_never_overlap(self):
        before = window_policy(effective_to="2026-11-01T00:00:00+08:00")
        for instant in (at(2026, 10, 31, 23, 59), at(2026, 11, 1), at(2026, 11, 30, 23, 59),
                        at(2026, 12, 1)):
            with self.subTest(instant=instant.isoformat()):
                self.assertLessEqual(
                    is_policy_in_effect(before, instant) + is_policy_in_effect(self.promo, instant), 1
                )

    def test_open_ended_rule(self):
        standard = window_policy()
        self.assertFalse(is_policy_in_effect(standard, at(2025, 12, 31, 23, 59)))
        self.assertTrue(is_policy_in_effect(standard, at(2026, 1, 1)))
        self.assertTrue(is_policy_in_effect(standard, at(2031, 6, 1)))

    def test_as_of_must_be_aware(self):
        for bad in (datetime(2026, 11, 15), "2026-11-15T10:00:00+08:00", None):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    is_policy_in_effect(self.promo, bad)


class CategoryScopeTests(unittest.TestCase):
    def test_empty_scope_applies_to_every_category(self):
        record = window_policy()
        for category in ("服装", "贴身衣物", "数码"):
            self.assertTrue(policy_applies_to_category(record, category))

    def test_scope_is_exact_membership(self):
        record = window_policy(scope=("服装", "数码"))
        self.assertTrue(policy_applies_to_category(record, "服装"))
        self.assertTrue(policy_applies_to_category(record, "数码"))
        for category in ("贴身衣物", "服", "服装 ", "男装服装", "数码产品"):
            with self.subTest(category=category):
                self.assertFalse(policy_applies_to_category(record, category))

    def test_category_must_be_text(self):
        for bad in ("", "  ", None, 1):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    policy_applies_to_category(window_policy(), bad)


if __name__ == "__main__":
    unittest.main()
