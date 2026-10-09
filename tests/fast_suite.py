"""Fast daily test set: after-sales product runtime, M1-M3, and freeze checks.

Target: under one minute. Run with ``.\\run_tests.ps1 -Fast`` or
``python -X utf8 -m unittest tests.fast_suite``. Not picked up by
``unittest discover`` (the file name does not match ``test*.py``).

This is a subset, not a replacement: the full offline suite
(``.\\run_tests.ps1 -Full``) must pass before a PR. The slow end-to-end files
run only there: golden transcripts, crash recovery, persistence, M3 scripted
equivalence, and the synthetic unseal sandboxes of the seal tests.
"""

FAST_TESTS = (
    # M0 product runtime and routes, M1 grounding gate, M2 state codec / graph.
    "tests.test_aftersales_service",
    "tests.test_aftersales_grounding",
    "tests.test_aftersales_state_codec",
    "tests.test_aftersales_graph",
    # M1-A2 grounding eval; FrozenBoundaryTests also keeps aftersales/ and eval_v2/ frozen.
    "tests.test_m1_a2_grounding_eval",
    # M3 policy RAG.
    "tests.test_m3_answer_policy",
    "tests.test_m3_corpus_build",
    "tests.test_m3_decision_policy",
    "tests.test_m3_eval_runner",
    "tests.test_m3_evaluation_overlap",
    "tests.test_m3_generation",
    "tests.test_m3_history_safety",
    "tests.test_m3_retrieval",
    "tests.test_m3_runtime",
    # Freeze checks: frozen holdout inputs, seal manifests, holdouts never opened.
    "tests.test_v2_holdout_input_freeze",
    "tests.test_v2_holdout_seal.ManifestTests",
    "tests.test_v2_holdout_seal.ScriptSafetyTests",
    "tests.test_v2_stage6_holdout_seal.ManifestTests",
    "tests.test_v2_stage6_holdout_seal.ScriptSafetyTests",
    "tests.test_v2_stage6_holdout_seal.RepositoryStateTests",
    "tests.test_v2_stage6_author_bundle.ManifestTests",
)


def load_tests(loader, standard_tests, pattern):
    return loader.loadTestsFromNames(FAST_TESTS)
