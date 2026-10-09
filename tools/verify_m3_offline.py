"""Full unittest suite in a temporary V1 DB, with all real network blocked."""
from __future__ import annotations

import json
import hashlib
import argparse
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
parser.add_argument('--output', type=Path, required=True, help='Where to write the audit JSON')
arguments = parser.parse_args()
repo = arguments.repo.resolve()
output = arguments.output.resolve()
os.chdir(repo)
sys.path.insert(0, str(repo))
os.environ.pop('AFTERSALES_DECISION_POLICY', None)
os.environ['AFTERSALES_KB_OFFLINE'] = '1'
os.environ['AFTERSALES_KB_EMBED_CACHE'] = str(repo / '.cache' / 'm3-embeddings')
sys.dont_write_bytecode = True
import requests
import storage

active_test = None
http_attempts = []
socket_attempts = []
database_attempts = []

def request(self, method, url, *args, **kwargs):
    http_attempts.append({'test': active_test, 'method': method, 'url': str(url)})
    raise requests.ConnectionError('offline audit intercepted an unmocked HTTP request')

def audit(event, args):
    if event == 'socket.connect':
        caller = sys._getframe(1)
        if (caller.f_code.co_name == '_fallback_socketpair'
                and Path(caller.f_code.co_filename).resolve() == Path(socket.__file__).resolve()
                and args[1][0] in ('127.0.0.1', '::1')):
            return
        socket_attempts.append({'test': active_test, 'address': repr(args[1])})
        raise RuntimeError('offline audit forbids real socket connections')
    if event == 'sqlite3.connect':
        value = str(args[0])
        if value != ':memory:' and not value.startswith('file:'):
            resolved = Path(value).resolve()
            if any(resolved.is_relative_to(repo / path) for path in ('data', '.aftersales-demo')):
                database_attempts.append({'test': active_test, 'path': str(resolved)})
                raise RuntimeError('offline audit forbids the real repository database')

sys.addaudithook(audit)

def flatten(suite):
    for child in suite:
        if isinstance(child, unittest.TestSuite):
            yield from flatten(child)
        else:
            yield child

class AuditResult(unittest.TextTestResult):
    def startTest(self, test):
        global active_test
        active_test = test.id()
        super().startTest(test)

def cache_snapshot():
    directory = repo / '.cache' / 'm3-embeddings'
    return {str(path.relative_to(directory)): [hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns]
            for path in directory.rglob('*') if path.is_file()}

with tempfile.TemporaryDirectory(prefix='m3-phase1-offline-import-') as directory:
    original = storage.SQLiteStorage.__init__
    def init(self, path='data/knowledge_agent.db'):
        if str(path).replace('\\', '/') == 'data/knowledge_agent.db':
            path = Path(directory) / 'default-v1.db'
        original(self, path)
    with mock.patch.object(storage.SQLiteStorage, '__init__', init):
        discovered = list(flatten(unittest.defaultTestLoader.discover('.')))
    selected = [case for case in discovered if not case.id().startswith('tests.test_llm_provider_live.')]
    excluded = [case.id() for case in discovered if case.id().startswith('tests.test_llm_provider_live.')]
    started = time.perf_counter()
    cache_before = cache_snapshot()
    with mock.patch.object(requests.sessions.Session, 'request', request):
        result = unittest.TextTestRunner(resultclass=AuditResult).run(unittest.TestSuite(selected))
    cache_unchanged = cache_snapshot() == cache_before
    unexpected_http = [attempt for attempt in http_attempts
                       if not (attempt['test'] == 'tests.test_boundary_messages.PolicyQuestionsAreUnaffectedTests.test_such_a_question_produces_no_boundary_message'
                               and attempt['method'].lower() == 'post'
                               and attempt['url'] == 'http://localhost:11434/api/embed')]
    report = {
        'tests_discovered': len(discovered), 'tests_selected': len(selected),
        'tests_run': result.testsRun, 'excluded': excluded,
        'failures': len(result.failures), 'errors': len(result.errors),
        'skipped': len(result.skipped), 'seconds': round(time.perf_counter()-started, 3),
        'successful': result.wasSuccessful(),
        'blocked_unmocked_http_attempts': http_attempts,
        'unexpected_http_attempts': unexpected_http,
        'blocked_socket_attempts': socket_attempts,
        'blocked_persistent_database_attempts': database_attempts,
        'interpreter': sys.executable,
        'persistent_embedding_cache_unchanged': cache_unchanged,
        'persistent_embedding_cache_entries': len(cache_before),
        'isolation': 'Read-only M3 embedding cache; temporary V1 import database; real HTTP/socket and repository persistent DB blocked. Only two live DeepSeek tests excluded. Known legacy V1 HTTP attempts reported, not repaired.'
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result.wasSuccessful() and cache_unchanged and not unexpected_http and not socket_attempts and not database_attempts else 1)
