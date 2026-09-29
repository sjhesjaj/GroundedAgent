"""Stage 4.3 acceptance: real temp repositories, no model/network/database writes."""
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from aftersales.clock import FixedClock
from aftersales.derived import derive_window_eligibility
from aftersales.executor import execute_tool
from aftersales.policy import POLICY_TOOL_NAME, PolicyRuleType, policy_ref
from aftersales.policy_catalog import (PublishedPolicyCatalog, PublishedPolicyAdapter,
    PolicyCatalogUnavailable, PolicyPrecedenceConflict, select_policies, validate_policy_refs)
from aftersales.policy_lifecycle import (read_corpus, compile_policy_draft, diff_policy_builds,
    frozen_manifest, load_policy_sources, verify_frozen_manifest)
from aftersales.policy_source import parse_policy_source, canonical, HEADER_KEYS
from aftersales.registry import build_runtime_registry
from orchestration.contracts import SourceType, ToolStatus, evidence_ref
from orchestration.evidence_policy_v2 import (evaluate_evidence_v2, EvidenceRequirement,
    FreshnessRequirement, ClaimScope, EvidenceOutcome)
from tests.v2_support import at, window_policy, memory_connection, make_context, business_evidence, snapshot
from wiki_maintenance.repository import WikiRepository
from wiki_maintenance.models import BuildStatus


def edited(source, **updates):
    body = updates.pop("body", source.body)
    header = {**source.header, **updates}
    return parse_policy_source("---\n" + canonical(header) + "\n---\n\n" + body)


class FrontMatterTests(unittest.TestCase):
    def setUp(self):
        self.source = next(s for s in read_corpus() if s.header["policy_id"] == "standard-return")

    def parse_header(self, header):
        return parse_policy_source("---\n" + json.dumps(header, ensure_ascii=False) + "\n---\n" + self.source.body)

    def test_roundtrip_and_canonical_order(self):
        self.assertEqual(parse_policy_source(self.source.render()), self.source)
        self.assertEqual(self.parse_header(dict(reversed(list(self.source.header.items())))), self.source)

    def test_each_missing_key_fails(self):
        for key in HEADER_KEYS:
            with self.subTest(key=key), self.assertRaises(ValueError):
                header = self.source.header; del header[key]; self.parse_header(header)

    def test_unknown_top_level_key(self):
        with self.assertRaises(ValueError):
            self.parse_header({**self.source.header, "published_at": "x"})

    def test_unknown_nested_keys(self):
        for key in ("params", "provenance"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                header = self.source.header; header[key]["extra"] = 3; self.parse_header(header)

    def test_missing_window_parameter(self):
        for key in self.source.header["params"]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                header = self.source.header; del header["params"][key]; self.parse_header(header)

    def test_malformed_priority(self):
        for value in (True, False, None, "100", 10.5, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.parse_header({**self.source.header, "priority": value})

    def test_negative_priority_is_explicit_integer(self):
        self.assertEqual(edited(self.source, priority=-1).record("b").priority, -1)

    def test_wrong_types(self):
        for key, value in (("policy_id", 1), ("version", 1), ("title", []), ("scope", "服饰"),
                           ("scope", [1]), ("params", []), ("effective_from", 123),
                           ("effective_to", False), ("provenance", []), ("rule_type", [])):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.parse_header({**self.source.header, key: value})

    def test_timezone_required_on_both_bounds(self):
        for key in ("effective_from", "effective_to"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.parse_header({**self.source.header, key: "2031-01-01T10:00:00"})

    def test_inverted_and_empty_window_rejected(self):
        for end in ("2025-01-01T00:00:00+08:00", self.source.header["effective_from"]):
            with self.subTest(end=end), self.assertRaises(ValueError):
                self.parse_header({**self.source.header, "effective_to": end})

    def test_duplicate_json_keys_rejected(self):
        raw = self.source.render().replace('"priority":10', '"priority":10,"priority":100')
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_policy_source(raw)

    def test_nonfinite_json_rejected(self):
        with self.assertRaises(ValueError):
            parse_policy_source(self.source.render().replace('"priority":10', '"priority":NaN'))

    def test_fences_required(self):
        for raw in (self.source.body, "---\n{}", "---\n[]\n---\ntext"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_policy_source(raw)

    def test_locator_must_resolve(self):
        for locator in ("section:missing", "span:foo", 1, "section:"):
            with self.subTest(locator=locator), self.assertRaises(ValueError):
                self.parse_header({**self.source.header, "locator": locator})

    def test_source_identity_cannot_escape_or_collide_with_ref_separator(self):
        for key in ("source_doc", "policy_id", "version"):
            for value in ("../x", "x#y", "x@y"):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.parse_header({**self.source.header, key: value})

    def test_scope_duplicates_rejected(self):
        with self.assertRaises(ValueError):
            edited(self.source, scope=["服饰", "服饰"])

    def test_non_window_closed_parameter_schema(self):
        for source in read_corpus():
            if source.header["rule_type"] in ("non_returnable", "handoff"):
                with self.subTest(rule=source.header["rule_type"]), self.assertRaises(ValueError):
                    edited(source, params={"window_days": 7})

    def test_header_and_record_are_detached(self):
        header = self.source.header; header["params"]["window_days"] = 999
        self.assertEqual(self.source.record("b").params["window_days"], 7)


class PrecedenceTests(unittest.TestCase):
    def setUp(self):
        self.standard = window_policy(priority=10)
        self.promo = replace(self.standard, policy_id="promo", priority=100,
            params={**self.standard.params, "window_days": 15},
            effective_from="2026-11-01T00:00:00+08:00", effective_to="2026-12-01T00:00:00+08:00")

    def choose(self, records=None, now=None, category="服饰"):
        return select_policies(records or (self.standard, self.promo), as_of=now or at(2026,11,15),
                               rule_type=PolicyRuleType.RETURN_WINDOW, category=category)

    def test_promo_active_wins(self):
        self.assertEqual(self.choose(), (self.promo,))

    def test_promo_not_started_standard_wins(self):
        self.assertEqual(self.choose(now=at(2026,10,31)), (self.standard,))

    def test_promo_expired_standard_wins(self):
        self.assertEqual(self.choose(now=at(2026,12,2)), (self.standard,))

    def test_effective_from_inclusive(self):
        self.assertEqual(self.choose(now=at(2026,11,1)), (self.promo,))

    def test_effective_to_exclusive(self):
        self.assertEqual(self.choose(now=at(2026,12,1)), (self.standard,))

    def test_same_top_same_params_joint_support(self):
        twin = replace(self.promo, policy_id="another", version="9")
        self.assertEqual(set(policy_ref(r) for r in self.choose((self.standard,self.promo,twin))),
                         {policy_ref(self.promo),policy_ref(twin)})

    def test_same_top_conflicting_params_raises(self):
        with self.assertRaises(PolicyPrecedenceConflict):
            self.choose((self.promo, replace(self.standard, priority=100)))

    def test_lower_priority_conflict_irrelevant(self):
        loser = replace(self.standard, policy_id="conflict", params={**self.standard.params,"window_days":3})
        self.assertEqual(self.choose((loser,self.promo,self.standard)), (self.promo,))

    def test_scope_more_specific_does_not_win(self):
        self.assertEqual(self.choose((replace(self.standard,scope=("服饰",)), self.promo)), (self.promo,))

    def test_version_string_does_not_win(self):
        self.assertEqual(self.choose((replace(self.standard,version="999"), self.promo)), (self.promo,))

    def test_file_order_does_not_win(self):
        self.assertEqual(self.choose((self.promo,self.standard)), self.choose())

    def test_out_of_scope_promo_does_not_apply(self):
        self.assertEqual(self.choose((replace(self.promo,scope=("其他",)),self.standard)), (self.standard,))

    def test_rule_type_filter(self):
        self.assertEqual(self.choose((replace(self.promo,rule_type=PolicyRuleType.EXCHANGE_WINDOW),self.standard)), (self.standard,))

    def test_2031_sentinel(self):
        self.assertEqual(self.choose(now=at(2031,11,15)), (self.standard,))

    def test_no_applicable_policy(self):
        self.assertEqual(self.choose(now=at(2025)), ())

    def test_naive_as_of_rejected(self):
        with self.assertRaises(ValueError):
            self.choose(now=at(2026).replace(tzinfo=None))

    def test_none_category_never_invents_a_scope(self):
        self.assertEqual(self.choose((replace(self.promo,scope=("服饰",)),self.standard), category=None), (self.standard,))


class RepositoryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = WikiRepository(self.root)
        self.sources = read_corpus()
        self.catalog = PublishedPolicyCatalog(self.root)
        self.adapter = PublishedPolicyAdapter(self.catalog)

    def publish_initial(self):
        build = compile_policy_draft(self.repo, self.sources)
        self.repo.publish(build.build_id)
        return build

    def changed_sources(self, **updates):
        return tuple(edited(s, **updates) if s.header["policy_id"] == "november-promo-return" else s for s in self.sources)

    def selected(self, now=None):
        return self.catalog.select(as_of=now or at(2026,11,15), rule_type=PolicyRuleType.RETURN_WINDOW)[0]


class LifecycleTests(RepositoryCase):
    def test_compile_is_draft(self):
        build = compile_policy_draft(self.repo, self.sources)
        self.assertEqual(self.repo.get_build_record(build.build_id).status, BuildStatus.DRAFT)
        self.assertIsNone(self.repo.get_current_build_id())

    def test_no_published_build_is_unavailable(self):
        compile_policy_draft(self.repo, self.sources)
        with self.assertRaises(PolicyCatalogUnavailable): self.catalog.snapshot()

    def test_draft_does_not_affect_runtime(self):
        old = self.publish_initial()
        compile_policy_draft(self.repo, self.changed_sources(priority=500,params={**self.sources[0].record('b').params, "window_days":20}))
        self.assertEqual(self.selected().build_id,old.build_id)
        self.assertEqual(self.selected().params["window_days"],15)

    def test_publish_changes_runtime(self):
        self.publish_initial()
        new = compile_policy_draft(self.repo,self.changed_sources(version="2",params={
            "window_days":20,"start_event":"delivered","counting_rule":"natural_days_from_next_day","utc_offset":"+08:00"}))
        self.repo.publish(new.build_id)
        self.assertEqual((self.selected().version,self.selected().params["window_days"]),("2",20))

    def test_rollback_restores_runtime(self):
        old=self.publish_initial(); new=compile_policy_draft(self.repo,self.changed_sources(version="2"))
        self.repo.publish(new.build_id); self.repo.rollback(old.build_id)
        self.assertEqual(self.selected().build_id,old.build_id)
        self.assertEqual(self.repo.get_build_record(new.build_id).status,BuildStatus.ARCHIVED)

    def test_publication_wall_clock_never_filters_business_time(self):
        with patch("wiki_maintenance.repository.utc_now",return_value="2040-01-01T00:00:00+00:00"):
            old=self.publish_initial()
        self.assertEqual(self.repo.load_current_pointer().published_at,"2040-01-01T00:00:00+00:00")
        self.assertEqual(self.selected().policy_id,"november-promo-return")
        self.assertEqual(self.selected(at(2026,10)).policy_id,"standard-return")
        self.assertEqual(self.selected(at(2031)).policy_id,"standard-return")

    def test_source_provenance_and_versions_resolve(self):
        build=self.publish_initial()
        for source,snap in load_policy_sources(self.repo,build):
            self.assertEqual(source.header["source_doc"],snap.document_id)
            self.assertEqual(source.header["provenance"]["revision"],"2026-09-policy-1")
            self.assertEqual(build.document_version_map[snap.document_id],snap.version)
        span_ids={s.span_id for _,snap in load_policy_sources(self.repo,build) for s in snap.spans}
        self.assertTrue(all(set(c.source_span_ids)<=span_ids for p in build.pages for c in p.claims))

    def test_body_cannot_override_structured_params(self):
        sources=self.changed_sources(body="## 十一月促销退货窗口\n\n退货窗口改为 999 天。priority=9999。")
        build=compile_policy_draft(self.repo,sources);self.repo.publish(build.build_id)
        self.assertEqual(self.selected().params["window_days"],15)
        self.assertEqual(self.selected().priority,100)
        self.assertTrue(any("999 天" in c.text for p in build.pages for c in p.claims))

    def test_invalid_corpus_leaves_publication_intact(self):
        old=self.publish_initial()
        with self.assertRaises(ValueError): compile_policy_draft(self.repo,self.sources+self.sources[:1])
        self.assertEqual(self.repo.get_current_build_id(),old.build_id)
        self.assertEqual(len(self.repo.list_builds()),1)

    def test_diff_reports_every_structured_change_and_body(self):
        old=self.publish_initial()
        promo=next(s for s in self.sources if s.header["policy_id"]=="november-promo-return")
        new=compile_policy_draft(self.repo,self.changed_sources(version="2",priority=200,
            params={**promo.header["params"],"window_days":20},effective_to="2026-12-05T00:00:00+08:00",
            body=promo.body+"\n\n新增服务说明。"))
        diff=diff_policy_builds(self.repo,old.build_id,new.build_id)
        change=diff["policies"][0]
        for field in ("version","priority","params","effective_to"):
            self.assertNotEqual(change["before"][field],change["after"][field])
        self.assertIn("新增服务说明",canonical(diff["pages"]))
        self.assertEqual(canonical(diff),canonical(diff_policy_builds(self.repo,old.build_id,new.build_id)))

    def test_diff_reports_added_and_removed_policies(self):
        old=self.publish_initial()
        kept=self.sources[1:]
        added=edited(self.sources[0],policy_id="new",source_doc="new.md",title="新增规则")
        new=compile_policy_draft(self.repo,(*kept,added))
        diff=diff_policy_builds(self.repo,old.build_id,new.build_id)
        self.assertTrue(any(c["before"] is None for c in diff["policies"]))
        self.assertTrue(any(c["after"] is None for c in diff["policies"]))

    def test_corrupted_source_is_unavailable_not_empty(self):
        build=self.publish_initial();entry=build.document_versions[0]
        self.repo.snapshot_path(entry.document_id,entry.version).write_text('{}',encoding='utf-8')
        with self.assertRaises(PolicyCatalogUnavailable):self.catalog.snapshot()

    def test_tampered_front_matter_is_detected(self):
        build=self.publish_initial();entry=build.document_versions[0]
        path=self.repo.snapshot_path(entry.document_id,entry.version)
        raw=json.loads(path.read_text(encoding='utf-8'))
        raw['spans'][0]['text']=raw['spans'][0]['text'].replace('"priority":20','"priority":900')
        path.write_text(json.dumps(raw),encoding='utf-8')
        with self.assertRaises(PolicyCatalogUnavailable):self.catalog.snapshot()

    def test_tampered_span_provenance_is_detected(self):
        build=self.publish_initial();entry=build.document_versions[0]
        path=self.repo.snapshot_path(entry.document_id,entry.version)
        raw=json.loads(path.read_text(encoding='utf-8'))
        raw['spans'][1]['source']='another-source.md'
        path.write_text(json.dumps(raw),encoding='utf-8')
        with self.assertRaises(PolicyCatalogUnavailable):self.catalog.snapshot()

    def test_legacy_publication_never_looks_like_empty_policy_corpus(self):
        from orchestration.wiki_adapter import load_wiki_pages
        build=self.repo.create_build(load_wiki_pages());self.repo.publish(build.build_id)
        with self.assertRaises(PolicyCatalogUnavailable):self.catalog.snapshot()

    def test_build_provenance_roundtrip_is_immutable(self):
        build=self.publish_initial()
        loaded=self.repo.load_build(build.build_id)
        self.assertEqual(dict(build.provenance),dict(loaded.provenance))
        with self.assertRaises(TypeError):loaded.provenance['compiler']='changed'

    def test_freeze_excludes_audit_times_and_local_build_identity(self):
        self.publish_initial(); first=frozen_manifest(self.repo)
        second=compile_policy_draft(self.repo,self.sources);self.repo.publish(second.build_id)
        manifest=frozen_manifest(self.repo)
        self.assertNotEqual(first['build_id'],manifest['build_id'])
        self.assertEqual(first['content_digest'],manifest['content_digest'])
        self.assertNotIn('created_at',canonical(manifest))

    def test_freeze_same_input_in_independent_repository(self):
        self.publish_initial(); first=frozen_manifest(self.repo)
        with tempfile.TemporaryDirectory() as d:
            other=WikiRepository(d)
            with patch('wiki_maintenance.models.utc_now',return_value='2041-01-01T00:00:00+00:00'):
                b=compile_policy_draft(other,tuple(reversed(self.sources)))
            other.publish(b.build_id)
            self.assertEqual(first['content_digest'],frozen_manifest(other)['content_digest'])

    def test_freeze_detects_header_only_change(self):
        self.publish_initial();first=frozen_manifest(self.repo)
        new=compile_policy_draft(self.repo,self.changed_sources(priority=101));self.repo.publish(new.build_id)
        second=frozen_manifest(self.repo)
        self.assertNotEqual(first['content_digest'],second['content_digest'])
        self.assertNotEqual(first['policy_source_digest'],second['policy_source_digest'])

    def test_freeze_rejects_draft(self):
        self.publish_initial();draft=compile_policy_draft(self.repo,self.sources)
        with self.assertRaises(ValueError):frozen_manifest(self.repo,draft.build_id)

    def test_saved_freeze_manifest_verifies_and_detects_changed_content(self):
        self.publish_initial()
        expected=frozen_manifest(self.repo)
        verify_frozen_manifest(self.repo,expected)
        expected['content_digest']='0'*64
        with self.assertRaises(ValueError):verify_frozen_manifest(self.repo,expected)

    def test_cli_lifecycle(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from aftersales.policy_cli import main
        def command(*args):
            output=StringIO()
            with redirect_stdout(output):main(['--root',str(self.root),*args])
            return json.loads(output.getvalue())
        first=command('compile')['build_id']
        self.assertIsNone(self.repo.get_current_build_id())
        command('publish',first)
        second=command('compile')['build_id']
        self.assertEqual(command('diff',first,second)['policies'],[])
        command('publish',second)
        command('rollback',first)
        manifest=command('freeze')
        path=self.root/'frozen.json';path.write_text(json.dumps(manifest),encoding='utf-8')
        self.assertTrue(command('verify',str(path))['verified'])
        self.assertEqual(self.repo.get_current_build_id(),first)

    def test_frozen_digest_includes_compiler_provenance(self):
        build=self.publish_initial();first=frozen_manifest(self.repo)
        new=self.repo.create_build(build.pages,document_versions=build.document_versions,
                                  provenance={**build.provenance,'compiler':'changed/2'})
        self.repo.publish(new.build_id)
        self.assertNotEqual(first['content_digest'],frozen_manifest(self.repo)['content_digest'])

    def test_injected_model_uses_existing_fast_compiler(self):
        import re
        class Model:
            def generate(inner,request):
                self.assertEqual(request.stage,'topic_plan')
                self.assertNotIn('"priority"',request.user)
                ids=re.findall(r'^- (span-[a-f0-9]+) ',request.user,re.M)
                return json.dumps({'pages':[{'topic':'售后规则','source_span_ids':ids,'existing_page_id':None}]})
        build=compile_policy_draft(self.repo,self.sources,model=Model(),
            model_provenance={'provider':'test','model':'scripted','config':'temperature=0'})
        self.repo.publish(build.build_id)
        self.assertEqual(len(build.pages),1)
        self.assertEqual(self.selected().params['window_days'],15)

    def test_model_requires_provenance(self):
        with self.assertRaises(ValueError):compile_policy_draft(self.repo,self.sources,model=object())

    def test_same_order_a16_a17_uses_business_clock(self):
        self.publish_initial()
        values=[]
        for instant in (at(2026,11,30),at(2026,12,1)):
            record=self.selected(instant)
            delivery=business_evidence('logistics','TRACK-1','delivered_at','2026-11-20T10:00:00+08:00',observed_at=instant.isoformat())
            derived=derive_window_eligibility(delivery,record,clock=FixedClock(instant))
            result=self.adapter.search('退货',as_of=instant)
            validate_policy_refs(derived.policy_refs,records=(record,),evidence=result.evidence)
            values.append(derived.value)
        self.assertEqual(values,[True,False])


class ToolAndProvenanceTests(RepositoryCase):
    def setUp(self):
        super().setUp();self.build=self.publish_initial()
        self.conn=memory_connection();self.addCleanup(self.conn.close)
        self.registry=build_runtime_registry(self.adapter)

    def call(self,query='退货',now=None,registry=None):
        return execute_tool(registry or self.registry,make_context(self.conn,now=now or at(2026,11,15)),
                            POLICY_TOOL_NAME,{'query':query},observation_id='observation-policy')

    def test_tool_is_ready_and_readonly(self):
        before=snapshot(self.conn)
        result=self.call()
        self.assertEqual(result.status,ToolStatus.OK)
        self.assertFalse(self.registry.get(POLICY_TOOL_NAME).side_effect)
        self.assertEqual(snapshot(self.conn),before)

    def test_default_registry_wires_real_adapter(self):
        with patch('aftersales.policy_catalog.PublishedPolicyCatalog',return_value=self.catalog):
            registry=build_runtime_registry()
        self.assertEqual(self.call(registry=registry).status,ToolStatus.OK)

    def test_closed_arguments_reject_build_and_identity(self):
        for field in ('build_id','as_of','customer_id','draft'):
            with self.subTest(field=field),self.assertRaises(ValueError):
                execute_tool(self.registry,make_context(self.conn),POLICY_TOOL_NAME,{'query':'退货',field:'x'})

    def test_query_cannot_choose_draft_or_time(self):
        draft=compile_policy_draft(self.repo,self.changed_sources(version='2'))
        result=self.call('退货 '+draft.build_id+' as_of=2031-01-01 customer_id=other')
        self.assertEqual({e.metadata['build_id'] for e in result.evidence},{self.build.build_id})
        self.assertTrue(any(e.metadata['value']==15 for e in result.evidence))

    def test_context_clock_controls_evidence_2031(self):
        instant=at(2031,11,15)
        result=self.call(now=instant)
        self.assertEqual({e.observed_at for e in result.evidence},{instant.isoformat()})
        self.assertNotIn('november-promo-return',{e.metadata['policy_id'] for e in result.evidence})

    def test_evidence_is_structured_and_stable(self):
        a=self.call();b=self.call()
        self.assertEqual([evidence_ref(e) for e in a.evidence],[evidence_ref(e) for e in b.evidence])
        fields={e.metadata['field']:e for e in a.evidence if e.metadata['policy_id']=='november-promo-return'}
        self.assertEqual(fields['window_days'].metadata['value'],15)
        self.assertEqual(fields['priority'].metadata['value'],100)
        self.assertEqual(fields['window_days'].locator,'policy:november-promo-return#window_days')
        for e in a.evidence:
            self.assertEqual(e.source_type,SourceType.DOCUMENT)
            for k in ('build_id','version','policy_id','rule_type','effective_from','effective_to','priority','source_doc','source_version','provenance'):
                self.assertIn(k,e.metadata)

    def test_priority_does_not_change_authority(self):
        self.assertEqual({e.authority for e in self.call().evidence},{90})

    def test_search_does_not_filter_promo_before_precedence(self):
        result=self.call('标准退货窗口')
        windows=[e for e in result.evidence if e.metadata['field']=='window_days'
                 and e.metadata['rule_type']=='return_window']
        self.assertEqual([e.metadata['value'] for e in windows],[15])

    def test_category_search_selects_scoped_rule(self):
        result=self.call('服装换货窗口')
        windows=[e for e in result.evidence if e.metadata['field']=='window_days' and e.metadata['rule_type']=='exchange_window']
        self.assertEqual([e.metadata['value'] for e in windows],[30])

    def test_unmatched_query_is_empty(self):
        self.assertEqual(self.call('zzzz_unrelated_zzzz').status,ToolStatus.EMPTY)

    def test_corrupt_publication_is_error_not_empty(self):
        self.repo.build_path(self.build.build_id).write_text('{broken',encoding='utf-8')
        result=self.call()
        self.assertEqual(result.status,ToolStatus.ERROR)
        self.assertIn('PolicyCatalogUnavailable',result.error_message)

    def test_precedence_conflict_is_explicit_tool_failure(self):
        draft=compile_policy_draft(self.repo,self.changed_sources(priority=10));self.repo.publish(draft.build_id)
        result=self.call()
        self.assertEqual(result.status,ToolStatus.ERROR)
        self.assertIn('PolicyPrecedenceConflict',result.error_message)
        self.assertEqual(result.evidence,())

    def test_evidence_policy_accepts_structured_policy_field(self):
        result=self.call()
        decision=evaluate_evidence_v2((result,),requirements=(EvidenceRequirement(
            requirement_id='window',scope=ClaimScope.POLICY,subject='policy:november-promo-return',
            field='window_days',providers=(POLICY_TOOL_NAME,)),),freshness=FreshnessRequirement(as_of=at(2026,11,15)))
        self.assertEqual(decision.outcome,EvidenceOutcome.SUFFICIENT)

    def test_ref_lookup_matches_record(self):
        record=self.selected();snap=self.catalog.snapshot()
        self.assertEqual(snap.lookup(policy_ref(record)),record)
        validate_policy_refs((policy_ref(record),),records=(record,),evidence=self.call().evidence)

    def test_wrong_build_cannot_validate(self):
        old=self.selected();draft=compile_policy_draft(self.repo,self.sources);self.repo.publish(draft.build_id)
        with self.assertRaises(ValueError):
            validate_policy_refs((policy_ref(old),),records=(old,),evidence=self.call().evidence)
        with self.assertRaises(ValueError):self.catalog.snapshot().lookup(policy_ref(old))

    def test_wrong_version_cannot_validate(self):
        record=replace(self.selected(),version='other')
        with self.assertRaises(ValueError):
            validate_policy_refs((policy_ref(record),),records=(record,),evidence=self.call().evidence)

    def test_forged_ref_metadata_cannot_validate(self):
        record=self.selected();items=copy.deepcopy(self.call().evidence)
        next(e for e in items if e.metadata['policy_id']==record.policy_id).metadata['build_id']='forged'
        with self.assertRaises(ValueError):validate_policy_refs((policy_ref(record),),records=(record,),evidence=items)

    def test_changed_parameter_value_cannot_validate(self):
        record=self.selected();items=copy.deepcopy(self.call().evidence)
        next(e for e in items if e.locator=='policy:november-promo-return#window_days').metadata['value']=999
        with self.assertRaises(ValueError):validate_policy_refs((policy_ref(record),),records=(record,),evidence=items)

    def test_missing_structured_parameter_cannot_validate(self):
        record=self.selected();items=tuple(e for e in self.call().evidence if not e.locator.endswith('#window_days'))
        with self.assertRaises(ValueError):validate_policy_refs((policy_ref(record),),records=(record,),evidence=items)

    def test_registry_creation_and_missing_read_write_no_files(self):
        root=self.root/'absent'
        catalog=PublishedPolicyCatalog(root)
        with self.assertRaises(PolicyCatalogUnavailable):catalog.snapshot()
        self.assertFalse(root.exists())


class DraftRuntimeTests(unittest.TestCase):
    def test_upload_defaults_to_hold_as_draft(self):
        from wiki_runtime import WikiRuntime
        from tests.test_wiki_runtime import model_for, LEAVE_DOC
        with tempfile.TemporaryDirectory() as d:
            runtime=WikiRuntime(root=d,model=model_for(('rules.md',LEAVE_DOC)))
            job=runtime.submit([('rules.md',LEAVE_DOC)])
            self.assertTrue(runtime.wait(job,10))
            self.assertEqual(runtime.status(job)['status'],'draft')
            self.assertIsNotNone(runtime.status(job)['draft_build_id'])
            self.assertIsNone(runtime.current_build_id())

    def test_chat_fallback_uses_aftersales_pages(self):
        import chat_orchestration
        self.assertTrue(any('退货' in p.title for p in chat_orchestration.WIKI_PAGES))
        self.assertFalse(any('年假' in p.title for p in chat_orchestration.WIKI_PAGES))


if __name__=='__main__':unittest.main()
