"""Local-only model calls: grounded candidates, then per-line deadline revision judgment."""
from __future__ import annotations

import json

from service.browser.contracts import ContractViolation
from service.config.endpoints import is_loopback, local_role_target
from service.discovery.extraction import (
    COVERAGE, MAX_MODEL_BYTES, MAX_REVISION_LINES, _issue, _model_candidates, _observation,
    _revision_answers, build_model_request, build_revision_request,
    extract_observation,
)
from service.inference.omlx_client import OMLXClient

MAX_LOCAL_INPUT_CHARS = 4096
MAX_REVISION_INPUT_CHARS = 16384
ATTEMPTS = 2


async def _ask(client, model: str, instruction: str, content: str, name: str,
               schema: dict, check):
    """One schema-constrained call; invalid output is retried once, then None.

    check(decoded) must raise on a response that does not validate. A transport
    failure is not retried. Thinking that leaked into the content is invalid.
    """
    for _ in range(ATTEMPTS):
        try:
            response = await client.chat(
                model,
                [{'role': 'system', 'content': instruction},
                 {'role': 'user', 'content': content}],
                temperature=0, max_tokens=2048,
                response_format={'type': 'json_schema', 'json_schema': {
                    'name': name, 'strict': True, 'schema': schema}})
        except Exception:
            return None
        try:
            if type(response) is not dict or type(response.get('choices')) is not list or not response['choices']:
                raise ValueError('Missing model choice')
            choice = response['choices'][0]
            if type(choice) is not dict or choice.get('finish_reason') != 'stop':
                raise ValueError('Incomplete model result')
            message = choice.get('message')
            text = message.get('content') if type(message) is dict else None
            if (type(text) is not str or len(text.encode('utf-8')) > MAX_MODEL_BYTES or
                    not text.lstrip().startswith('{') or '</think>' in text):
                raise ValueError('Invalid model content')
            decoded = json.loads(text)
            check(decoded)
            return decoded
        except Exception:
            continue
    return None


def _degraded(result: dict, note: str) -> dict:
    result['clarifications'].append(_issue('invalid_model_output'))
    result['processing_complete'] = False
    for item in result['items']:
        item['ambiguity'] += ' ' + note
    return result


async def extract_observation_local(observation: dict, *, coverage: str = 'unknown',
                                    timezone_name: str | None = None,
                                    client=None, model: str | None = None) -> dict:
    """Run local inference for candidates, then per-line deadline revision judgment.

    Caller-supplied clients must identify a managed loopback server. No cloud
    binding or remote fallback is allowed. The model can only copy quotes and
    answer closed revision questions; extraction locates every quote, derives
    all offsets and constructs all authoritative fields. Invalid output is retried once, then
    the affected deadlines stay unresolved.
    """
    if type(coverage) is not str or coverage not in COVERAGE:
        return extract_observation(observation, coverage=coverage,
                                   timezone_name=timezone_name)
    try:
        # The caller may mutate its dict while inference awaits. Freeze the
        # validated scalar fields once, then use that revision for every span
        # check and Evidence record, including recovery paths.
        snapshot = dict(_observation(observation))
        request = build_model_request(snapshot)
    except (ContractViolation, ValueError, TypeError, OverflowError):
        return extract_observation(observation, coverage=coverage,
                                   timezone_name=timezone_name)
    # OMLX's generic fit path may trim an overlong user message. A model span
    # generated against trimmed text must never appear to cover a full capture.
    if len(request['source']['text']) > MAX_LOCAL_INPUT_CHARS:
        result = extract_observation(snapshot, coverage=coverage,
                                     timezone_name=timezone_name)
        result['clarifications'].append(_issue('model_input_limit'))
        result['processing_complete'] = False
        return result
    base_url = getattr(client, 'base_url', None) if client is not None else None
    if client is not None and (getattr(client, 'managed', None) is not True or
                               type(base_url) is not str or not is_loopback(base_url)):
        result = extract_observation(snapshot, coverage=coverage,
                                     timezone_name=timezone_name)
        result['clarifications'].append(_issue('local_model_required'))
        result['processing_complete'] = False
        return result
    owned_client = client is None
    candidates = revision = revision_issue = None
    try:
        target = local_role_target('fast') if owned_client else None
        if owned_client:
            if not target.endpoint.managed or not is_loopback(target.endpoint.base_url):
                raise ValueError('Local model target is not a managed loopback')
            client = OMLXClient(target=target, timeout=30.0)
        chosen_model = target.model if target else (model or getattr(client, 'model', None))
        if not isinstance(chosen_model, str) or not chosen_model:
            raise ValueError('Missing local model identity')
        text = snapshot['text']
        candidates = await _ask(
            client, chosen_model, request['instruction'], text,
            'obligation_candidates', request['output_schema'],
            lambda decoded: _model_candidates(decoded, text))
        questions = build_revision_request(snapshot, coverage=coverage,
                                           model_output=candidates,
                                           timezone_name=timezone_name)
        if questions is not None:
            content = json.dumps({key: questions[key] for key in
                                  ('lines', 'items', 'date_candidates')},
                                 ensure_ascii=False)
            if (len(content) > MAX_REVISION_INPUT_CHARS or
                    len(questions['lines']) > MAX_REVISION_LINES):
                revision_issue = 'model_input_limit'
            else:
                keys = {item['item'] for item in questions['items']}
                facts = extract_observation(snapshot, coverage=coverage,
                                            timezone_name=timezone_name)['temporal_facts']
                revision = await _ask(
                    client, chosen_model, questions['instruction'], content,
                    'deadline_revision', questions['output_schema'],
                    lambda decoded: _revision_answers(decoded, text, keys, facts))
                if revision is None:
                    revision_issue = 'invalid_revision_output'
    except Exception:
        candidates = revision = None
        failed = True
    else:
        failed = candidates is None
    finally:
        if owned_client and client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
    result = extract_observation(snapshot, coverage=coverage, model_output=candidates,
                                 revision_output=revision, timezone_name=timezone_name)
    if failed:
        return _degraded(result, 'Local model output was unavailable or invalid.')
    if revision_issue is not None:
        # The unanswered deadlines are already unresolved; name the cause.
        result['clarifications'].append(_issue(revision_issue))
        result['processing_complete'] = False
    return result
