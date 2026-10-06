#!/usr/bin/env python3
"""Isolated production-path replay. Scripted output is NOT model measurement."""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import inspect
import importlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from contextlib import contextmanager, asynccontextmanager, ExitStack
from datetime import datetime, date
from zoneinfo import ZoneInfo
from unittest.mock import patch
from types import SimpleNamespace
from urllib.parse import urlsplit
from contextvars import ContextVar
import stat

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'eval/router-update-20261005'
_ISOLATION_ROOT = None
EVALUATION_TIMEZONE = 'America/Los_Angeles'
_REAL_WALL = time.time
_REAL_MONOTONIC = time.monotonic
_RUNTIME_PHASE = ContextVar('evaluation_runtime_phase', default=None)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SOURCES = {
    'summarize_emails': 'email', 'view_emails': 'email',
    'summarize_messages': 'messages', 'view_messages': 'messages',
    'get_upcoming': 'calendar', 'get_past_events': 'calendar',
    'get_reminders': 'reminders', 'search_reminders': 'reminders', 'search_notes': 'notes',
    'web_search': 'public', 'web_fetch': 'public', 'get_weather': 'public',
    'get_stock_price': 'public', 'find_free_time': 'calendar',
}
EFFECTS = frozenset({'send_message', 'send_email', 'reply_to_email', 'forward_email',
    'schedule_send', 'draft_message', 'draft_email', 'add_reminder', 'update_reminder',
    'add_calendar_event', 'cancel_event', 'delete_path', 'write_file'})


def digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def read_cases(split):
    path = DATA / f'{split}.jsonl'
    manifest = json.loads((DATA / 'seal.json').read_text())
    if digest_bytes(path.read_bytes()) != manifest['splits'][split]['sha256']:
        raise ValueError(f'{split} corpus seal mismatch; refusing evaluation')
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if len(rows) != manifest['splits'][split]['count']:
        raise ValueError('corpus count mismatch')
    return rows


@contextmanager
def evaluation_timezone():
    """Reversible process-local TZ contract; synchronous, dedicated worker only.

    The production resolver creates naive midnights, whose timestamp() uses
    process local time even when its injected `now` has an explicit offset.
    Never use this helper concurrently with other local-time consumers.
    """
    if not hasattr(time, 'tzset'):
        raise RuntimeError('Evaluation requires time.tzset for its explicit timezone contract')
    previous = os.environ.get('TZ')
    try:
        os.environ['TZ'] = EVALUATION_TIMEZONE
        time.tzset()
        yield
    finally:
        if previous is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = previous
        time.tzset()


def resolve_evaluation_span(scope, *, clock):
    """Use the real resolver with the same local zone as production-path replay."""
    from service.tools.timeranges import resolve_span
    with evaluation_timezone():
        now = datetime.fromisoformat(clock).astimezone(ZoneInfo(EVALUATION_TIMEZONE))
        return resolve_span(scope, now=now)


def canonical_args(name, args, schemas, *, clock=None):
    """Only remove actual runtime schema defaults, preserving literals/types."""
    defaults = schemas.get(name, {}).get('properties', {})
    args = dict(args)
    # These tools feed day and period into the same resolve_span runtime.
    # Equality of exact resolved boundaries is the only accepted date equivalence.
    if clock and name in {'summarize_emails', 'view_emails', 'summarize_messages', 'view_messages', 'search_notes'}:
        scope = args.get('period') or args.get('day')
        if scope:
            try:
                span = resolve_evaluation_span(scope, clock=clock)
            except (ValueError, TypeError):
                pass
            else:
                args.pop('day', None)
                args.pop('period', None)
                args['_resolved_span'] = list(span[:2])
    return {k: v for k, v in args.items()
            if not (k in defaults and 'default' in defaults[k]
                    and type(v) is type(defaults[k]['default'])
                    and v == defaults[k]['default'])}


def runtime_constraint_issues(case, calls):
    """Known behavior from actual baseline tool implementations, not schemas."""
    issues = []
    for call in calls:
        name, args = call['name'], call['args']
        count_requested = any(c['name'] == name and 'count' in c['args'] for c in case['expected'].get('calls', []))
        if name == 'summarize_emails':
            if args.get('unread') and (args.get('day') or args.get('period')):
                issues.append('email-unread-ignores-date-scope')
            if count_requested and args.get('unread') and args.get('count', 20) < 50:
                issues.append('email-unread-count-clamped-to-at-least-50')
            if count_requested and not args.get('unread') and (args.get('day') or args.get('period')):
                issues.append('email-date-scope-ignores-count')
        if name == 'summarize_messages' and not args.get('conversation') and count_requested and (args.get('day') or args.get('period')):
            issues.append('message-date-scope-ignores-count')
        if name == 'get_upcoming' and args.get('period') and case.get('clock'):
            with evaluation_timezone():
                now = datetime.fromisoformat(case['clock']).astimezone(ZoneInfo(EVALUATION_TIMEZONE))
            try:
                _, end, _ = resolve_evaluation_span(args['period'], clock=case['clock'])
            except (ValueError, TypeError):
                issues.append('unresolved-calendar-period')
            else:
                if end <= now.timestamp():
                    issues.append('upcoming-tool-cannot-read-elapsed-calendar-window')
    return sorted(set(issues))


def same_typed(left, right):
    """JSON equality without Python's bool/int or int/float coercion."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_typed(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same_typed(a, b) for a, b in zip(left, right))
    return left == right


def advertised_message_guard(schemas):
    # Runtime-default augmentation adds only `default`, never schema types.
    # Require an actual Boolean registration, not a signature-only parameter.
    prop = schemas.get('view_messages', {}).get('properties', {}).get('strict_match', {})
    if prop.get('type') != 'boolean':
        return False
    if 'const' in prop and prop['const'] is not True:
        return False
    if 'enum' in prop and not any(value is True for value in prop['enum']):
        return False
    return True


def request_arguments_match(calls, gold, normalized, desired, schemas):
    """One bounded addition: registered view_messages query guard=True.

    Explicit gold fields remain authoritative. This is a request-contract metric,
    not a declaration that guarded and broadening tool behavior are equivalent.
    """
    if len(calls) != len(gold):
        return False
    for actual, expected, actual_norm, expected_norm in zip(calls, gold, normalized, desired):
        if same_typed(actual_norm, expected_norm):
            continue
        args, expected_args = actual['args'], expected['args']
        if (actual['name'] != 'view_messages' or expected['name'] != 'view_messages'
                or 'strict_match' in expected_args or args.get('strict_match') is not True
                or not advertised_message_guard(schemas)
                or not isinstance(expected_args.get('query'), str) or not expected_args['query']
                or not same_typed(args.get('query'), expected_args['query'])):
            return False
        amended = {'name': actual_norm['name'], 'args': dict(actual_norm['args'])}
        amended['args'].pop('strict_match', None)
        if not same_typed(amended, expected_norm):
            return False
    return True


def query_scope_guard(calls, gold, schemas):
    applicable = []
    for index, expected in enumerate(gold):
        args = expected['args']
        if expected['name'] != 'view_messages' or not isinstance(args.get('query'), str) or not args['query']:
            continue
        # Explicit False requests the legacy contract: no guarded-success credit.
        if 'strict_match' in args and args['strict_match'] is False:
            continue
        applicable.append((index, expected))
    if not applicable:
        return None
    if not advertised_message_guard(schemas):
        return False
    return all(index < len(calls) and calls[index]['name'] == 'view_messages'
               and same_typed(calls[index]['args'].get('query'), expected['args']['query'])
               and calls[index]['args'].get('strict_match') is True
               for index, expected in applicable)


def evaluation_path(events):
    for event in events:
        if event.get('type') == 'routed':
            disposition = event.get('intent_disposition')
            if isinstance(disposition, str) and disposition in {'compiled', 'clarify', 'declined'}:
                return 'intent_' + disposition
            return event.get('route_source', 'router_agent')
    return ('workflow' if any(event.get('type') == 'workflow' for event in events) else
            'task' if any(event.get('type') == 'task_plan' for event in events) else 'shortcut_or_read')


def score(case, run):
    expected = case['expected']
    calls = run['executed_calls']
    actual_sources = {SOURCES[c['name']] for c in calls if c['name'] in SOURCES}
    if any(c['name'] == 'get_upcoming' and not c['args'].get('calendar_only', False) for c in calls):
        actual_sources.add('reminders')
    actual_sources = sorted(actual_sources)
    required = sorted(expected.get('sources', []))
    allowed_calls = expected.get('calls', [])
    schemas = run.get('schemas', {})
    normalized = [{'name': c['name'], 'args': canonical_args(c['name'], c['args'], schemas, clock=case.get('clock'))} for c in calls]
    desired = [{'name': c['name'], 'args': canonical_args(c['name'], c['args'], schemas, clock=case.get('clock'))} for c in allowed_calls]
    offered = run.get('offered_tools')  # None means no model menu was needed.
    menu_required = {c['name'] for c in allowed_calls}
    exclusions = set(expected.get('excluded_sources', []))
    forbidden = sorted(exclusions & set(actual_sources))
    effects = [c for c in calls if c['name'] in EFFECTS]
    fingerprints = [json.dumps(c, sort_keys=True) for c in normalized]
    answer = run.get('answer', '')
    # Fact checks are exact line-label/value pairs, not substring credit.
    fact_pairs = re.findall(r'(?m)^([A-Za-z][A-Za-z ]*):\s*([^\n]+)$', answer)
    fact_gold = expected.get('answer_facts', {})
    literals = expected.get('answer_literals', [])
    literal_tokens = re.findall(r'(?<![\w@.+-])[A-Za-z0-9][A-Za-z0-9@.+_-]*(?![\w@.+-])', answer)
    factual = all([value for key, value in fact_pairs if key == k] == [v] for k, v in fact_gold.items())
    literal_ok = all(value in literal_tokens for value in literals)
    failure_results = [e for e in run.get('events', []) if e.get('type') == 'tool_result'
                       and (e.get('status') in {'failed', 'denied', 'unsupported', 'needs_input'}
                            or re.match(r'(?i)^\(?error[: ]', str(e.get('result', ''))))]
    failure_ack = bool(re.search(r'(?i)\b(?:unable|unavailable|cannot|failed|blocked|denied|could[’\']?nt|couldn[’\']t|didn[’\']t|not sent|did not|could not|not checked)\b', answer))
    errors = any(e.get('type') == 'error' for e in run.get('events', []))
    exact = normalized == desired
    results = {e.get('id'): e for e in run.get('events', []) if e.get('type') == 'tool_result'}
    planned_only = any(e.get('status') == 'planned' for e in results.values())
    runtime_issues = runtime_constraint_issues(case, calls)
    scored = {
        'runtime_constraints': not runtime_issues, 'source': actual_sources == required, 'excluded_source': not forbidden,
        'menu': None if offered is None else menu_required <= set(offered),
        'first_call': None if not desired else bool(normalized) and normalized[0] == desired[0],
        'exact_arguments': exact, 'effects': len(effects) == expected.get('effects', 0),
        'literal_fidelity': literal_ok if literals else None,
        'answer_facts': factual if fact_gold else None,
        'failure_honesty': failure_ack if failure_results else None,
        'duplicate': len(fingerprints) == len(set(fingerprints)),
        'completed': not errors and not planned_only and any(e.get('type') == 'done' for e in run.get('events', [])),
    }
    scored['end_to_end'] = all(v for v in scored.values() if v is not None)
    # Preserve the original three metrics before adding this frozen amendment.
    original_gates = {key: value for key, value in scored.items()
                      if key not in {'exact_arguments', 'first_call', 'end_to_end'}}
    scored['request_argument'] = request_arguments_match(calls, allowed_calls, normalized, desired, schemas)
    scored['query_scope_guard'] = query_scope_guard(calls, allowed_calls, schemas)
    scored['guarded_end_to_end'] = (all(value for value in original_gates.values() if value is not None)
                                  and scored['request_argument']
                                  and scored['query_scope_guard'] is not False)
    return {'metrics': scored, 'actual_sources': actual_sources,
            'forbidden_sources': forbidden, 'runtime_constraint_issues': runtime_issues, 'expected_calls': desired, 'actual_calls': normalized}


def aggregate(rows):
    names = sorted({k for row in rows for k in row['score']['metrics']})
    return {name: {'numerator': sum(row['score']['metrics'].get(name) is True for row in rows),
                   'denominator': sum(row['score']['metrics'].get(name) is not None for row in rows)} for name in names}


def scripted_intent(case):
    """Oracle interpretation derived from sealed annotations, NOT a prediction."""
    sources = []
    for call in case['expected'].get('calls', []):
        name, args = call['name'], call['args']
        domain = SOURCES.get(name)
        if domain not in {'calendar', 'reminders', 'email', 'messages', 'notes'}:
            return {'version': 1, 'kind': 'none', 'sources': [], 'excluded_sources': [], 'unsupported_constraints': []}
        source = {'domain': domain, 'operation': 'free_time' if name == 'find_free_time' else 'overview' if name.startswith('summarize_') or name == 'get_upcoming' else 'records'}
        for key in ('query', 'account', 'unread', 'count', 'conversation', 'minutes', 'scope'):
            if key in args:
                source[key] = args[key]
        period = args.get('period') or args.get('day')
        if period:
            source['time'] = {'date' if re.fullmatch(r'\d{4}-\d{2}-\d{2}', period) else 'named': period}
        elif 'days' in args:
            source['time'] = {'last_n_days' if name == 'get_past_events' else 'rolling_days': args['days']}
        sources.append(source)
        if name == 'get_upcoming' and not args.get('calendar_only', False) and 'reminders' in case['expected'].get('sources', []):
            reminder = {'domain': 'reminders', 'operation': 'overview'}
            if 'time' in source:
                reminder['time'] = dict(source['time'])
            sources.append(reminder)
    exclusions = list(case['expected'].get('excluded_sources', []))
    if any(c['name'] == 'get_upcoming' and c['args'].get('calendar_only') for c in case['expected'].get('calls', [])) and 'reminders' not in exclusions:
        exclusions.append('reminders')
    return {'version': 1, 'kind': 'read' if sources else 'none', 'sources': sources,
            'excluded_sources': exclusions, 'unsupported_constraints': []}


@dataclass(frozen=True)
class InferenceGrant:
    """Future explicit resource grant; never enabled by the CLI or by default."""
    model: str
    revision: str
    checkpoint_sha256: str
    approval_receipt: str
    endpoint: tuple[str, int]
    enabled: bool = False

    def validate(self):
        if not self.enabled:
            raise PermissionError('Injected inference is disabled')
        if ('Ling' not in self.model or not self.revision or not self.approval_receipt
                or not re.fullmatch(r'[0-9a-f]{64}', self.checkpoint_sha256)
                or self.endpoint[0] not in {'127.0.0.1', '::1'}
                or not 1 <= self.endpoint[1] <= 65535):
            raise PermissionError('A complete, exact loopback resident-Ling grant is required')


class RuntimeCapability:
    """Explicit future execution lease. Merely implementing this grants no runtime.

    Original home is captured, never supplied as an arbitrary read-root. The
    private phases are entered only by the supported factory/client below.
    """
    def __init__(self, grant, *, end_utc, execution_receipt, enabled=False):
        self.grant = grant
        self.enabled = enabled
        self.execution_receipt = execution_receipt
        self.home = Path.home().resolve()
        end = datetime.fromisoformat(end_utc.replace('Z', '+00:00'))
        if end.tzinfo is None:
            raise PermissionError('Execution expiry requires an explicit timezone')
        self.end_wall = end.timestamp()
        self.stop_wall = self.end_wall - 60  # Owned inspection/transport cleanup reserve.
        self.stop_monotonic = _REAL_MONOTONIC() + self.stop_wall - _REAL_WALL()
        self.revoked = False
        self.installed_state = None
        self.lock_identity = None
        self.raw = []
        self._busy = False

    def check(self):
        self.grant.validate()
        if (not self.enabled or not self.execution_receipt or self.revoked
                or self.grant.endpoint != ('127.0.0.1', 8000)
                or _REAL_WALL() >= self.stop_wall or _REAL_MONOTONIC() >= self.stop_monotonic):
            raise PermissionError('A live exact desktop execution capability is required')

    @contextmanager
    def _phase(self, phase):
        if phase != 'cleanup':
            self.check()
        token = _RUNTIME_PHASE.set((self, phase))
        try:
            yield
        finally:
            _RUNTIME_PHASE.reset(token)

    def _current_phase(self):
        active = _RUNTIME_PHASE.get()
        return active[1] if active and active[0] is self else None

    def _real_home(self):
        return self._current_phase() in {'configure', 'construct', 'peer', 'cleanup'}

    def _qualified_directory(self):
        path = self.home / '.moe'
        info = path.lstat()
        if (path.resolve() != path or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid() or info.st_mode & 0o022):
            raise PermissionError('Unqualified existing credential directory')
        return path

    @staticmethod
    def _fd_snapshot(fd):
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096):
            raise PermissionError('Unqualified existing credential lock')
        raw = os.read(fd, info.st_size + 1)
        stable = lambda value: (value.st_dev, value.st_ino, value.st_mode, value.st_uid,
                                value.st_gid, value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if len(raw) != info.st_size or stable(os.fstat(fd)) != stable(info):
            raise PermissionError('Changed existing credential lock')
        return stable(info), hashlib.sha256(raw).digest()

    def _lock_snapshot(self):
        path = self._qualified_directory() / '.provisioning.lock'
        # Read through the guard: no symlink, replacement, creation or writable fd.
        if path.resolve() != path:
            raise PermissionError('Unqualified existing credential lock')
        fd = self._original_open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            return self._fd_snapshot(fd)
        finally:
            os.close(fd)

    def _check_lock(self):
        current = self._lock_snapshot()
        if self.lock_identity is None:
            self.lock_identity = current
        elif current != self.lock_identity:
            raise PermissionError('Existing credential lock changed')

    def _read_allowed(self, path):
        phase = self._current_phase()
        if phase is None:
            return False
        if phase != 'cleanup':
            self.check()
        config = {self.home / '.moe/config.yaml', self.home / '.omlx/settings.json',
                  self.home / '.omlx/model_settings.json'}
        recovery = {self.home / '.moe/.credential-generation',
                    self.home / '.moe/.helper-transaction.json',
                    self.home / '.moe/.provisioning.lock'}
        authority = {self.home / '.moe/omlx-runtime-authorization.json'}
        allowed = config | recovery | authority if phase in {'configure', 'construct'} else recovery | authority
        if phase == 'cleanup':
            allowed = recovery
        return path in allowed and path.resolve() == path

    @staticmethod
    def _argv_allowed(argv):
        fixed = (
            ['/usr/sbin/lsof', '-nP', '-a', '-iTCP:8000', '-sTCP:LISTEN', '-Fpufn'],
            ['/usr/sbin/lsof', '-nP', '-a', '-iTCP', '-sTCP:LISTEN', '-Fpufn'],
            ['/usr/sbin/lsof', '-nP', '-a', '-iTCP:8000', '-sTCP:ESTABLISHED', '-FpufPtTn', '-Ts'],
        )
        return (argv in fixed or isinstance(argv, list) and len(argv) == 6
                and argv[:3] == ['/bin/ps', '-ww', '-p']
                and isinstance(argv[3], str) and re.fullmatch(r'[1-9][0-9]*', argv[3]) is not None
                and int(argv[3]) <= 2**31 - 1 and argv[4:] == ['-o', 'ppid=,uid=,comm='])

    def _process_allowed(self, args):
        if self._current_phase() != 'peer':
            return False
        self.check()
        executable, argv, cwd, env = args
        if (not self._argv_allowed(argv) or executable != argv[0] or cwd is not None
                or env not in ({'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C'},
                               {'PATH': '/usr/bin:/bin:/usr/sbin', 'LC_ALL': 'C'})):
            return False
        # Exact trusted production call sites, not an executable-name allowance.
        frame = inspect.currentframe()
        try:
            while frame:
                if ((frame.f_code.co_filename == str(ROOT / 'service/inference/attributed_transport.py')
                     and frame.f_code.co_name == 'inspect_command')
                        or (frame.f_code.co_filename == str(ROOT / 'service/inference/local_peer.py')
                            and frame.f_code.co_name == 'tcp_listeners')):
                    return True
                frame = frame.f_back
        finally:
            del frame
        return False

    def _request_allowed(self, request):
        self.check()
        if self._current_phase() != 'peer':
            raise PermissionError('HTTP dispatch is outside the private runtime phase')
        url = request.url
        if (str(url.copy_with(path='', query=None, fragment=None)).rstrip('/') != 'http://127.0.0.1:8000'
                or url.username or url.password or url.query or url.fragment):
            raise PermissionError('HTTP origin differs from the exact runtime capability')
        if request.method == 'GET' and url.path == '/v1/models/status':
            return
        if request.method == 'POST' and url.path == '/v1/chat/completions':
            try:
                body = json.loads(request.content)
            except Exception:
                raise PermissionError('Completion request must be bounded JSON') from None
            if isinstance(body, dict) and body.get('model') == self.grant.model:
                return
        raise PermissionError('HTTP operation is outside status/synthetic completion scope')

    def _validate_target(self, target):
        self.check()
        ep = target.endpoint
        if (target.role != 'router' or target.model != self.grant.model
                or target.revision and target.revision != self.grant.revision
                or ep.name != 'local' or ep.base_url.rstrip('/') != 'http://127.0.0.1:8000'
                or ep.managed is not True or ep.provider != 'omlx' or ep.api_prefix != '/v1'
                or ep.credential_ref != 'local_omlx'):
            raise PermissionError('Actual configuration is outside desktop runtime scope')


class _ScopedResidentClient:
    """Only supported real-client delegation; no independent auth or authority."""
    managed = True

    def __init__(self, client, capability):
        self._client, self._capability = client, capability
        async def request_hook(request):
            capability._request_allowed(request)
        client._client.event_hooks.setdefault('request', []).append(request_hook)

    @property
    def target(self): return self._client.target
    @property
    def base_url(self): return self._client.base_url
    @property
    def endpoint_name(self): return self._client.endpoint_name
    @property
    def provider(self): return self._client.provider
    @property
    def api_prefix(self): return self._client.api_prefix
    @property
    def _credential_transport(self): return self._client._credential_transport

    @asynccontextmanager
    async def _operation(self):
        cap = self._capability
        cap.check()
        if cap._busy:
            raise PermissionError('Evaluation permits only one owned runtime request')
        cap._busy = True
        try:
            with cap._phase('peer'):
                cap._check_lock()
                async with asyncio.timeout(cap.stop_monotonic - _REAL_MONOTONIC()):
                    yield
                cap.check()
        finally:
            cap._busy = False
            with cap._phase('cleanup'):
                cap._check_lock()

    async def status(self):
        async with self._operation():
            return await self._client.status()

    async def chat(self, model, messages, **kwargs):
        if model != self._capability.grant.model:
            raise PermissionError('Completion model differs from the runtime grant')
        record = {'kind': 'runtime_chat', 'model': model, 'messages': copy.deepcopy(messages),
                  'kwargs': copy.deepcopy(kwargs), 'outcome': 'pending'}
        self._capability.raw.append(record)
        try:
            async with self._operation():
                response = await self._client.chat(model, messages, **kwargs)
            record.update(outcome='completed', response=copy.deepcopy(response))
            return response
        except BaseException as error:
            record['outcome'] = type(error).__name__  # Never stringify auth/client exceptions.
            raise

    async def stream_events(self, model, messages, **kwargs):
        if model != self._capability.grant.model:
            raise PermissionError('Completion model differs from the runtime grant')
        record = {'kind': 'runtime_stream', 'model': model, 'messages': copy.deepcopy(messages),
                  'kwargs': copy.deepcopy(kwargs), 'outcome': 'pending', 'events': []}
        self._capability.raw.append(record)
        try:
            async with self._operation():
                stream = self._client.stream_events(model, messages, **kwargs)
                try:
                    async for event in stream:
                        record['events'].append(copy.deepcopy(event))
                        yield event
                finally:
                    await stream.aclose()
            record['outcome'] = 'completed'
        except BaseException as error:
            record['outcome'] = type(error).__name__
            raise

    async def aclose(self):
        with self._capability._phase('cleanup'):
            await self._client.aclose()


@asynccontextmanager
async def supported_resident_client(capability):
    """Future explicitly admitted factory. Never called by the scripted CLI.

    Disk/hash provenance supplied in InferenceGrant still needs independently
    verified loaded-instance binding; this factory does not invent that proof.
    """
    cap = capability
    cap.check()
    if cap.installed_state is None or cap.installed_state != _ISOLATION_ROOT:
        raise PermissionError('Install the exact runtime capability before service imports')
    from service import config as cfg
    from service.config import quarantine
    from service.config.endpoints import role_target
    from service.inference.omlx_client import OMLXClient
    import yaml

    def clear_caches():
        for function in (cfg.omlx_settings, cfg.models_config, cfg._user_overlay):
            function.cache_clear()

    state = cap.installed_state
    client = None
    saved_state = {}
    try:
        # Capture through existing configuration APIs, not caller-authored identity.
        with cap._phase('configure'), ExitStack() as stack:
            cap._check_lock()
            if (cap.home / '.moe/omlx-runtime-authorization.json').exists():
                raise PermissionError('Managed runtime authorization requires separate admission')
            for name, path in (('USER_CONFIG', cap.home / '.moe/config.yaml'),
                               ('OMLX_SETTINGS', cap.home / '.omlx/settings.json'),
                               ('OMLX_MODEL_SETTINGS', cap.home / '.omlx/model_settings.json')):
                stack.enter_context(patch.object(cfg, name, path))
            clear_caches()
            try:
                if cfg.USER_CONFIG.exists():
                    overlay = yaml.safe_load(cfg.USER_CONFIG.read_text())
                    if overlay is not None and not isinstance(overlay, dict):
                        raise PermissionError('Invalid actual configuration overlay')
                target = role_target('router')
                cap._validate_target(target)
                targets = {role: role_target(role) for role in ('router', 'fast', 'agent', 'general', 'coding', 'reasoning')}
                if any(t.identity != target.identity for t in targets.values()):
                    raise PermissionError('A generation role requires a different model/endpoint')
                effective = cfg.models_config()
                projection = {key: copy.deepcopy(effective[key]) for key in
                              ('tool_capable', 'no_thinking_capable', 'narration') if key in effective}
                projection['roles'] = {role: t.model for role, t in targets.items()}
                if effective['default_role'] not in targets:
                    raise PermissionError('Default generation role is outside the captured roster')
                projection['default_role'] = effective['default_role']
                projection['inference'] = {'endpoints': {'local': {
                    'base_url': target.endpoint.base_url, 'credential_ref': target.endpoint.credential_ref,
                    'provider': target.endpoint.provider, 'api_prefix': target.endpoint.api_prefix,
                    'readiness_timeout': target.endpoint.readiness_timeout}}, 'bindings': {
                    role: {'endpoint': 'local', 'model_id': t.model, 'revision': t.revision, 'profile': t.profile,
                           'context_window': t.context_window, 'qualified_capabilities': list(t.capabilities),
                           'dimensions': t.dimensions} for role, t in targets.items()}}
                context_window = cfg.model_context_window(target.model)
            finally:
                clear_caches()
        # Only nonsecret identity/roster projection is written into synthetic state.
        (state / '.moe').mkdir(exist_ok=True)
        for path in (state / '.moe/config.yaml', state / '.omlx/model_settings.json'):
            saved_state[path] = path.read_bytes() if path.exists() else None
        (state / '.moe/config.yaml').write_text(yaml.safe_dump(projection))
        (state / '.omlx/model_settings.json').write_text(json.dumps({'models': {
            target.model: {'max_context_window': context_window}}}))
        clear_caches()
        if role_target('router') != target:
            raise PermissionError('Isolated target differs from captured actual configuration')
        # Genuine normal gate with actual recovery state; normal transport captures it.
        gate = quarantine.RecoveryGate(home=lambda: cap.home)
        gate.callbacks.extend(quarantine._gate.callbacks)
        with cap._phase('construct'), ExitStack() as stack:
            stack.enter_context(patch.object(cfg, 'OMLX_SETTINGS', cap.home / '.omlx/settings.json'))
            stack.enter_context(patch.object(quarantine, '_gate', gate))
            clear_caches()
            try:
                gate.check()
                client = OMLXClient(target=target)
            finally:
                clear_caches()
        from service.router.intent.planner import _client_matches_target
        if not _client_matches_target(client, target):
            raise PermissionError('Supported client identity differs from actual target')
        wrapped = _ScopedResidentClient(client, cap)
        yield wrapped
    finally:
        try:
            if client is not None:
                with cap._phase('cleanup'):
                    await client.aclose()
                    cap._check_lock()
        finally:
            cap.revoked = True
            for path, previous in saved_state.items():
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(previous)
            clear_caches()


class ResidentInferenceAdapter:
    """Optional injected client, independent of the always-synthetic executor.

    No construction, start, load, swap, unload, or arbitrary endpoint methods.
    Caller must coordinate resources and provide frozen model provenance first.
    """
    managed = True

    def __init__(self, client, grant):
        self._client, self.grant = client, grant
        self.raw = []

    def _validated_target(self):
        self.grant.validate()
        # Resolve only the caller's isolated configuration, never real settings.
        if (_ISOLATION_ROOT is None or Path.home().resolve() != _ISOLATION_ROOT
                or not os.environ.get('WISP_HOME')
                or Path(os.environ['WISP_HOME']).resolve() != _ISOLATION_ROOT / '.moe'):
            raise PermissionError('Injected inference requires the isolated evaluation guard')
        try:
            from service.paths import MOE_DIR
            from service.config.endpoints import role_target
            from service.router.intent.planner import _client_matches_target
            if MOE_DIR.resolve() != (_ISOLATION_ROOT / '.moe').resolve():
                raise PermissionError('Role configuration is outside isolated evaluation state')
            target = role_target('router')
            origin = urlsplit(target.endpoint.base_url)
            if (target.model != self.grant.model
                    or (origin.hostname, origin.port) != self.grant.endpoint
                    or target.revision and target.revision != self.grant.revision
                    or not _client_matches_target(self._client, target)):
                raise PermissionError('Supplied client identity does not match the frozen grant and router configuration')
            return target
        except PermissionError:
            raise
        except Exception:
            raise PermissionError('Strict isolated router identity cannot be verified') from None

    def _borrowed_metadata(self, name):
        self._validated_target()
        # Exact references only. No credentials, transport copying, or authority.
        return getattr(self._client, name)

    @property
    def target(self):
        return self._borrowed_metadata('target')

    @property
    def base_url(self):
        return self._borrowed_metadata('base_url')

    @property
    def endpoint_name(self):
        return self._borrowed_metadata('endpoint_name')

    @property
    def provider(self):
        return self._borrowed_metadata('provider')

    @property
    def api_prefix(self):
        return self._borrowed_metadata('api_prefix')

    @property
    def _credential_transport(self):
        return self._borrowed_metadata('_credential_transport')

    async def status(self):
        self._validated_target()
        return await self._client.status()

    async def ensure_only(self, model, **kwargs):
        self.grant.validate()
        if model != self.grant.model:
            raise PermissionError('Model differs from the frozen inference grant')
        status = await self.status()
        if not any(item.get('id') == model and item.get('loaded') is True for item in status.get('models', [])):
            raise PermissionError('Granted model is not already resident; evaluation never loads it')

    async def chat(self, model, messages, **kwargs):
        await self.ensure_only(model)
        self._validated_target()  # Recheck identity after awaited residency status.
        response = await self._client.chat(model, messages, **kwargs)
        self.raw.append({'kind': 'resident_injected_chat', 'model': model,
                         'messages': copy.deepcopy(messages), 'kwargs': copy.deepcopy(kwargs),
                         'response': copy.deepcopy(response)})
        return response

    async def stream_events(self, model, messages, **kwargs):
        await self.ensure_only(model)
        self._validated_target()  # Recheck identity after awaited residency status.
        record = {'kind': 'resident_injected_generation', 'model': model,
                  'messages': copy.deepcopy(messages), 'kwargs': copy.deepcopy(kwargs), 'response_events': []}
        self.raw.append(record)
        stream = self._client.stream_events(model, messages, **kwargs)
        try:
            async for event in stream:
                record['response_events'].append(copy.deepcopy(event))
                yield event
        finally:
            await stream.aclose()


class ScriptedClient:
    """Independent replay responses, never invokes or loads a model."""
    managed = True

    def __init__(self, case):
        from service.config.endpoints import role_target
        import httpx
        self.target = role_target('router')
        self.base_url = self.target.endpoint.base_url.rstrip('/')
        self.managed = self.target.endpoint.managed
        self.endpoint_name = self.target.endpoint.name
        self.api_prefix = self.target.endpoint.api_prefix
        self.provider = SimpleNamespace(name=self.target.endpoint.provider)
        # Identity-only fixture metadata. No transport, key loader, or socket.
        self._credential_transport = SimpleNamespace(origin=httpx.URL(self.base_url), backend=object())
        self.case = case
        self.raw = []
        self.step = 0

    async def status(self):
        return {'models': [{'id': self.target.model, 'loaded': True}]}

    async def ensure_only(self, model, **kwargs):
        self.raw.append({'kind': 'fixture_readiness', 'model': model})

    async def chat(self, model, messages, **kwargs):
        intent = self.case.get('intent_response') or scripted_intent(self.case)
        if intent is None:
            raise RuntimeError('No scripted intent response supplied; this is not live inference')
        response = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(intent)}}]}
        self.raw.append({'kind': 'scripted_intent', 'model': model, 'messages': copy.deepcopy(messages),
                         'kwargs': kwargs, 'response': response})
        return response

    async def stream_events(self, model, messages, **kwargs):
        script = self.case.get('model_script', [])
        if self.step < len(script):
            message = copy.deepcopy(script[self.step])
        else:
            message = {'role': 'assistant', 'content': self.case.get('fixture_answer', 'Synthetic fixture answer.')}
        self.step += 1
        self.raw.append({'kind': 'scripted_generation', 'model': model, 'messages': copy.deepcopy(messages),
                         'kwargs': copy.deepcopy(kwargs), 'response': message})
        if message.get('content'):
            yield {'kind': 'content', 'text': message['content']}
        yield {'kind': 'final', 'message': message}


def install_guard(state, output, *, inference_grant=None, runtime_capability=None):
    """Process-wide guard before service imports. Dedicated worker only."""
    if any(name == 'service' or name.startswith('service.') for name in sys.modules):
        raise RuntimeError('Service already imported: isolation cannot be established')
    if inference_grant is not None:
        inference_grant.validate()
    if runtime_capability is not None:
        runtime_capability.check()
        if inference_grant != runtime_capability.grant or runtime_capability.home != Path.home().resolve():
            raise PermissionError('Runtime capability/grant/home mismatch')
        if 'WISP_CREDENTIAL_PIPE' in os.environ or 'WISP_CREDENTIAL_GENERATION' in os.environ:
            raise PermissionError('Evaluation must not inherit a native credential bridge')
    os.environ['WISP_HOME'] = str(state / '.moe')
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    if not hasattr(time, 'tzset'):
        raise RuntimeError('Evaluation requires time.tzset for its explicit timezone contract')
    os.environ['TZ'] = EVALUATION_TIMEZONE
    time.tzset()
    sys.dont_write_bytecode = True
    real_home = Path.home().resolve()
    allowed_reads = [ROOT.resolve(), Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), state.resolve()]
    allowed_writes = [state.resolve(), output.resolve()]
    Path.home = classmethod(lambda cls: runtime_capability.home
                            if runtime_capability is not None and runtime_capability._real_home() else state)

    def inside(path, roots):
        return any(path == root or root in path.parents for root in roots)

    def audit(event, args):
        if event in {'socket.connect', 'socket.getaddrinfo'}:
            target = args[1] if event == 'socket.connect' else args[:2]
            if inference_grant is None or tuple(target[:2]) != inference_grant.endpoint:
                raise PermissionError(f'evaluation denies {event}')
            if runtime_capability is not None:
                runtime_capability.check()
                if runtime_capability._current_phase() != 'peer':
                    raise PermissionError('Socket access requires the private runtime phase')
        if event in {'subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn'}:
            if not (event == 'subprocess.Popen' and runtime_capability is not None
                    and runtime_capability._process_allowed(args)):
                raise PermissionError(f'evaluation denies {event}')
        if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).resolve()
            mode, flags = args[1], args[2]
            writing = isinstance(mode, str) and any(c in mode for c in 'wax+') or isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            if writing and not inside(path, allowed_writes):
                raise PermissionError(f'evaluation denies write outside synthetic/output state: {path}')
            original = Path(os.fsdecode(args[0])).absolute()
            if runtime_capability is not None and original in {real_home / '.moe/config.yaml',
                    real_home / '.omlx/settings.json', real_home / '.omlx/model_settings.json',
                    real_home / '.moe/.provisioning.lock', real_home / '.moe/.credential-generation',
                    real_home / '.moe/.helper-transaction.json',
                    real_home / '.moe/omlx-runtime-authorization.json'} and original != path:
                raise PermissionError('Runtime file capability denies symlinks')
            if (real_home in path.parents or real_home in original.parents) and not inside(path, allowed_reads):
                if writing or runtime_capability is None or not runtime_capability._read_allowed(original):
                    raise PermissionError('evaluation denies personal-home reads')
        if event in {'os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'sqlite3.connect'} and args and isinstance(args[0], (str, bytes, os.PathLike)):
            target = Path(os.fsdecode(args[0])).resolve()
            if not inside(target, allowed_writes):
                raise PermissionError(f'evaluation denies {event} outside synthetic/output state')
        if event in {'os.rename', 'os.replace'}:
            if any(not inside(Path(os.fsdecode(value)).resolve(), allowed_writes) for value in args[:2]):
                raise PermissionError('evaluation denies rename outside synthetic/output state')
    sys.addaudithook(audit)
    if runtime_capability is not None:
        cap = runtime_capability
        cap.installed_state = state.resolve()
        cap._original_open = os.open
        def existing_lock_open(path, flags, mode=0o777, *, dir_fd=None):
            if isinstance(path, (str, bytes, os.PathLike)) and Path(os.fsdecode(path)).absolute() == cap.home / '.moe/.provisioning.lock':
                if cap._current_phase() != 'peer' or dir_fd is not None or flags != (os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW):
                    # Snapshot reads use the original open; arbitrary lock opens stay denied.
                    raise PermissionError('Only the normal existing-lock lease is permitted')
                cap.check()
                cap._check_lock()
                # flock(LOCK_SH) works on a read-only fd. No O_CREAT/writable fd escapes.
                fd = cap._original_open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    if cap._fd_snapshot(fd) != cap.lock_identity:
                        raise PermissionError('Credential lock replaced before lease acquisition')
                    return fd
                except BaseException:
                    os.close(fd)
                    raise
            return cap._original_open(path, flags, mode, dir_fd=dir_fd)
        os.open = existing_lock_open
        original_mkdir = os.mkdir
        def existing_gate_directory(path, mode=0o777, *, dir_fd=None):
            if isinstance(path, (str, bytes, os.PathLike)) and Path(os.fsdecode(path)).absolute() == cap.home / '.moe':
                if cap._current_phase() != 'peer' or mode != 0o700 or dir_fd is not None:
                    raise PermissionError('Only the normal existing credential directory check is permitted')
                cap.check()
                cap._qualified_directory()
                return None  # Never a real mkdir, even if the existing directory disappears.
            return original_mkdir(path, mode, dir_fd=dir_fd)
        os.mkdir = existing_gate_directory
    global _ISOLATION_ROOT
    _ISOLATION_ROOT = state.resolve()
    settings = state / '.omlx/settings.json'
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({'api_key': 'fixture-only', 'host': '127.0.0.1', 'port': 1}))


async def run_case(case, state, *, candidate=False, inference_adapter=None):
    for turn in case.get('context', []):
        if not isinstance(turn, dict) or turn.get('role') not in {'user', 'assistant'} or not isinstance(turn.get('content'), str):
            raise ValueError('context must be explicit role/content objects')
    from service import main
    if candidate and not (ROOT / 'service/router/intent/__init__.py').exists():
        raise RuntimeError('Candidate intent API absent; integrate the core owner commit first')
    if candidate:
        # Import lazily loaded clocks BEFORE freezing service module datetime.
        importlib.import_module('service.router.intent.planner')
        importlib.import_module('service.router.intent.compiler')
    from service.memory import context
    from service.memory.store import SessionStore
    from service.tools import registry
    from service.tools.registry import Tool
    from service.tools import imessage_tools, email_tools, action_tools
    if inference_adapter is not None and not isinstance(inference_adapter, ResidentInferenceAdapter):
        raise TypeError('Only a scoped ResidentInferenceAdapter can replace scripted responses')
    if inference_adapter is not None:
        inference_adapter.grant.validate()
    client = inference_adapter or ScriptedClient(case)
    raw_start = len(client.raw)
    store = SessionStore(state / (case['id'] + '.db'))
    calls = []
    original_registry = dict(registry.REGISTRY)
    schemas = {name: copy.deepcopy(tool.parameters) for name, tool in original_registry.items()}
    for name, tool in original_registry.items():
        for key, parameter in inspect.signature(tool.func).parameters.items():
            if parameter.default is not inspect.Parameter.empty:
                schemas[name].setdefault('properties', {}).setdefault(key, {}).setdefault('default', parameter.default)
    config = copy.deepcopy(main.models_config())
    config['tool_retrieval'] = {'provider': 'lexical'}
    config['intent_router'] = {'enabled': candidate, 'domains': ['calendar', 'reminders', 'email', 'messages', 'notes'], 'deadline_seconds': 2.5, 'repair': True}
    config['super_model'] = {'enabled': False}
    for name, original in original_registry.items():
        async def fixture(*, _name=name, **args):
            frames = inspect.stack(context=0)
            stage = next((str(Path(frame.filename).relative_to(ROOT)) for frame in frames
                          if frame.filename.startswith(str(ROOT / 'service')) and 'registry.py' not in frame.filename), 'unknown')
            calls.append({'name': _name, 'args': copy.deepcopy(args), 'source': SOURCES.get(_name), 'fixture': True, 'execution_stage': stage})
            values = case.get('tool_results', {}).get(_name, [case.get('fixture_answer', 'Synthetic fixture result.')])
            index = sum(c['name'] == _name for c in calls) - 1
            return values[min(index, len(values) - 1)]
        registry.REGISTRY[name] = Tool(name=original.name, description=original.description,
            parameters=original.parameters, category=original.category, func=fixture,
            aliases=original.aliases, unavailable_reason=original.unavailable_reason)
    for name in case.get('unavailable_tools', []):
        registry.REGISTRY.pop(name, None)
    sid = store.create_session()
    # Keep real nested context turns; reject the historically lossy list-of-lists shape.
    for turn in case.get('context', []):
        store.add_turn(sid, turn['role'], turn['content'], tool_digest=turn.get('tool_digest'))

    async def ready():
        client.raw.append({'kind': 'fixture_engine_ready'})

    async def no_summary(*args, **kwargs):
        return None  # Rolling memory generation is outside this benchmark's scope.

    fixed = datetime.fromisoformat(case.get('clock', '2026-10-05T12:00:00-07:00')).astimezone(ZoneInfo(EVALUATION_TIMEZONE))
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
        @classmethod
        def today(cls):
            return cls.now()
    class FrozenDate(date):
        @classmethod
        def today(cls):
            return fixed.date()

    names = ['Avery', 'Kai', 'Mina', 'Jules', 'Noor', 'Ren', 'Sasha', 'Dee']
    def contacts(name):
        if name not in names:
            return []
        number = f'+15550100{100 + names.index(name)}'
        return [{'name': name, 'preferred': number, 'handles': [number, name.lower() + '@example.invalid']}]

    async def unavailable_reply(*args, **kwargs):
        return None, 'Synthetic reply preparation unavailable; nothing was sent.'

    events = []
    try:
        with patch.object(main, 'client', client, create=True), patch.object(main, 'store', store), \
             patch.object(context, 'store', store), patch.object(main, 'models_config', lambda: config), \
             patch.object(main, 'ensure_omlx', ready), patch.object(main, 'maybe_summarize', no_summary), \
             patch.object(main, 'cloud_super_model_enabled', lambda: False), \
             patch.object(imessage_tools, 'find_contacts', contacts), \
             patch.object(email_tools, 'ensure_reply_source', no_summary), \
             patch.object(action_tools, 'prepare_reply_args', unavailable_reply):
            from contextlib import ExitStack
            with ExitStack() as stack:
                if candidate and inference_adapter is None:
                    planner = importlib.import_module('service.router.intent.planner')
                    # Same frozen synthetic role target used by fake identity/status.
                    def fixture_router_target(role):
                        if role != 'router':
                            raise PermissionError('Fixture planner requested a non-router role')
                        return client.target
                    stack.enter_context(patch.object(planner, 'role_target', fixture_router_target))
                stack.enter_context(patch.object(time, 'time', lambda: fixed.timestamp()))
                for module in list(sys.modules.values()):
                    if module and getattr(module, '__name__', '').startswith('service.') and getattr(module, 'datetime', None) is datetime:
                        stack.enter_context(patch.object(module, 'datetime', FrozenDatetime))
                    if module and getattr(module, '__name__', '').startswith('service.') and getattr(module, 'date', None) is date:
                        stack.enter_context(patch.object(module, 'date', FrozenDate))
                response = await main.agent({'prompt': case['prompt'], 'session_id': sid, 'debug': True})
                async with asyncio.timeout(15):
                    async for item in response.body_iterator:
                        event = json.loads((item.decode() if isinstance(item, bytes) else item).removeprefix('data: ').strip())
                        events.append(event)
                        if event.get('type') == 'confirm':
                            # Synthetic confirmation only; callable registry is already entirely replaced.
                            await main.approve({'session_id': sid, 'action_id': event['id'],
                                'request_id': event.get('request_id'), 'approved': case.get('approve', False)})
    finally:
        registry.REGISTRY.clear()
        registry.REGISTRY.update(original_registry)
        store._db.close()
    menus = [entry['kwargs']['tools'] for entry in client.raw[raw_start:] if entry.get('kind') in {'scripted_generation', 'resident_injected_generation'} and entry['kwargs'].get('tools')]
    offered = None if not menus else sorted({tool['function']['name'] for tool in menus[0]})
    answer = '\n'.join(e.get('text', '') for e in events if e.get('type') == 'text')
    if not answer:
        answer = ''.join(e.get('text', '') for e in events if e.get('type') == 'delta')
    return {'id': case['id'], 'events': events, 'executed_calls': calls, 'offered_tools': offered,
            'answer': answer, 'raw_model_io': client.raw[raw_start:], 'schemas': schemas,
            'mode': 'resident-injected-production-path' if inference_adapter else 'scripted-production-path',
            'not_model_measurement': inference_adapter is None,
            'inference_provenance': None if inference_adapter is None else {
                'model': inference_adapter.grant.model, 'revision': inference_adapter.grant.revision,
                'checkpoint_sha256': inference_adapter.grant.checkpoint_sha256,
                'approval_receipt': inference_adapter.grant.approval_receipt}, 'candidate': candidate,
            'clock': case.get('clock'), 'synthetic_contacts': {name: contacts(name)[0] for name in names}, 'context': copy.deepcopy(case.get('context', [])),
            'path': evaluation_path(events)}


def git_revision():
    marker = ROOT / '.git'
    gitdir = marker if marker.is_dir() else Path(marker.read_text().strip().removeprefix('gitdir: '))
    if not gitdir.is_absolute():
        gitdir = ROOT / gitdir
    head = (gitdir / 'HEAD').read_text().strip()
    if not head.startswith('ref: '):
        return head
    ref = head.removeprefix('ref: ')
    common = gitdir / (gitdir / 'commondir').read_text().strip() if (gitdir / 'commondir').exists() else gitdir
    loose = common / ref
    if loose.exists():
        return loose.read_text().strip()
    for line in (common / 'packed-refs').read_text().splitlines():
        if line.endswith(' ' + ref):
            return line.split()[0]
    raise ValueError('Unable to resolve evaluation checkout HEAD')


def provenance():
    # Git metadata read directly: no subprocess escapes worker isolation.
    return {'base_sha': '4994caa15533c0cf84c07208c9097e4197f2815b',
            'script_sha256': digest_bytes(Path(__file__).read_bytes()),
            'production_files': {str(path.relative_to(ROOT)): digest_bytes(path.read_bytes()) for path in sorted((ROOT / 'service').rglob('*.py'))},
            'git_head': git_revision(),
            'model': {'kind': 'scripted', 'revision': None, 'checkpoint': None, 'weights_sha256': None},
            'python': sys.version, 'seal': json.loads((DATA / 'seal.json').read_text())}


def main_cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--split', choices=['dev', 'test'], default='dev')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--candidate', action='store_true')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--candidate-sha', help='Exact frozen checkout SHA, required for sealed test')
    parser.add_argument('--allow-heldout', action='store_true', help='Explicit frozen-candidate permission; never use to tune')
    args = parser.parse_args()
    if args.split == 'test' and not args.allow_heldout:
        parser.error('sealed test requires --allow-heldout after candidate freeze')
    if args.split == 'test' and (not args.candidate_sha or args.candidate_sha != git_revision()):
        parser.error('sealed test requires --candidate-sha matching frozen checkout HEAD')
    cases = read_cases(args.split)
    if args.limit is not None:
        if args.limit < 1:
            parser.error('--limit must be positive')
        cases = cases[:args.limit]
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = provenance()
    with tempfile.TemporaryDirectory(prefix='wisp-router-eval-') as temporary:
        state = Path(temporary)
        install_guard(state, args.output)
        rows = []
        for case in cases:
            start = time.monotonic()
            run = asyncio.run(run_case(case, state, candidate=args.candidate))
            run['wall_seconds_scripted_only'] = time.monotonic() - start
            run['score'] = score(case, run)
            rows.append(run)
        raw = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows).encode()
        (args.output / 'raw.jsonl').write_bytes(raw)
        report = {'mode': 'scripted-production-path', 'not_model_measurement': True,
                  'split': args.split, 'cases': len(rows), 'metrics': aggregate(rows), 'provenance': manifest,
                  'raw_sha256': digest_bytes(raw), 'frozen_candidate_sha': args.candidate_sha,
                  'per_family': {family: aggregate([row for row, case in zip(rows, cases) if case['family'] == family])
                                 for family in sorted({case['family'] for case in cases})}}
        (args.output / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'mode': report['mode'], 'not_model_measurement': True, 'cases': len(rows), 'metrics': report['metrics']}))


if __name__ == '__main__':
    main_cli()
