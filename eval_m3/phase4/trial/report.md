# eval_m3 run

level: Phase 4 runner check with real DeepSeek on 5 KB-DEV cases; not a Phase 5 result

## kb-dev

```json
{
  "runs": 1,
  "cases": 5,
  "pass_rate_per_run": [
    {
      "numerator": 3,
      "denominator": 5,
      "rate": 0.6
    }
  ],
  "pass_hat_k": {
    "k": 1,
    "numerator": 3,
    "denominator": 5,
    "rate": 0.6
  },
  "failed_cases_per_run": [
    [
      "kb-dev-013",
      "kb-dev-025"
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
    "numerator": 1,
    "denominator": 1,
    "rate": 1.0
  },
  "per_turn": {
    "business_turns": 7,
    "prompt_tokens_p50": 24999.0,
    "prompt_tokens_p95": 26481.5,
    "completion_tokens_p50": 266.0,
    "completion_tokens_p95": 336.2,
    "seconds_p50": 4.423,
    "seconds_p95": 4.961,
    "model_calls_p50": 3.0,
    "model_calls_p95": 3.0,
    "prompt_tokens_total": 130008,
    "completion_tokens_total": 1715,
    "cache_hit_tokens_total": 49920
  },
  "kb": {
    "turn_e2e": {
      "numerator": 5,
      "denominator": 7,
      "rate": 0.7143
    },
    "disposition": {
      "numerator": 7,
      "denominator": 7,
      "rate": 1.0
    },
    "routing_accuracy": {
      "numerator": 7,
      "denominator": 7,
      "rate": 1.0
    },
    "preferred_route_coverage": {
      "numerator": 1,
      "denominator": 10,
      "rate": 0.1
    },
    "facts": {
      "numerator": 5,
      "denominator": 7,
      "rate": 0.7143
    },
    "must_include_missing": 2,
    "must_not_include_violated": 0,
    "judge_errors": 0,
    "citation_hit": {
      "numerator": 5,
      "denominator": 5,
      "rate": 1.0
    },
    "citation_section_hit": {
      "numerator": 2,
      "denominator": 5,
      "rate": 0.4
    },
    "rule_consistency_statements": {
      "numerator": 3,
      "denominator": 3,
      "rate": 1.0
    },
    "rule_consistency_turns": {
      "numerator": 3,
      "denominator": 3,
      "rate": 1.0
    },
    "history_only": {
      "numerator": 0,
      "denominator": 1,
      "rate": 0.0,
      "display": null,
      "flagged": []
    },
    "failed_turns": [
      {
        "case_id": "kb-dev-013",
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

## cost

```json
{
  "agent": {
    "calls": 19,
    "errors": 0,
    "prompt_tokens": 130008,
    "cache_hit_tokens": 49920,
    "completion_tokens": 1715,
    "usd": 0.0132,
    "cny_approx": 0.094
  },
  "judge": {
    "calls": 7,
    "errors": 0,
    "prompt_tokens": 12259,
    "cache_hit_tokens": 3840,
    "completion_tokens": 1630,
    "usd": 0.0023,
    "cny_approx": 0.016
  },
  "total_usd": 0.0154,
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
