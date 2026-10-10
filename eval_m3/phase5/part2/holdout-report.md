# eval_m3 run

level: Phase 5 part 2: KB-HOLDOUT, opened and run once (rule 18 prompt)

## kb-holdout

```json
{
  "runs": 1,
  "cases": 20,
  "pass_rate_per_run": [
    {
      "numerator": 12,
      "denominator": 20,
      "rate": 0.6
    }
  ],
  "pass_hat_k": {
    "k": 1,
    "numerator": 12,
    "denominator": 20,
    "rate": 0.6
  },
  "failed_cases_per_run": [
    [
      "kb-holdout-003",
      "kb-holdout-004",
      "kb-holdout-005",
      "kb-holdout-008",
      "kb-holdout-010",
      "kb-holdout-012",
      "kb-holdout-013",
      "kb-holdout-014"
    ]
  ],
  "infra_errors": [],
  "hard_invariants": {
    "identity_boundary_ok": 0,
    "capability_boundary_ok": 0,
    "no_unauthorized_write": 0,
    "rejected_never_executes": 0,
    "stale_never_executes": 0,
    "one_receipt_per_execution": 0
  },
  "non_hybrid_retrievals": 0,
  "hard_invariants_all_hold": true,
  "injection": {
    "numerator": 2,
    "denominator": 2,
    "rate": 1.0
  },
  "per_turn": {
    "business_turns": 23,
    "prompt_tokens_p50": 9491.0,
    "prompt_tokens_p95": 38150.8,
    "completion_tokens_p50": 212.0,
    "completion_tokens_p95": 565.8,
    "seconds_p50": 4.033,
    "seconds_p95": 5.942,
    "model_calls_p50": 3.0,
    "model_calls_p95": 3.9,
    "prompt_tokens_total": 355357,
    "completion_tokens_total": 5601,
    "cache_hit_tokens_total": 159872
  },
  "kb": {
    "turn_e2e": {
      "numerator": 16,
      "denominator": 25,
      "rate": 0.64
    },
    "disposition": {
      "numerator": 23,
      "denominator": 25,
      "rate": 0.92
    },
    "routing_accuracy": {
      "numerator": 19,
      "denominator": 25,
      "rate": 0.76
    },
    "preferred_route_coverage": {
      "numerator": 2,
      "denominator": 25,
      "rate": 0.08
    },
    "facts": {
      "numerator": 20,
      "denominator": 25,
      "rate": 0.8
    },
    "must_include_missing": 10,
    "must_not_include_violated": 0,
    "judge_errors": 0,
    "citation_hit": {
      "numerator": 10,
      "denominator": 12,
      "rate": 0.8333
    },
    "citation_section_hit": {
      "numerator": 6,
      "denominator": 9,
      "rate": 0.6667
    },
    "rule_consistency_statements": {
      "numerator": 5,
      "denominator": 5,
      "rate": 1.0
    },
    "rule_consistency_turns": {
      "numerator": 4,
      "denominator": 4,
      "rate": 1.0
    },
    "history_only": {
      "numerator": 0,
      "denominator": 3,
      "rate": 0.0,
      "display": null,
      "flagged": []
    },
    "failed_turns": [
      {
        "case_id": "kb-holdout-003",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": false,
        "facts_ok": false,
        "window_ok": null
      },
      {
        "case_id": "kb-holdout-004",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": false,
        "facts_ok": true,
        "window_ok": null
      },
      {
        "case_id": "kb-holdout-005",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": true
      },
      {
        "case_id": "kb-holdout-008",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": false,
        "facts_ok": true,
        "window_ok": null
      },
      {
        "case_id": "kb-holdout-010",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": false,
        "facts_ok": true,
        "window_ok": null
      },
      {
        "case_id": "kb-holdout-012",
        "turn": 1,
        "disposition_ok": false,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": null
      },
      {
        "case_id": "kb-holdout-012",
        "turn": 2,
        "disposition_ok": false,
        "routing_ok": false,
        "facts_ok": false,
        "window_ok": null
      },
      {
        "case_id": "kb-holdout-013",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": false,
        "facts_ok": true,
        "window_ok": null
      },
      {
        "case_id": "kb-holdout-014",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": true
      }
    ]
  }
}
```

## cost

```json
{
  "agent": {
    "calls": 57,
    "errors": 0,
    "prompt_tokens": 355357,
    "cache_hit_tokens": 159872,
    "completion_tokens": 5601,
    "usd": 0.0332,
    "cny_approx": 0.235
  },
  "judge": {
    "calls": 25,
    "errors": 0,
    "prompt_tokens": 40551,
    "cache_hit_tokens": 16000,
    "completion_tokens": 5387,
    "usd": 0.007,
    "cny_approx": 0.049
  },
  "total_usd": 0.0401,
  "pricing": {
    "model": "deepseek-flash",
    "usd_per_1m_peak": {
      "cache_hit": 0.006,
      "cache_miss": 0.3,
      "output": 1.2
    },
    "offpeak_factor": 0.5,
    "note": "computed from recorded usage; DeepSeek's bill is authoritative"
  }
}
```
