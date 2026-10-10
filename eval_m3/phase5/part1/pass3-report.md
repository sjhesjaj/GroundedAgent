# eval_m3 run

level: Phase 5 part 1: KB-DEV + Stage 6 subset pass^3 (formal DEV run)

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
      "numerator": 30,
      "denominator": 40,
      "rate": 0.75
    },
    {
      "numerator": 32,
      "denominator": 40,
      "rate": 0.8
    }
  ],
  "pass_hat_k": {
    "k": 3,
    "numerator": 28,
    "denominator": 40,
    "rate": 0.7
  },
  "failed_cases_per_run": [
    [
      "kb-dev-005",
      "kb-dev-006",
      "kb-dev-009",
      "kb-dev-012",
      "kb-dev-013",
      "kb-dev-014",
      "kb-dev-016",
      "kb-dev-025",
      "kb-dev-028",
      "kb-dev-040"
    ],
    [
      "kb-dev-005",
      "kb-dev-009",
      "kb-dev-012",
      "kb-dev-013",
      "kb-dev-016",
      "kb-dev-022",
      "kb-dev-025",
      "kb-dev-028",
      "kb-dev-036",
      "kb-dev-040"
    ],
    [
      "kb-dev-005",
      "kb-dev-006",
      "kb-dev-009",
      "kb-dev-012",
      "kb-dev-013",
      "kb-dev-016",
      "kb-dev-025",
      "kb-dev-040"
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
    "prompt_tokens_p50": 9342.0,
    "prompt_tokens_p95": 29792.0,
    "completion_tokens_p50": 225.0,
    "completion_tokens_p95": 558.8,
    "seconds_p50": 4.018,
    "seconds_p95": 6.643,
    "model_calls_p50": 3.0,
    "model_calls_p95": 3.0,
    "prompt_tokens_total": 2071806,
    "completion_tokens_total": 33329,
    "cache_hit_tokens_total": 1428603
  },
  "kb": {
    "turn_e2e": {
      "numerator": 122,
      "denominator": 150,
      "rate": 0.8133
    },
    "disposition": {
      "numerator": 143,
      "denominator": 150,
      "rate": 0.9533
    },
    "routing_accuracy": {
      "numerator": 150,
      "denominator": 150,
      "rate": 1.0
    },
    "preferred_route_coverage": {
      "numerator": 24,
      "denominator": 138,
      "rate": 0.1739
    },
    "facts": {
      "numerator": 122,
      "denominator": 150,
      "rate": 0.8133
    },
    "must_include_missing": 41,
    "must_not_include_violated": 0,
    "judge_errors": 0,
    "citation_hit": {
      "numerator": 78,
      "denominator": 81,
      "rate": 0.963
    },
    "citation_section_hit": {
      "numerator": 51,
      "denominator": 51,
      "rate": 1.0
    },
    "rule_consistency_statements": {
      "numerator": 26,
      "denominator": 26,
      "rate": 1.0
    },
    "rule_consistency_turns": {
      "numerator": 21,
      "denominator": 21,
      "rate": 1.0
    },
    "history_only": {
      "numerator": 1,
      "denominator": 15,
      "rate": 0.0667,
      "display": null,
      "flagged": [
        {
          "case_id": "kb-dev-028",
          "turn": 2,
          "facts": [
            {
              "fact": "与本次寄回商品关联的赠品需要一同寄回",
              "supported_by_current_reads": false
            },
            {
              "fact": "赠品一同寄回并核对一致时，不会从退款金额中扣除赠品价值",
              "supported_by_current_reads": true
            }
          ]
        }
      ]
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
        "case_id": "kb-dev-040",
        "turn": 1,
        "disposition_ok": false,
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
        "case_id": "kb-dev-009",
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
        "case_id": "kb-dev-022",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": true
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
        "case_id": "kb-dev-036",
        "turn": 1,
        "disposition_ok": false,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": null
      },
      {
        "case_id": "kb-dev-040",
        "turn": 1,
        "disposition_ok": false,
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
        "case_id": "kb-dev-025",
        "turn": 1,
        "disposition_ok": true,
        "routing_ok": true,
        "facts_ok": false,
        "window_ok": true
      },
      {
        "case_id": "kb-dev-040",
        "turn": 1,
        "disposition_ok": false,
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
      "numerator": 21,
      "denominator": 24,
      "rate": 0.875
    },
    {
      "numerator": 21,
      "denominator": 24,
      "rate": 0.875
    }
  ],
  "pass_hat_k": {
    "k": 3,
    "numerator": 21,
    "denominator": 24,
    "rate": 0.875
  },
  "failed_cases_per_run": [
    [
      "s6-dev-015",
      "s6-dev-017",
      "s6-dev-025"
    ],
    [
      "s6-dev-015",
      "s6-dev-017",
      "s6-dev-025"
    ],
    [
      "s6-dev-015",
      "s6-dev-017",
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
    "prompt_tokens_p50": 9085.0,
    "prompt_tokens_p95": 27222.0,
    "completion_tokens_p50": 167.0,
    "completion_tokens_p95": 533.35,
    "seconds_p50": 2.728,
    "seconds_p95": 6.121,
    "model_calls_p50": 2.0,
    "model_calls_p95": 4.0,
    "prompt_tokens_total": 844454,
    "completion_tokens_total": 13946,
    "cache_hit_tokens_total": 674938
  },
  "stage6": {
    "outcome_ok": {
      "numerator": 63,
      "denominator": 72,
      "rate": 0.875
    },
    "clarification_ok": {
      "numerator": 69,
      "denominator": 72,
      "rate": 0.9583
    },
    "capabilities_ok": {
      "numerator": 69,
      "denominator": 72,
      "rate": 0.9583
    },
    "final_state_ok": {
      "numerator": 69,
      "denominator": 72,
      "rate": 0.9583
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
    "prompt_tokens": 2916260,
    "cache_hit_tokens": 2103541,
    "completion_tokens": 47275,
    "usd": 0.1566,
    "cny_approx": 1.112
  },
  "judge": {
    "calls": 150,
    "errors": 0,
    "prompt_tokens": 243477,
    "cache_hit_tokens": 116719,
    "completion_tokens": 29950,
    "usd": 0.0373,
    "cny_approx": 0.265
  },
  "total_usd": 0.1939,
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
