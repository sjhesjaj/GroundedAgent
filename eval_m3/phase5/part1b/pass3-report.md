# eval_m3 run

level: Phase 5 part 1 rerun after rule 18 (Revision 11): KB-DEV + Stage 6 subset pass^3

## kb-dev

```json
{
  "runs": 3,
  "cases": 40,
  "pass_rate_per_run": [
    {
      "numerator": 30,
      "denominator": 40,
      "rate": 0.75
    },
    {
      "numerator": 31,
      "denominator": 40,
      "rate": 0.775
    },
    {
      "numerator": 30,
      "denominator": 40,
      "rate": 0.75
    }
  ],
  "pass_hat_k": {
    "k": 3,
    "numerator": 27,
    "denominator": 40,
    "rate": 0.675
  },
  "failed_cases_per_run": [
    [
      "kb-dev-005",
      "kb-dev-006",
      "kb-dev-009",
      "kb-dev-011",
      "kb-dev-012",
      "kb-dev-013",
      "kb-dev-014",
      "kb-dev-016",
      "kb-dev-025",
      "kb-dev-028"
    ],
    [
      "kb-dev-002",
      "kb-dev-005",
      "kb-dev-006",
      "kb-dev-009",
      "kb-dev-013",
      "kb-dev-014",
      "kb-dev-016",
      "kb-dev-025",
      "kb-dev-028"
    ],
    [
      "kb-dev-005",
      "kb-dev-006",
      "kb-dev-008",
      "kb-dev-009",
      "kb-dev-011",
      "kb-dev-012",
      "kb-dev-013",
      "kb-dev-016",
      "kb-dev-019",
      "kb-dev-028"
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
    "numerator": 12,
    "denominator": 12,
    "rate": 1.0
  },
  "per_turn": {
    "business_turns": 138,
    "prompt_tokens_p50": 9470.5,
    "prompt_tokens_p95": 39854.25,
    "completion_tokens_p50": 228.5,
    "completion_tokens_p95": 492.55,
    "seconds_p50": 4.358,
    "seconds_p95": 6.988,
    "model_calls_p50": 3.0,
    "model_calls_p95": 3.0,
    "prompt_tokens_total": 2125830,
    "completion_tokens_total": 31956,
    "cache_hit_tokens_total": 1313910
  },
  "kb": {
    "turn_e2e": {
      "numerator": 121,
      "denominator": 150,
      "rate": 0.8067
    },
    "disposition": {
      "numerator": 144,
      "denominator": 150,
      "rate": 0.96
    },
    "routing_accuracy": {
      "numerator": 149,
      "denominator": 150,
      "rate": 0.9933
    },
    "preferred_route_coverage": {
      "numerator": 27,
      "denominator": 138,
      "rate": 0.1957
    },
    "facts": {
      "numerator": 121,
      "denominator": 150,
      "rate": 0.8067
    },
    "must_include_missing": 40,
    "must_not_include_violated": 0,
    "judge_errors": 0,
    "citation_hit": {
      "numerator": 73,
      "denominator": 79,
      "rate": 0.9241
    },
    "citation_section_hit": {
      "numerator": 48,
      "denominator": 51,
      "rate": 0.9412
    },
    "rule_consistency_statements": {
      "numerator": 24,
      "denominator": 24,
      "rate": 1.0
    },
    "rule_consistency_turns": {
      "numerator": 20,
      "denominator": 20,
      "rate": 1.0
    },
    "history_only": {
      "numerator": 0,
      "denominator": 15,
      "rate": 0.0,
      "display": null,
      "flagged": []
    },
    "failed_turns": [
      {
        "case_id": "kb-dev-005",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": true
      },
      {
        "case_id": "kb-dev-006",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": true
      },
      {
        "case_id": "kb-dev-009",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": null
      },
      {
        "case_id": "kb-dev-011",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": null
      },
      {
        "case_id": "kb-dev-012",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": null
      },
      {
        "case_id": "kb-dev-013",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": true
      },
      {
        "case_id": "kb-dev-014",
        "turn": 1,
        "disposition_ok": false,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": null
      },
      {
        "case_id": "kb-dev-016",
        "turn": 1,
        "disposition_ok": false,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": null
      },
      {
        "case_id": "kb-dev-025",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": true
      },
      {
        "case_id": "kb-dev-028",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": null
      },
      {
        "case_id": "kb-dev-002",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": null
      },
      {
        "case_id": "kb-dev-005",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": true
      },
      {
        "case_id": "kb-dev-006",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": true
      },
      {
        "case_id": "kb-dev-009",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": null
      },
      {
        "case_id": "kb-dev-013",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": true
      },
      {
        "case_id": "kb-dev-014",
        "turn": 1,
        "disposition_ok": false,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": null
      },
      {
        "case_id": "kb-dev-016",
        "turn": 1,
        "disposition_ok": false,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": null
      },
      {
        "case_id": "kb-dev-025",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": true
      },
      {
        "case_id": "kb-dev-028",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": null
      },
      {
        "case_id": "kb-dev-005",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": true
      },
      {
        "case_id": "kb-dev-006",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": true
      },
      {
        "case_id": "kb-dev-008",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": null
      },
      {
        "case_id": "kb-dev-009",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": null
      },
      {
        "case_id": "kb-dev-011",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": null
      },
      {
        "case_id": "kb-dev-012",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": null
      },
      {
        "case_id": "kb-dev-013",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": true
      },
      {
        "case_id": "kb-dev-016",
        "turn": 1,
        "disposition_ok": false,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": null
      },
      {
        "case_id": "kb-dev-019",
        "turn": 1,
        "disposition_ok": false,
        "routing_ok": false,
        "facts_ok": false,
        "window_ok": null
      },
      {
        "case_id": "kb-dev-028",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": null
      }
    ]
  },
  "retrieval": {
    "turns": 17,
    "gold_sections": 35,
    "summary": {
      "bm25": {
        "recall@1": {
          "macro": 0.1765,
          "micro": 0.1429
        },
        "recall@3": {
          "macro": 0.4922,
          "micro": 0.4286
        },
        "recall@5": {
          "macro": 0.651,
          "micro": 0.6286
        }
      },
      "vector": {
        "recall@1": {
          "macro": 0.1863,
          "micro": 0.2
        },
        "recall@3": {
          "macro": 0.5392,
          "micro": 0.4857
        },
        "recall@5": {
          "macro": 0.6471,
          "micro": 0.6
        }
      },
      "hybrid": {
        "recall@1": {
          "macro": 0.2157,
          "micro": 0.2286
        },
        "recall@3": {
          "macro": 0.4824,
          "micro": 0.4571
        },
        "recall@5": {
          "macro": 0.7392,
          "micro": 0.7143
        }
      }
    }
  }
}
```

## stage6-subset

```json
{
  "runs": 3,
  "cases": 24,
  "pass_rate_per_run": [
    {
      "numerator": 21,
      "denominator": 24,
      "rate": 0.875
    },
    {
      "numerator": 20,
      "denominator": 24,
      "rate": 0.8333
    },
    {
      "numerator": 19,
      "denominator": 24,
      "rate": 0.7917
    }
  ],
  "pass_hat_k": {
    "k": 3,
    "numerator": 19,
    "denominator": 24,
    "rate": 0.7917
  },
  "failed_cases_per_run": [
    [
      "s6-dev-015",
      "s6-dev-017",
      "s6-dev-025"
    ],
    [
      "s6-dev-005",
      "s6-dev-015",
      "s6-dev-017",
      "s6-dev-025"
    ],
    [
      "s6-dev-005",
      "s6-dev-015",
      "s6-dev-017",
      "s6-dev-019",
      "s6-dev-025"
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
    "numerator": 21,
    "denominator": 21,
    "rate": 1.0
  },
  "per_turn": {
    "business_turns": 72,
    "prompt_tokens_p50": 9221.0,
    "prompt_tokens_p95": 27358.0,
    "completion_tokens_p50": 165.0,
    "completion_tokens_p95": 525.25,
    "seconds_p50": 2.827,
    "seconds_p95": 6.717,
    "model_calls_p50": 2.0,
    "model_calls_p95": 4.0,
    "prompt_tokens_total": 836923,
    "completion_tokens_total": 13736,
    "cache_hit_tokens_total": 633078
  },
  "stage6": {
    "outcome_ok": {
      "numerator": 60,
      "denominator": 72,
      "rate": 0.8333
    },
    "clarification_ok": {
      "numerator": 69,
      "denominator": 72,
      "rate": 0.9583
    },
    "capabilities_ok": {
      "numerator": 66,
      "denominator": 72,
      "rate": 0.9167
    },
    "final_state_ok": {
      "numerator": 66,
      "denominator": 72,
      "rate": 0.9167
    }
  }
}
```

## cost

```json
{
  "agent": {
    "calls": 496,
    "errors": 0,
    "prompt_tokens": 2962753,
    "cache_hit_tokens": 1946988,
    "completion_tokens": 45692,
    "usd": 0.1856,
    "cny_approx": 1.318
  },
  "judge": {
    "calls": 150,
    "errors": 0,
    "prompt_tokens": 240139,
    "cache_hit_tokens": 135016,
    "completion_tokens": 30148,
    "usd": 0.0343,
    "cny_approx": 0.243
  },
  "total_usd": 0.2199,
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
