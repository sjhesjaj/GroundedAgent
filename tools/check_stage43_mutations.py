"""Run Stage 4.3 semantic mutants in a disposable source copy, never the checkout.

Uses the active interpreter and stdlib unittest. A clean control must pass;
each mutant must fail the designated assertion/semantic path, not import.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
TEST = "tests.test_v2_policy_lifecycle.LifecycleTests."
PRECEDENCE = "tests.test_v2_policy_lifecycle.PrecedenceTests."
PROVENANCE = "tests.test_v2_policy_lifecycle.ToolAndProvenanceTests."
MUTATIONS = (
    ("generic_upload_defaults_to_draft", "wiki_runtime.py",
     "hold_as_draft: bool = False,", "hold_as_draft: bool = True,",
     "tests.test_v2_policy_lifecycle.DraftRuntimeTests.test_upload_defaults_to_publish"),
    ("source_digest_not_validated", "aftersales/policy_catalog.py",
     '                            "source_digest": source_digest,\n', '',
     PROVENANCE + "test_forged_source_digest_cannot_validate"),
    ("source_version_not_validated", "aftersales/policy_catalog.py",
     '                            "source_version": source_version,\n', '',
     PROVENANCE + "test_forged_source_version_cannot_validate"),
    ("provenance_not_validated", "aftersales/policy_catalog.py",
     '                            "provenance": policy_provenance,\n', '',
     PROVENANCE + "test_forged_provenance_cannot_validate"),
    ("default_catalog_uses_empty_generic_root", "aftersales/policy_catalog.py",
     'root: str | Path = DEFAULT_AFTERSALES_POLICY_ROOT',
     'root: str | Path = Path(__file__).resolve().parent.parent / "data" / "wiki"',
     PROVENANCE + "test_default_registry_wires_real_adapter"),
    ("publication_time_used_as_effective_time", "aftersales/policy_catalog.py",
     "tuple(s.record(build.build_id) for s, _ in pairs)",
     "tuple(__import__('dataclasses').replace(s.record(build.build_id), "
     "effective_from=repository.load_current_pointer().published_at, effective_to=None) for s, _ in pairs)",
     TEST + "test_publication_wall_clock_never_filters_business_time"),
    ("priority_ignored", "aftersales/policy_catalog.py",
     "if r.priority == priority", "if True",
     PRECEDENCE + "test_promo_active_wins"),
    ("lower_priority_wins", "aftersales/policy_catalog.py",
     "priority = max(r.priority for r in applicable)", "priority = min(r.priority for r in applicable)",
     PRECEDENCE + "test_promo_active_wins"),
    ("draft_visible", "aftersales/policy_catalog.py",
     "build = repository.load_current_build()",
     "build = repository.load_build(repository.list_builds()[-1].build_id)",
     TEST + "test_draft_does_not_affect_runtime"),
    ("rollback_noop", "wiki_maintenance/repository.py",
     "return self._switch_current(build_id, require_archived=True)",
     "return self.get_build_record(self.get_current_build_id())",
     TEST + "test_rollback_restores_runtime"),
    ("body_overrides_front_matter", "aftersales/policy_source.py",
     "return PolicyRecord(**values, build_id=build_id)",
     "if '999 天' in self.body:\n            values['params']['window_days'] = 999\n"
     "        return PolicyRecord(**values, build_id=build_id)",
     TEST + "test_body_cannot_override_structured_params"),
)


def main():
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
    with tempfile.TemporaryDirectory(prefix="stage43-mutants-") as directory:
        copied = Path(directory)
        for name in ("aftersales", "orchestration", "wiki_maintenance", "tests", "policy_sources",
                     "system_fixtures", "wiki_pages"):
            shutil.copytree(ROOT / name, copied / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for source in ROOT.glob("*.py"):
            shutil.copy2(source, copied / source.name)
        shutil.copy2(ROOT / "sample_company_rules.md", copied / "sample_company_rules.md")

        def run(tests):
            return subprocess.run([sys.executable, "-B", "-m", "unittest", *tests, "-q"],
                                  cwd=copied, env=env, capture_output=True, text=True, encoding="utf-8",
                                  timeout=60)

        control = run(sorted({m[-1] for m in MUTATIONS}))
        if control.returncode:
            raise RuntimeError("mutation control failed:\n" + control.stderr)
        results = []
        for name, filename, before, after, test in MUTATIONS:
            path = copied / filename
            original = path.read_text(encoding="utf-8")
            if original.count(before) != 1:
                raise RuntimeError("mutation anchor is not unique: " + name)
            try:
                path.write_text(original.replace(before, after), encoding="utf-8")
                result = run([test])
            finally:
                path.write_text(original, encoding="utf-8")
            killed = (result.returncode == 1 and "FAILED (" in result.stderr
                      and "ImportError" not in result.stderr and "SyntaxError" not in result.stderr
                      and test.rsplit(".", 1)[-1] in result.stderr)
            results.append({"mutation": name, "killed": killed, "test": test,
                            "exit_code": result.returncode, "output": result.stderr})
        report = {"control": "passed", "killed": sum(r["killed"] for r in results),
                  "total": len(results), "results": results}
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 0 if all(r["killed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
