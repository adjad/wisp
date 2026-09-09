"""Inspectable memory API; read operations never run extraction or model inference."""
from __future__ import annotations
from typing import Literal
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from service.memory.facts import store as facts
from service.memory.store import store as sessions
from service.memory.capture import MemoryWorker

router = APIRouter(prefix='/memory', tags=['memory'])
worker = MemoryWorker(sessions, facts)

class FactInput(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    category: Literal['person', 'preference', 'project', 'routine', 'fact'] = 'fact'
    pinned: bool = False

class ReviewInput(BaseModel):
    decision: Literal['confirm', 'reject', 'later', 'edit']
    text: str | None = Field(default=None, max_length=1000)
    supersedes_id: int | None = Field(default=None, ge=1)

class CaptureInput(BaseModel):
    enabled: bool

class PinInput(BaseModel):
    pinned: bool = True

class PilotInput(BaseModel):
    sessions: int = Field(default=20, ge=1, le=100)

@router.get('/status')
def status():
    pending = facts.all(1000, status='proposed')
    return {**worker.queue.stats(), 'worker': worker.state, 'count': facts.count(),
            'proposals': len(pending), 'unreviewed': sum(not r['reviewed'] for r in pending),
            'storage': {'facts': str(__import__('service.memory.facts', fromlist=['DB_PATH']).DB_PATH),
                        'transcripts': str(__import__('service.memory.store', fromlist=['DB_PATH']).DB_PATH)},
            'investigation_models': {'routine': 'agent', 'research': 'research or coding'},
            'capture_policy': 'Verbatim standalone user statements; historical facts and inferred connections require review.'}

@router.get('/facts')
def list_facts(limit: int = Query(500, ge=1, le=1000), status: Literal['active', 'proposed', 'deferred', 'superseded'] = 'active', query: str = ''):
    rows = facts.search(query, limit) if query and status == 'active' else facts.all(limit, status=status)
    if query and status != 'active':
        rows = [r for r in rows if query.casefold() in r['text'].casefold()]
    return {'facts': rows, 'count': facts.count()}

@router.get('/facts/{fid}')
def detail(fid: int):
    row = facts.get(fid)
    if not row or row['status'] == 'forgotten':
        raise HTTPException(404, 'Memory not found')
    return {'fact': row}

@router.post('/facts')
def add_fact(body: FactInput):
    try:
        return {'ok': True, **facts.add(body.text, body.category, pinned=body.pinned)}
    except ValueError as e:
        raise HTTPException(422, str(e)) from e

@router.delete('/facts/{fid}')
def delete_fact(fid: int):
    return {'ok': facts.delete(fid)}

@router.post('/facts/{fid}/pin')
def pin(fid: int, body: PinInput):
    return {'ok': facts.set_pinned(fid, body.pinned)}

@router.post('/facts/{fid}/review')
def review(fid: int, body: ReviewInput):
    try:
        return {'ok': True, 'fact': facts.review(fid, body.decision, body.text, body.supersedes_id)}
    except ValueError as e:
        raise HTTPException(422, str(e)) from e

@router.post('/capture')
def capture(body: CaptureInput):
    worker.queue.enable(body.enabled)
    return {'ok': True, **status()}

@router.post('/backfill')
def backfill(body: PilotInput):
    return {'ok': True, 'queued': worker.queue.pilot(body.sessions),
            'message': 'Historical candidates will appear for review; they are not automatically confirmed.'}

@router.post('/retry')
def retry():
    return {'ok': True, 'queued': worker.queue.retry()}

@router.get('/search')
def search(query: str = Query(min_length=1, max_length=1000), limit: int = Query(15, ge=1, le=50)):
    return {'facts': facts.search(query, limit), 'conversations': worker.queue.search(query, facts, limit)}


class InvestigationInput(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    model_role: Literal['agent', 'research'] = 'agent'

@router.get('/investigations')
def investigations():
    return {'investigations': worker.connections.list()}

@router.post('/investigations')
def investigate(body: InvestigationInput):
    try:
        return {'ok': True, 'id': worker.connections.create(body.question, body.model_role)}
    except ValueError as e:
        raise HTTPException(422, str(e)) from e

@router.post('/investigations/{jid}/cancel')
def cancel_investigation(jid: int):
    found = next((j for j in worker.connections.list() if j['id'] == jid), None)
    if not found:
        raise HTTPException(404, 'Investigation not found')
    if found['status'] in ('queued', 'running'):
        worker.connections.set_status(jid, 'cancelled')
    return {'ok': True}
