"""Read-only diagnostic plugin: unchanged test plus adversarial clock replay."""
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import socket
import subprocess


def pytest_runtest_call(item):
    if item.name != 'test_distinct_redacted_requests_keep_both_source_occurrences':
        return
    module = item.module
    patch = item.funcargs['monkeypatch']
    epoch = float(os.environ['ROUTER_DIGEST_DIAG_EPOCH'])
    case = os.environ['ROUTER_DIGEST_DIAG_CASE']
    now = datetime.fromtimestamp(epoch)
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls.fromtimestamp(epoch)
            return value if tz is None else cls.fromtimestamp(epoch, tz)
    patch.setattr(module.time, 'time', lambda: epoch)
    patch.setattr(module.M, 'datetime', FrozenDateTime)
    patch.setattr(module.D, 'datetime', FrozenDateTime)
    from service.tools import timeranges
    patch.setattr(module.M, 'resolve_span', lambda period: timeranges.resolve_span(period, now=now))
    attempts=[]
    def forbidden(effect):
        def deny(*args, **kwargs):
            attempts.append(effect)
            raise AssertionError('diagnostic forbids '+effect)
        return deny
    patch.setattr(socket.socket, 'connect', forbidden('network connect'))
    patch.setattr(socket.socket, 'connect_ex', forbidden('network connect_ex'))
    patch.setattr(socket, 'getaddrinfo', forbidden('network resolve'))
    patch.setattr(subprocess, 'Popen', forbidden('native subprocess'))
    source = [(epoch-2, 'Alex', 'Alex: Please review the report by Friday. Verification code is 1234.'),
              (epoch-1, 'Alex', 'Alex: Please review the proposal by Friday. Verification code is 5678.')]
    rows=module.M.filter_summary_message_rows(source)
    secrets=('1234','5678','report','proposal')
    text_fields=[(row[1],row[2]) for row in rows]
    assert len(rows)==2 and rows[0][2]==rows[1][2]
    assert all(secret not in str(text_fields) for secret in secrets)
    assert len(module.M.filter_summary_message_rows(rows))==2
    patch.setattr(module.M,'_lines','\n'.join(module._freshness_record(ts,'U',1,'Alex',body) for ts,_,body in source))
    selected=module.M.summary_message_rows(require_read_state=True)
    assert len(selected)==2
    assert all(secret not in str([(r[1],r[2]) for r in selected]) for secret in secrets)
    chat=module.client(patch,error=RuntimeError('synthetic offline'))
    outputs=[]
    with module.debug_capture.capture() as records:
        for args in ({},{'day':'today'},{'period':'this week'},{'conversation':'Alex'}):
            output=asyncio.run(module.M.summarize_messages(**args))
            assert '2 messages across 1 conversation' in output
            assert 'Action items mentioned' in output
            assert all(secret not in output for secret in secrets)
            outputs.append({'args':args,'text':output})
    # Preserve the original whole-payload privacy check for model/debug data.
    assert all(secret not in str(records)+str(chat.call_args_list) for secret in secrets)
    assert not attempts
    evidence={'case':case,'epoch':epoch,'local_datetime':now.isoformat(),
              'row_metadata':[{'timestamp':r[0],'source_before':r.source_before,'source_after':r.source_after} for r in rows],
              'row_text_fields':text_fields,'selected_text_fields':[(r[1],r[2]) for r in selected],
              'original_rows_repr':str(rows),'original_repr_secret_matches':{s:s in str(rows) for s in secrets},
              'metadata_matches':{s:any(s in str(r[0]) for r in rows) for s in secrets},
              'string_content_matches':{s:s in str(text_fields) for s in secrets},
              'outputs':outputs,'debug_records':records,'fake_model_call_arguments':str(chat.call_args_list),
              'network_native_attempts':attempts,'all_payload_privacy_checks_passed':True,
              'two_occurrences_preserved_before_and_after_read_selection':True}
    Path('/private/tmp/WISP_DIGEST_DIAG_'+case+'.json').write_text(json.dumps(evidence,indent=2)+'\n')
