"""Memory acceptance tests. All state is temporary; no user sources or effects."""
import asyncio
import json
import sqlite3
from unittest.mock import patch
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from service.memory.facts import FactStore, key, memory_context_block, _clean
from service.memory.store import SessionStore
from service.memory.queue import MemoryQueue
from service.memory.capture import MemoryWorker, validated_candidates
from service.memory.connections import ConnectionJobs, packet, original_body

@pytest.fixture
def world(tmp_path):
    facts = FactStore(tmp_path / 'facts.db')
    sessions = SessionStore(tmp_path / 'sessions.db')
    yield facts, sessions, MemoryQueue(sessions)
    facts._db.close()
    sessions._db.close()


def evidence(sid, idx, quote, when=10):
    return {'source_type': 'conversation', 'source_id': f'{sid}:{idx}', 'session_id': sid,
            'turn_idx': idx, 'quote': quote, 'observed_at': when}

class FakeModel:
    def __init__(self, *outputs):
        self.outputs = iter(outputs)
        self.calls = []
    async def chat(self, model, messages, **kwargs):
        self.calls.append((model, messages, kwargs))
        return {'choices': [{'message': {'content': json.dumps(next(self.outputs))}, 'finish_reason': 'stop'}]}


def test_migrates_legacy_without_losing_facts(tmp_path):
    path = tmp_path / 'old.db'
    db = sqlite3.connect(path)
    db.execute('''CREATE TABLE facts(id INTEGER PRIMARY KEY AUTOINCREMENT,text TEXT NOT NULL,category TEXT,
        session_id TEXT,created_at REAL,updated_at REAL,last_used REAL DEFAULT 0,use_count INTEGER DEFAULT 0,pinned INTEGER DEFAULT 0)''')
    db.execute("INSERT INTO facts(text,category,created_at,updated_at,pinned) VALUES('User prefers short notes','preference',1,2,1)")
    db.commit(); db.close()
    facts = FactStore(path)
    assert facts.count() == 1
    row = facts.search('short notes')[0]
    assert row['origin'] == 'legacy' and row['pinned'] == 1
    assert facts.get(row['id'])['evidence'] == []
    facts._db.close()
    reopened = FactStore(path)
    assert len(reopened.search('notes')) == 1
    reopened._db.close()


def test_exact_dedup_does_not_overwrite_substring_facts(world):
    facts, _, _ = world
    first = facts.add('I like coffee.')
    second = facts.add('I like coffee. My sister dislikes coffee.')
    duplicate = facts.add('I like coffee!')
    assert first['id'] != second['id']
    assert duplicate['id'] == first['id']
    assert facts.count() == 2


def test_capture_survives_restart_and_enters_new_session_context(world, monkeypatch, tmp_path):
    facts, sessions, q = world
    sid = sessions.create_session()
    text = 'I prefer numbered notes for Project Cedar.'
    sessions.add_turn(sid, 'user', text)
    sessions.add_turn(sid, 'assistant', 'Understood.')
    assert q.stats()['jobs'] == {'queued': 1}
    model = FakeModel({'facts': [{'quote': text, 'category': 'preference'}]})
    worker = MemoryWorker(sessions, facts)
    job = q.next()
    assert asyncio.run(worker.process(job, model, 'Ling')) == 1
    q.finish(job)
    fresh = sessions.create_session()
    assert fresh != sid
    reopened = FactStore(tmp_path / 'facts.db')
    with patch('service.memory.facts.store', reopened):
        block = memory_context_block(query='Draft notes for Cedar')
    assert text in block
    assert reopened.all()[0]['origin'] == 'automatic'
    assert reopened.get(reopened.all()[0]['id'])['evidence'][0]['session_id'] == sid
    reopened._db.close()


def test_capture_setting_applies_at_persistence_time(world):
    facts, sessions, q = world
    q.enable(False)
    sid = sessions.create_session()
    sessions.add_turn(sid, 'user', 'I prefer tea.')
    assert q.next() is None
    q.enable(True)
    sessions.add_turn(sid, 'user', 'I prefer coffee.')
    assert q.next()['turn_idx'] == 1


def test_historical_pilot_does_not_confirm_or_process_twice(world):
    facts, sessions, q = world
    q.enable(False)
    sid = sessions.create_session()
    text = 'My project is called Cedar.'
    sessions.add_turn(sid, 'user', text)
    assert q.pilot(20) == 1
    assert q.pilot(20) == 0
    job = q.next()
    worker = MemoryWorker(sessions, facts)
    model = FakeModel({'facts': [{'quote': text, 'category': 'project'}]})
    asyncio.run(worker.process(job, model, 'Ling'))
    assert facts.count() == 0
    proposed = facts.all(status='proposed')
    assert len(proposed) == 1
    facts.review(proposed[0]['id'], 'confirm')
    assert len(facts.search('Cedar')) == 1


@pytest.mark.parametrize('text,quote', [
    ('Imagine I prefer numbered notes.', 'I prefer numbered notes.'),
    ('My example: "I prefer numbered notes."', 'I prefer numbered notes.'),
    ('I no longer say I prefer numbered notes.', 'I prefer numbered notes.'),
    ('I prefer numbered notes but only in this fictional test.', 'I prefer numbered notes'),
    ('My password is correct-horse-battery.', 'My password is correct-horse-battery.'),
    ('My mom uses mom@example.net.', 'My mom uses mom@example.net.'),
    ('I have an allergy to peanuts.', 'I have an allergy to peanuts.'),
])
def test_quoted_hypothetical_sensitive_and_contact_facts_are_not_auto_saved(text, quote):
    assert validated_candidates(json.dumps({'facts': [{'quote': quote}]}), text) == []


def test_extractor_cannot_invent_evidence(world):
    with pytest.raises(ValueError, match='exact'):
        validated_candidates(json.dumps({'facts': [{'quote': 'I live in Boston.'}]}), 'I live in Seattle.')


def test_proposals_deferred_and_rejected_never_enter_recall(world):
    facts, _, _ = world
    data = evidence('s', 0, 'Possible address')
    row = facts.add('Mom may use this address.', origin='connection', evidence=[data])
    assert facts.search('Mom') == []
    facts.review(row['id'], 'later')
    assert facts.search('Mom') == []
    facts.add('Mom may use this address.', origin='connection', evidence=[data])
    assert len(facts.all(status='deferred')) == 1
    facts.review(row['id'], 'reject')
    assert facts.add('Mom may use this address.', origin='connection', evidence=[data])['suppressed']


def test_correction_wins_when_old_evidence_is_reprocessed(world):
    facts, sessions, q = world
    sid = sessions.create_session()
    old = 'I prefer numbered notes.'
    sessions.add_turn(sid, 'user', old)
    row = facts.add(old, 'preference', origin='automatic', evidence=[evidence(sid, 0, old)])
    corrected = facts.review(row['id'], 'edit', 'I prefer short paragraphs.')
    assert corrected['text'] == 'I prefer short paragraphs.'
    assert facts.add(old, origin='historical', evidence=[evidence(sid, 0, old)])['suppressed']
    assert q.search('numbered', facts) == []
    assert facts.search('numbered') == []
    assert facts.search('paragraphs')


def test_forget_removes_evidence_and_blocks_transcript_fallback(world):
    facts, sessions, q = world
    sid = sessions.create_session()
    text = 'My project is Cedar.'
    sessions.add_turn(sid, 'user', text)
    row = facts.add(text, origin='automatic', evidence=[evidence(sid, 0, text)])
    assert facts.delete(row['id'])
    assert facts.get(row['id'])['text'] == ''
    assert facts.get(row['id'])['evidence'] == []
    assert q.search('Cedar', facts) == []
    assert facts.add(text, origin='automatic', evidence=[evidence(sid, 0, text)])['suppressed']
    assert facts.add(text)['status'] == 'active'  # A new explicit save is allowed.


def test_deleted_conversation_removes_index_jobs_and_unconfirmed_memories(world):
    facts, sessions, q = world
    sid = sessions.create_session()
    sessions.add_turn(sid, 'user', 'My project is Cedar.')
    row = facts.add('My project is Cedar.', origin='automatic', evidence=[evidence(sid, 0, 'My project is Cedar.')])
    sessions.delete_session(sid)
    facts.remove_session(sid)
    assert q.search('Cedar', facts) == []
    assert q.next() is None
    assert facts.get(row['id'])['status'] == 'forgotten'


def test_entire_history_search_is_order_independent(world):
    facts, sessions, q = world
    sid = sessions.create_session()
    sessions.add_turn(sid, 'user', 'My project is Project Cedar.')
    for _ in range(25):
        other = sessions.create_session()
        sessions.add_turn(other, 'user', 'Hello.')
    assert q.search('Cedar project', facts)[0]['session_id'] == sid


def test_context_is_bounded_relevant_and_never_includes_proposals(world):
    facts, _, _ = world
    facts.add('My Cedar project uses Python.', 'project')
    for i in range(35):
        facts.add(f'My unrelated project number {i} uses Ruby.', 'project')
    facts.add('Cedar may belong to Mom.', origin='connection', evidence=[evidence('x', 0, 'Cedar')])
    with patch('service.memory.facts.store', facts):
        block = memory_context_block(600, 'Cedar Python')
    assert 'uses Python' in block and 'may belong' not in block and 'Ruby' not in block
    assert len(block) <= 600


def test_transaction_rolls_back_nested_correction(world):
    facts, _, _ = world
    row = facts.add('I like tea.')
    with patch.object(facts, 'add', side_effect=RuntimeError('write failure')):
        with pytest.raises(RuntimeError):
            facts.review(row['id'], 'edit', 'I like coffee.')
    assert facts.get(row['id'])['status'] == 'active'
    assert facts._db.execute('SELECT count(*) FROM memory_suppression').fetchone()[0] == 0


def test_restart_recovers_queue_jobs(world):
    _, sessions, q = world
    sid = sessions.create_session()
    sessions.add_turn(sid, 'user', 'I prefer tea.')
    job = q.next()
    assert q.next() is None
    q.recover()
    assert q.next()['session_id'] == job['session_id']


def test_extract_does_not_write_when_source_deleted_during_model_call(world):
    facts, sessions, q = world
    sid = sessions.create_session()
    text = 'I prefer tea.'
    sessions.add_turn(sid, 'user', text)
    class DeletedModel(FakeModel):
        async def chat(self, *args, **kwargs):
            sessions.delete_session(sid)
            return await super().chat(*args, **kwargs)
    worker = MemoryWorker(sessions, facts)
    job = q.next()
    assert asyncio.run(worker.process(job, DeletedModel({'facts': [{'quote': text}]}), 'Ling')) == 0
    assert facts.count() == 0


def test_connection_investigation_is_evidence_backed_and_always_proposed(world):
    facts, sessions, _ = world
    p = packet('contacts', 'c1', 'Mom: +15551234567', 'Contact', 1)
    e = packet('email', 'e1', 'From: candidate@example.net\nSignature: +15551234567', 'Email', 2)
    class Sources:
        def search(self, query): return [p, e]
    jobs = ConnectionJobs(facts, sessions, Sources())
    jid = jobs.create('Which email might belong to Mom?')
    job = jobs.next()
    model = FakeModel({'queries': ['Mom']}, {'connections': [{
        'statement': 'Mom may use candidate@example.net.', 'subject': 'Mom', 'predicate': 'possibly_uses_email',
        'object': 'candidate@example.net', 'explanation': 'The phone matches; account ownership still needs confirmation.',
        'evidence': [{'id': p['id'], 'quote': '+15551234567'}, {'id': e['id'], 'quote': 'candidate@example.net'}]}]})
    asyncio.run(jobs.process(job, model, 'Ling'))
    assert facts.count() == 0
    row = facts.all(status='proposed')[0]
    assert row['subject'] == 'Mom'
    assert len(facts.get(row['id'])['evidence']) == 2
    assert all('tools' not in c[2] for c in model.calls)
    assert jobs.list()[0]['status'] == 'done'


def test_invalid_connection_evidence_cannot_save_partial_results(world):
    facts, sessions, _ = world
    p = packet('contacts', 'c1', 'Mom', 'Contact', 1)
    class Sources:
        def search(self, query): return [p]
    jobs = ConnectionJobs(facts, sessions, Sources()); jobs.create('Mom')
    c = {'statement': 'Mom uses an address.', 'subject': 'Mom', 'predicate': 'email', 'object': 'address',
         'explanation': 'Possible', 'evidence': [{'id': 'invented', 'quote': 'Mom'}]}
    model = FakeModel({'queries': ['Mom']}, {'connections': [c]})
    with pytest.raises(ValueError, match='Unsupported'):
        asyncio.run(jobs.process(jobs.next(), model, 'Ling'))
    assert facts.all(status='proposed') == []


def test_forwarded_signature_is_not_authored_by_current_sender():
    assert '12345' not in original_body('Hello\nOn Monday someone wrote:\nPhone: 12345')
    assert '12345' not in original_body('Hello\n> Phone: 12345')
    assert '12345' not in original_body('Hello\n----- Forwarded message -----\nPhone: 12345')


def test_api_review_search_and_capture_settings(world, monkeypatch):
    import service.memory.api as api
    facts, sessions, _ = world
    monkeypatch.setattr(api, 'facts', facts)
    monkeypatch.setattr(api, 'sessions', sessions)
    monkeypatch.setattr(api, 'worker', MemoryWorker(sessions, facts))
    app = FastAPI(); app.include_router(api.router)
    with TestClient(app) as client:
        assert client.post('/memory/facts', json={'text': '   '}).status_code == 422
        row = client.post('/memory/facts', json={'text': 'I prefer tea.'}).json()
        assert row['ok']
        fid = row['id']
        assert client.get(f'/memory/facts/{fid}').json()['fact']['origin'] == 'explicit'
        assert client.post('/memory/capture', json={'enabled': False}).json()['capture_enabled'] is False
        assert client.get('/memory/search', params={'query': 'tea'}).json()['facts']
        assert client.post(f'/memory/facts/{fid}/review', json={'decision': 'edit', 'text': 'I prefer coffee.'}).status_code == 200
        assert not client.get('/memory/search', params={'query': 'tea'}).json()['facts']
        assert client.post('/memory/investigations', json={'question': 'Mom', 'model_role': 'research'}).json()['ok']
        assert client.get('/memory/investigations').json()['investigations'][0]['model_role'] == 'research'


def test_clean_empty_input():
    assert _clean('') == ''
