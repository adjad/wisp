# R11 — P1 — BLOCK

Frozen candidate `67e4cefa53687f265287b9d068fc549c1f7955b6`, [PR160](https://github.com/adjad/wisp/pull/160).

`_ScopedResidentClient.stream_events` yields at `scripts/eval_router_update.py:582` while `_operation` retains the private `peer` ContextVar. Through the supported factory and ResidentInferenceAdapter, caller-side policy changes from denied to admitted after the first frame, and a child created there keeps that policy after stream close. Its `Path.home()` also resolves to the fixture original home. Four explicit boundary assertions fail; parent context/early-close/cancellation cleanup still pass.

The audit at line886 trusts the copied ambient phase, so it no longer confines socket admission to owned client I/O. Reset caller context around event transfer and invalidate authority when the owned operation closes, including inherited task contexts.

All transport/auth/home/lock inputs were synthetic; only sys.audit policy events were used, with no sockets, native operations, real credentials or model execution. Full audit and remaining CI are recorded separately. No candidate edit or merge is authorized.
