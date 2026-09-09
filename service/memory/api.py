"""Memory review API. Historical extraction and investigations require deliberate action."""
from __future__ import annotations

from typing import Literal
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from service.memory.facts import store as facts
from service.memory.store import store as sessions
from service.memory.capture import MemoryWorker
from service.memory.retrieval import retrieve, render_context

router = APIRouter(prefix='/memory', tags=['memory'])
worker = MemoryWorker(sessions, facts)


class FactInput(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    category: Literal['fact', 'preference', 'person', 'project', 'routine'] = 'fact'
    pinned: bool = False


class ReviewInput(BaseModel):
    decision: Literal['confirm', 'reject', 'later', 'edit']
    text: str | None = Field(default=None, max_length=1000)
    supersedes_id: int | None = Field(default=None, ge=1)


class CaptureInput(BaseModel):
    enabled: bool


class PinInput(BaseModel):
    pinned: bool


class PilotInput(BaseModel):
    sessions: int = Field(default=20, ge=1, le=100)


class InvestigationInput(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    model_role: Literal['agent', 'research'] = 'agent'


@router.get('/status')
def status():
    proposals = facts.all(1000, status='proposed')
    return {**worker.queue.stats(), 'worker': worker.state, 'count': facts.count(),
        'proposals': len(proposals), 'unreviewed': sum(not r['reviewed'] for r in proposals),
        'investigations_enabled': facts.setting('investigations_enabled', False),
        'capture_policy': 'Complete verbatim user statements only; sensitive or ambiguous content is skipped.',
        'retention_policy': 'Deleting a conversation removes its evidence and unreviewed unpinned extracted memories. Explicit, confirmed and pinned memories remain. Forget removes every correction version and suppresses old sources.',
        'backfill_policy': 'Manual bounded pilot only; historical candidates always require review.'}


@router.get('/facts')
def list_facts(query: str = '', status: Literal['active', 'proposed', 'deferred', 'superseded'] = 'active', limit: int = 500):
    limit = max(1, min(1000, limit))
    rows = facts.search(query, limit, status=status) if query.strip() else facts.all(limit, status=status)
    return {'facts': rows, 'count': facts.count()}


@router.get('/facts/{fid}')
def get_fact(fid: int):
    row = facts.get(fid)
    if not row or row['status'] == 'forgotten':
        raise HTTPException(404, 'Memory is unavailable')
    return {'fact': row}


@router.post('/facts')
def add_fact(body: FactInput):
    try:
        return {'ok': True, **facts.add(body.text, body.category, pinned=body.pinned)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete('/facts/{fid}')
def delete_fact(fid: int):
    return {'ok': facts.delete(fid)}


@router.post('/facts/{fid}/pin')
def pin_fact(fid: int, body: PinInput):
    return {'ok': facts.set_pinned(fid, body.pinned)}


@router.post('/facts/{fid}/review')
def review_fact(fid: int, body: ReviewInput):
    try:
        return {'ok': True, 'fact': facts.review(fid, body.decision, text=body.text, supersedes_id=body.supersedes_id)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get('/sources/{sid}/{idx}')
def source_context(sid: str, idx: int):
    turns = worker.queue.source_context(sid, idx, facts)
    if turns is None:
        raise HTTPException(404, 'Source was deleted or suppressed')
    return {'session_id': sid, 'turn_idx': idx, 'turns': turns}


@router.post('/capture')
def capture(body: CaptureInput):
    worker.queue.enable(body.enabled)
    return status()


@router.post('/backfill')
def backfill(body: PilotInput):
    return {'ok': True, 'queued': worker.queue.pilot(body.sessions), **status()}


@router.post('/backfill/cancel')
def cancel_backfill():
    return {'ok': True, 'cancelled': worker.queue.cancel_pilot(), **status()}


@router.post('/retry')
def retry():
    return {'ok': True, 'retried': worker.queue.retry(), **status()}


@router.get('/search')
def search(query: str, limit: int = 15):
    result = retrieve(query, facts=facts, sessions=sessions, limit=max(1, min(50, limit)))
    render_context(result)  # Populate a content-free selection/budget trace.
    return result


@router.get('/investigations')
def investigations():
    return {'enabled': facts.setting('investigations_enabled', False), 'investigations': worker.connections.list()}


@router.post('/investigations')
def investigate(body: InvestigationInput):
    if not facts.setting('investigations_enabled', False):
        raise HTTPException(409, 'Connection investigations are a separate rollout and are currently disabled')
    try:
        jid = worker.connections.create(body.question, body.model_role)
        return {'ok': True, 'id': jid}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post('/investigations/{jid}/cancel')
def cancel_investigation(jid: int):
    row = next((j for j in worker.connections.list() if j['id'] == jid), None)
    if not row or row['status'] not in ('queued', 'running'):
        raise HTTPException(409, 'Investigation is not pending')
    worker.connections.set_status(jid, 'cancelled')
    return {'ok': True}
