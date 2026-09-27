"""Bounded, local-only model invocation for grounded obligation candidates."""
from __future__ import annotations

import json

from service.browser.contracts import ContractViolation
from service.config.endpoints import is_loopback, local_role_target
from service.discovery.extraction import (
    COVERAGE, MAX_MODEL_BYTES, _issue, _model_candidates, build_model_request,
    extract_observation,
)
from service.inference.omlx_client import OMLXClient

MAX_LOCAL_INPUT_CHARS = 4096


async def extract_observation_local(observation: dict, *, coverage: str = 'unknown',
                                    timezone_name: str | None = None,
                                    client=None, model: str | None = None) -> dict:
    """Run one schema-constrained local inference and recover from bad output.

    Caller-supplied clients must identify a managed loopback server. No cloud
    binding or remote fallback is allowed. The model can only propose spans;
    extraction rechecks every quote and constructs all authoritative fields.
    """
    if type(coverage) is not str or coverage not in COVERAGE:
        return extract_observation(observation, coverage=coverage,
                                   timezone_name=timezone_name)
    try:
        request = build_model_request(observation)
    except (ContractViolation, ValueError, TypeError, OverflowError):
        return extract_observation(observation, coverage=coverage,
                                   timezone_name=timezone_name)
    # OMLX's generic fit path may trim an overlong user message. A model span
    # generated against trimmed text must never appear to cover a full capture.
    if len(request['source']['text']) > MAX_LOCAL_INPUT_CHARS:
        result = extract_observation(observation, coverage=coverage,
                                     timezone_name=timezone_name)
        result['clarifications'].append(_issue('model_input_limit'))
        result['processing_complete'] = False
        return result
    base_url = getattr(client, 'base_url', None) if client is not None else None
    if client is not None and (getattr(client, 'managed', None) is not True or
                               type(base_url) is not str or not is_loopback(base_url)):
        result = extract_observation(observation, coverage=coverage,
                                     timezone_name=timezone_name)
        result['clarifications'].append(_issue('local_model_required'))
        result['processing_complete'] = False
        return result
    owned_client = client is None
    try:
        target = local_role_target('fast') if owned_client else None
        if owned_client:
            if not target.endpoint.managed or not is_loopback(target.endpoint.base_url):
                raise ValueError('Local model target is not a managed loopback')
            client = OMLXClient(target=target, timeout=30.0)
        chosen_model = target.model if target else (model or getattr(client, 'model', None))
        if not isinstance(chosen_model, str) or not chosen_model:
            raise ValueError('Missing local model identity')
        response = await client.chat(
            chosen_model,
            [{'role': 'system', 'content': request['instruction']},
             {'role': 'user', 'content': request['source']['text']}],
            temperature=0, max_tokens=2048,
            response_format={'type': 'json_schema', 'json_schema': {
                'name': 'obligation_candidates', 'strict': True,
                'schema': request['output_schema']}},
        )
        if type(response) is not dict or type(response.get('choices')) is not list or not response['choices']:
            raise ValueError('Missing model choice')
        choice = response['choices'][0]
        if type(choice) is not dict or choice.get('finish_reason') != 'stop':
            raise ValueError('Incomplete model result')
        message = choice.get('message')
        content = message.get('content') if type(message) is dict else None
        if type(content) is not str or len(content.encode('utf-8')) > MAX_MODEL_BYTES:
            raise ValueError('Invalid model content')
        decoded = json.loads(content)
        _model_candidates(decoded, observation['text'])
    except Exception:
        result = extract_observation(observation, coverage=coverage,
                                     timezone_name=timezone_name)
        result['clarifications'].append(_issue('invalid_model_output'))
        result['processing_complete'] = False
        for item in result['items']:
            item['ambiguity'] += ' Local model output was unavailable or invalid.'
        return result
    finally:
        if owned_client and client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
    return extract_observation(observation, coverage=coverage,
                               model_output=decoded, timezone_name=timezone_name)
