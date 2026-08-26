# Static adversarial router findings

Generated from the data-only corpus in `tests/router_adversarial_cases.py` via:

```bash
.venv/bin/python scripts/audit_router_prompts.py
```

No chat model, embedding model, assistant tool, or external action is required
for this audit. These are deterministic rule-layer findings. Semantic routes
use `tests/stub_embedder.py`; their word-overlap results are recorded but are
excluded from the concrete bug count because real retrieval quality is a
separate model-backed measurement.

## Concrete rule-layer findings

| Cases | Observed route problem | Risk |
|---|---|---|
| `compound_wifi_email`, `compound_reminder_email` | Self-addressed email phrasing exposes `send_email`, bypassing the router's documented self-send-to-draft policy. | An attempted self-send can hit the own-address guard or drift to a wrong contact. |
| `compound_pdf_email` | The email rule claims the request and omits `find_files`, `read_file`, and contact lookup. | The assistant can draft but cannot obtain the requested PDF content or reliably resolve Sarah. |
| `compound_weather_reminder` | Reminder routing omits `get_weather`. | The model cannot evaluate the condition and may create a reminder blindly. |
| `conditional_low_power` | Router-direct dispatch pre-calls only `get_battery_status` and strands `toggle_setting`. | The conditional action cannot complete even when the battery is low. |
| `overload_email_body` | The email noun wins despite the explicit phrase “without opening my inbox.” | Violates a negative data-access instruction. |
| `overload_plain_text_notes` | Notes routing offers Notes.app write tools but no `write_file`. | “Plain-text file” can become another app note instead of a file. |
| `overload_code_message` | “What message is this code…” routes to Messages tools. | Wrong-domain personal-data access and an unusable answer path. |
| `reminder_email_sister` | The future task content (“email my sister”) arms immediate email and scheduled-send tools alongside the reminder. | The assistant may send instead of merely reminding. |
| `memory_told_dan` | A read about a past conversation becomes channel-ambiguous compose, forces `get_upcoming`, and offers unrelated email/calendar tools. | Wrong-domain calls and unnecessary channel clarification. |
| `memory_calendar_boundary` | “What do you know about my calendar?” merges `recall` into a live calendar read. | Stale memory can compete with authoritative calendar data. |
| `calendar_complete_reminder` | Negated calendar wording causes router-direct `get_upcoming`; `complete_reminder` is absent. | The requested completion cannot occur. |
| `device_wisp_slow` | The compound performance question direct-dispatches only `system_status`, omitting `wisp_status`. | It cannot distinguish machine pressure from Wisp-specific health. |
| `capability_matrix` | “Can you…” is treated as an action request and exposes `send_message` instead of a capability-status tool. | A capability question is unnecessarily armed with a real send tool. |
| `seq62_t3` | Confirmation after calendar-to-note flow inherits calendar tools but loses `create_note`. | “Yes, go ahead” cannot perform the action just offered. |
| `seq63_t2` | “Reply that Thursday works” ignores the prior Messages tool context, asks for a channel, and withholds `send_message`. | Natural reply follow-ups fail despite an unambiguous prior channel. |
| `seq66_t3` | “Delete that message from the queue” routes as a normal Messages write and omits `cancel_scheduled_send`. | The queued item cannot be cancelled and a new message could be sent instead. |
| `live_stock_topic_not_calendar` | The broad unresolved-topic rule forces `get_upcoming` for “movements of my stocks today,” even though `get_stock_price` is already available. | Adds irrelevant latency and calendar data to an outbound financial summary. |

## Static audit boundary

- **85 prompts total**: the original single-turn prompts, each individual turn
  in the six sequences, six live-regression shapes from the handoff, and the
  two known aggregate-gap prompts.
- **54 concrete rule routes**: fully inspectable without semantic retrieval.
- **31 stub-retrieval routes**: fully recorded with the offline deterministic
  embedder, but not graded as real retrieval-quality results.
- **47 plan-mode prompts**: every corpus entry that may write, send, delete, or
  change device state is marked for intercepted execution only.
