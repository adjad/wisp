# Review only — no tests run

## 02. Dates, accounts, and exact details

Preserve named accounts, date windows, source attribution, and exact wording.

### WRS-0051 · Explicit sequence

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Please do these in this order: show saved birthdays in Contacts over the next 30 days; then summarize only yesterday's unread Work emails; then look up Mom's saved phone number and email address; then tell me how far back Wisp can search email; then find Route pages I visited last month, and tell me if the retained history is too short.

**Required tools:** `contact_dates`, `summarize_emails`, `lookup_contact`, `search_coverage`, `search_browser_history`.
**Ordering constraints:** `contact_dates` before `summarize_emails`; `summarize_emails` before `lookup_contact`; `lookup_contact` before `search_coverage`; `search_coverage` before `search_browser_history`.
**Checks:** days 30; do not infer missing dates or age account Work; day yesterday AND unread true; do not drop date filter name Mom; resolve uniquely before any dependent contact use source email; coverage not inbox dump query Route; period last month; disclose coverage boundary Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Two synthetic contacts have saved birthday month/day fields
- Work inbox has 4 unread messages spanning several days; variant-specific state must satisfy: account Work; day yesterday AND unread true; do not drop date filter
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Documented coverage response; not proof of actual full sync
- Browser History opted in; yesterday's sanitized title/path fixture exists; variant-specific state must satisfy: query Route; period last month; disclose coverage boundary

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0052 · Explicit sequence

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Please do these in this order: show saved birthdays in Contacts over the next 30 days; then summarize yesterday's Messages conversations and who may need a reply; then check my Personal calendar for the next seven days; then look up Mom's saved phone number and email address; then tell me how far back Wisp can search email.

**Required tools:** `contact_dates`, `summarize_messages`, `get_upcoming`, `lookup_contact`, `search_coverage`.
**Ordering constraints:** `contact_dates` before `summarize_messages`; `summarize_messages` before `get_upcoming`; `get_upcoming` before `lookup_contact`; `lookup_contact` before `search_coverage`.
**Checks:** days 30; do not infer missing dates or age day yesterday; Messages only account Personal; days 7 name Mom; resolve uniquely before any dependent contact use source email; coverage not inbox dump Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Two synthetic contacts have saved birthday month/day fields
- Synthetic iMessage/SMS conversations include inbound/outbound attribution
- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Documented coverage response; not proof of actual full sync

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0053 · Explicit sequence

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Please do these in this order: look up Mom's saved phone number and email address; then summarize yesterday's Messages conversations and who may need a reply; then find Route pages I visited last month, and tell me if the retained history is too short; then summarize last week's Work email, including read and unread messages; then read the exact text of yesterday's Personal email with subject Route delivery.

**Required tools:** `lookup_contact`, `summarize_messages`, `search_browser_history`, `summarize_emails`, `view_emails`.
**Ordering constraints:** `lookup_contact` before `summarize_messages`; `summarize_messages` before `search_browser_history`; `search_browser_history` before `summarize_emails`; `summarize_emails` before `view_emails`.
**Checks:** name Mom; resolve uniquely before any dependent contact use day yesterday; Messages only query Route; period last month; disclose coverage boundary account Work; period last week; unread false account Personal; day yesterday; query Route delivery Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Synthetic iMessage/SMS conversations include inbound/outbound attribution
- Browser History opted in; yesterday's sanitized title/path fixture exists; variant-specific state must satisfy: query Route; period last month; disclose coverage boundary
- Work inbox has 4 unread messages spanning several days; variant-specific state must satisfy: account Work; period last week; unread false
- Unique Message-ID <route-delivery@example.test>; code BOX-4821 appears only in its body; variant-specific state must satisfy: account Personal; day yesterday; query Route delivery

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0054 · Explicit sequence

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Please do these in this order: find Route pages I visited last month, and tell me if the retained history is too short; then find last week's Work calendar meeting about the Atlas project; then tell me how far back Wisp can search email; then show saved birthdays in Contacts over the next 30 days; then summarize last week's Work email, including read and unread messages.

**Required tools:** `search_browser_history`, `get_past_events`, `search_coverage`, `contact_dates`, `summarize_emails`.
**Ordering constraints:** `search_browser_history` before `get_past_events`; `get_past_events` before `search_coverage`; `search_coverage` before `contact_dates`; `contact_dates` before `summarize_emails`.
**Checks:** query Route; period last month; disclose coverage boundary past-facing lookup; account Work; query Atlas; do not return future meetings source email; coverage not inbox dump days 30; do not infer missing dates or age account Work; period last week; unread false Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Browser History opted in; yesterday's sanitized title/path fixture exists; variant-specific state must satisfy: query Route; period last month; disclose coverage boundary
- Exactly one Work meeting titled Atlas retrospective occurred last week
- Documented coverage response; not proof of actual full sync
- Two synthetic contacts have saved birthday month/day fields
- Work inbox has 4 unread messages spanning several days; variant-specific state must satisfy: account Work; period last week; unread false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0055 · Explicit sequence

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Please do these in this order: tell me how far back Wisp can search email; then look up Mom's saved phone number and email address; then show saved birthdays in Contacts over the next 30 days; then find Route pages I visited last month, and tell me if the retained history is too short; then summarize last week's Work email, including read and unread messages.

**Required tools:** `search_coverage`, `lookup_contact`, `contact_dates`, `search_browser_history`, `summarize_emails`.
**Ordering constraints:** `search_coverage` before `lookup_contact`; `lookup_contact` before `contact_dates`; `contact_dates` before `search_browser_history`; `search_browser_history` before `summarize_emails`.
**Checks:** source email; coverage not inbox dump name Mom; resolve uniquely before any dependent contact use days 30; do not infer missing dates or age query Route; period last month; disclose coverage boundary account Work; period last week; unread false Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Documented coverage response; not proof of actual full sync
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Two synthetic contacts have saved birthday month/day fields
- Browser History opted in; yesterday's sanitized title/path fixture exists; variant-specific state must satisfy: query Route; period last month; disclose coverage boundary
- Work inbox has 4 unread messages spanning several days; variant-specific state must satisfy: account Work; period last week; unread false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0056 · Explicit sequence

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Please do these in this order: tell me how far back Wisp can search email; then look up Mom's saved phone number and email address; then read the exact text of yesterday's Personal email with subject Route delivery; then show saved birthdays in Contacts over the next 30 days; then find last week's Work calendar meeting about the Atlas project.

**Required tools:** `search_coverage`, `lookup_contact`, `view_emails`, `contact_dates`, `get_past_events`.
**Ordering constraints:** `search_coverage` before `lookup_contact`; `lookup_contact` before `view_emails`; `view_emails` before `contact_dates`; `contact_dates` before `get_past_events`.
**Checks:** source email; coverage not inbox dump name Mom; resolve uniquely before any dependent contact use account Personal; day yesterday; query Route delivery days 30; do not infer missing dates or age past-facing lookup; account Work; query Atlas; do not return future meetings Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Documented coverage response; not proof of actual full sync
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Unique Message-ID <route-delivery@example.test>; code BOX-4821 appears only in its body; variant-specific state must satisfy: account Personal; day yesterday; query Route delivery
- Two synthetic contacts have saved birthday month/day fields
- Exactly one Work meeting titled Atlas retrospective occurred last week

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0057 · Explicit sequence

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Please do these in this order: tell me how far back Wisp can search email; then read the exact packing list from my Notes note titled Route packing; then show saved birthdays in Contacts over the next 30 days; then find the exact pickup address in Mom's text messages from yesterday; then look up Mom's saved phone number and email address.

**Required tools:** `search_coverage`, `search_notes`, `contact_dates`, `view_messages`, `lookup_contact`.
**Ordering constraints:** `search_coverage` before `search_notes`; `search_notes` before `contact_dates`; `contact_dates` before `view_messages`; `view_messages` before `lookup_contact`.
**Checks:** source email; coverage not inbox dump query Route packing; raw content; no date unless requested days 30; do not infer missing dates or age query Mom; day yesterday; exact raw address name Mom; resolve uniquely before any dependent contact use Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Documented coverage response; not proof of actual full sync
- Unique note Route packing with three items; another old note is out of cache
- Two synthetic contacts have saved birthday month/day fields
- Mom yesterday said 42 Example Lane; similar email has a different address
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0058 · Explicit sequence

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Please do these in this order: read the exact packing list from my Notes note titled Route packing; then show saved birthdays in Contacts over the next 30 days; then read the exact text of yesterday's Personal email with subject Route delivery; then look up Mom's saved phone number and email address; then tell me how far back Wisp can search email.

**Required tools:** `search_notes`, `contact_dates`, `view_emails`, `lookup_contact`, `search_coverage`.
**Ordering constraints:** `search_notes` before `contact_dates`; `contact_dates` before `view_emails`; `view_emails` before `lookup_contact`; `lookup_contact` before `search_coverage`.
**Checks:** query Route packing; raw content; no date unless requested days 30; do not infer missing dates or age account Personal; day yesterday; query Route delivery name Mom; resolve uniquely before any dependent contact use source email; coverage not inbox dump Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Unique note Route packing with three items; another old note is out of cache
- Two synthetic contacts have saved birthday month/day fields
- Unique Message-ID <route-delivery@example.test>; code BOX-4821 appears only in its body; variant-specific state must satisfy: account Personal; day yesterday; query Route delivery
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Documented coverage response; not proof of actual full sync

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0059 · Explicit sequence

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Please do these in this order: summarize only yesterday's unread Work emails; then read the exact text of yesterday's Personal email with subject Route delivery; then tell me how far back Wisp can search email; then read Mom's messages from last week word for word; then find Route pages I visited last month, and tell me if the retained history is too short.

**Required tools:** `summarize_emails`, `view_emails`, `search_coverage`, `view_messages`, `search_browser_history`.
**Ordering constraints:** `summarize_emails` before `view_emails`; `view_emails` before `search_coverage`; `search_coverage` before `view_messages`; `view_messages` before `search_browser_history`.
**Checks:** account Work; day yesterday AND unread true; do not drop date filter account Personal; day yesterday; query Route delivery source email; coverage not inbox dump query Mom; period last week query Route; period last month; disclose coverage boundary Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Work inbox has 4 unread messages spanning several days; variant-specific state must satisfy: account Work; day yesterday AND unread true; do not drop date filter
- Unique Message-ID <route-delivery@example.test>; code BOX-4821 appears only in its body; variant-specific state must satisfy: account Personal; day yesterday; query Route delivery
- Documented coverage response; not proof of actual full sync
- Mom yesterday said 42 Example Lane; similar email has a different address; variant-specific state must satisfy: query Mom; period last week
- Browser History opted in; yesterday's sanitized title/path fixture exists; variant-specific state must satisfy: query Route; period last month; disclose coverage boundary

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0060 · Explicit sequence

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Please do these in this order: read the exact text of yesterday's Personal email with subject Route delivery; then read the exact packing list from my Notes note titled Route packing; then tell me how far back Wisp can search email; then look up Mom's saved phone number and email address; then summarize only yesterday's unread Work emails.

**Required tools:** `view_emails`, `search_notes`, `search_coverage`, `lookup_contact`, `summarize_emails`.
**Ordering constraints:** `view_emails` before `search_notes`; `search_notes` before `search_coverage`; `search_coverage` before `lookup_contact`; `lookup_contact` before `summarize_emails`.
**Checks:** account Personal; day yesterday; query Route delivery query Route packing; raw content; no date unless requested source email; coverage not inbox dump name Mom; resolve uniquely before any dependent contact use account Work; day yesterday AND unread true; do not drop date filter Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Unique Message-ID <route-delivery@example.test>; code BOX-4821 appears only in its body; variant-specific state must satisfy: account Personal; day yesterday; query Route delivery
- Unique note Route packing with three items; another old note is out of cache
- Documented coverage response; not proof of actual full sync
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Work inbox has 4 unread messages spanning several days; variant-specific state must satisfy: account Work; day yesterday AND unread true; do not drop date filter

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0061 · Natural compound request

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

I have a few things to finish. Check my Personal calendar for the next seven days. Read the exact text of yesterday's Personal email with subject Route delivery. Look up Mom's saved phone number and email address. Show saved birthdays in Contacts over the next 30 days. Tell me how far back Wisp can search email. Keep the results separate so I can tell what came from where.

**Required tools:** `get_upcoming`, `view_emails`, `lookup_contact`, `contact_dates`, `search_coverage`.
**Checks:** account Personal; days 7 account Personal; day yesterday; query Route delivery name Mom; resolve uniquely before any dependent contact use days 30; do not infer missing dates or age source email; coverage not inbox dump Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Unique Message-ID <route-delivery@example.test>; code BOX-4821 appears only in its body; variant-specific state must satisfy: account Personal; day yesterday; query Route delivery
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Two synthetic contacts have saved birthday month/day fields
- Documented coverage response; not proof of actual full sync

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0062 · Natural compound request

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

I have a few things to finish. Look up Mom's saved phone number and email address. Show saved birthdays in Contacts over the next 30 days. Find Route pages I visited last month, and tell me if the retained history is too short. Check my Personal calendar for the next seven days. Tell me how far back Wisp can search email. Keep the results separate so I can tell what came from where.

**Required tools:** `lookup_contact`, `contact_dates`, `search_browser_history`, `get_upcoming`, `search_coverage`.
**Checks:** name Mom; resolve uniquely before any dependent contact use days 30; do not infer missing dates or age query Route; period last month; disclose coverage boundary account Personal; days 7 source email; coverage not inbox dump Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Two synthetic contacts have saved birthday month/day fields
- Browser History opted in; yesterday's sanitized title/path fixture exists; variant-specific state must satisfy: query Route; period last month; disclose coverage boundary
- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Documented coverage response; not proof of actual full sync

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0063 · Natural compound request

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

I have a few things to finish. Find the Route project page I visited yesterday in Safari or Chrome. Show saved birthdays in Contacts over the next 30 days. Tell me how far back Wisp can search email. Look up Mom's saved phone number and email address. Find the exact pickup address in Mom's text messages from yesterday. Keep the results separate so I can tell what came from where.

**Required tools:** `search_browser_history`, `contact_dates`, `search_coverage`, `lookup_contact`, `view_messages`.
**Checks:** query Route; day yesterday; metadata only days 30; do not infer missing dates or age source email; coverage not inbox dump name Mom; resolve uniquely before any dependent contact use query Mom; day yesterday; exact raw address Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Browser History opted in; yesterday's sanitized title/path fixture exists
- Two synthetic contacts have saved birthday month/day fields
- Documented coverage response; not proof of actual full sync
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Mom yesterday said 42 Example Lane; similar email has a different address

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0064 · Natural compound request

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

I have a few things to finish. Find Route pages I visited last month, and tell me if the retained history is too short. Find last week's Work calendar meeting about the Atlas project. Show saved birthdays in Contacts over the next 30 days. Look up Mom's saved phone number and email address. Tell me how far back Wisp can search email. Keep the results separate so I can tell what came from where.

**Required tools:** `search_browser_history`, `get_past_events`, `contact_dates`, `lookup_contact`, `search_coverage`.
**Checks:** query Route; period last month; disclose coverage boundary past-facing lookup; account Work; query Atlas; do not return future meetings days 30; do not infer missing dates or age name Mom; resolve uniquely before any dependent contact use source email; coverage not inbox dump Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Browser History opted in; yesterday's sanitized title/path fixture exists; variant-specific state must satisfy: query Route; period last month; disclose coverage boundary
- Exactly one Work meeting titled Atlas retrospective occurred last week
- Two synthetic contacts have saved birthday month/day fields
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Documented coverage response; not proof of actual full sync

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0065 · Natural compound request

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

I have a few things to finish. Tell me how far back Wisp can search email. Show saved birthdays in Contacts over the next 30 days. Look up Mom's saved phone number and email address. Read Mom's messages from last week word for word. Find Route pages I visited last month, and tell me if the retained history is too short. Keep the results separate so I can tell what came from where.

**Required tools:** `search_coverage`, `contact_dates`, `lookup_contact`, `view_messages`, `search_browser_history`.
**Checks:** source email; coverage not inbox dump days 30; do not infer missing dates or age name Mom; resolve uniquely before any dependent contact use query Mom; period last week query Route; period last month; disclose coverage boundary Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Documented coverage response; not proof of actual full sync
- Two synthetic contacts have saved birthday month/day fields
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Mom yesterday said 42 Example Lane; similar email has a different address; variant-specific state must satisfy: query Mom; period last week
- Browser History opted in; yesterday's sanitized title/path fixture exists; variant-specific state must satisfy: query Route; period last month; disclose coverage boundary

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0066 · Natural compound request

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

I have a few things to finish. Read the exact packing list from my Notes note titled Route packing. Summarize yesterday's Messages conversations and who may need a reply. Tell me how far back Wisp can search email. Show saved birthdays in Contacts over the next 30 days. Look up Mom's saved phone number and email address. Keep the results separate so I can tell what came from where.

**Required tools:** `search_notes`, `summarize_messages`, `search_coverage`, `contact_dates`, `lookup_contact`.
**Checks:** query Route packing; raw content; no date unless requested day yesterday; Messages only source email; coverage not inbox dump days 30; do not infer missing dates or age name Mom; resolve uniquely before any dependent contact use Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Unique note Route packing with three items; another old note is out of cache
- Synthetic iMessage/SMS conversations include inbound/outbound attribution
- Documented coverage response; not proof of actual full sync
- Two synthetic contacts have saved birthday month/day fields
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0067 · Natural compound request

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

I have a few things to finish. Read the exact text of yesterday's Personal email with subject Route delivery. Tell me how far back Wisp can search email. Summarize yesterday's Messages conversations and who may need a reply. Look up Mom's saved phone number and email address. Read the exact packing list from my Notes note titled Route packing. Keep the results separate so I can tell what came from where.

**Required tools:** `view_emails`, `search_coverage`, `summarize_messages`, `lookup_contact`, `search_notes`.
**Checks:** account Personal; day yesterday; query Route delivery source email; coverage not inbox dump day yesterday; Messages only name Mom; resolve uniquely before any dependent contact use query Route packing; raw content; no date unless requested Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Unique Message-ID <route-delivery@example.test>; code BOX-4821 appears only in its body; variant-specific state must satisfy: account Personal; day yesterday; query Route delivery
- Documented coverage response; not proof of actual full sync
- Synthetic iMessage/SMS conversations include inbound/outbound attribution
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Unique note Route packing with three items; another old note is out of cache

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0068 · Natural compound request

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

I have a few things to finish. Read Mom's messages from last week word for word. Find last week's Work calendar meeting about the Atlas project. Summarize yesterday's Messages conversations and who may need a reply. Show saved birthdays in Contacts over the next 30 days. Check my Personal calendar for the next seven days. Keep the results separate so I can tell what came from where.

**Required tools:** `view_messages`, `get_past_events`, `summarize_messages`, `contact_dates`, `get_upcoming`.
**Checks:** query Mom; period last week past-facing lookup; account Work; query Atlas; do not return future meetings day yesterday; Messages only days 30; do not infer missing dates or age account Personal; days 7 Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Mom yesterday said 42 Example Lane; similar email has a different address; variant-specific state must satisfy: query Mom; period last week
- Exactly one Work meeting titled Atlas retrospective occurred last week
- Synthetic iMessage/SMS conversations include inbound/outbound attribution
- Two synthetic contacts have saved birthday month/day fields
- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0069 · Natural compound request

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

I have a few things to finish. Read Mom's messages from last week word for word. Tell me how far back Wisp can search email. Show saved birthdays in Contacts over the next 30 days. Find Route pages I visited last month, and tell me if the retained history is too short. Look up Mom's saved phone number and email address. Keep the results separate so I can tell what came from where.

**Required tools:** `view_messages`, `search_coverage`, `contact_dates`, `search_browser_history`, `lookup_contact`.
**Checks:** query Mom; period last week source email; coverage not inbox dump days 30; do not infer missing dates or age query Route; period last month; disclose coverage boundary name Mom; resolve uniquely before any dependent contact use Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Mom yesterday said 42 Example Lane; similar email has a different address; variant-specific state must satisfy: query Mom; period last week
- Documented coverage response; not proof of actual full sync
- Two synthetic contacts have saved birthday month/day fields
- Browser History opted in; yesterday's sanitized title/path fixture exists; variant-specific state must satisfy: query Route; period last month; disclose coverage boundary
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0070 · Natural compound request

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

I have a few things to finish. Read Mom's messages from last week word for word. Tell me how far back Wisp can search email. Find last week's Work calendar meeting about the Atlas project. Show saved birthdays in Contacts over the next 30 days. Summarize last week's Work email, including read and unread messages. Keep the results separate so I can tell what came from where.

**Required tools:** `view_messages`, `search_coverage`, `get_past_events`, `contact_dates`, `summarize_emails`.
**Checks:** query Mom; period last week source email; coverage not inbox dump past-facing lookup; account Work; query Atlas; do not return future meetings days 30; do not infer missing dates or age account Work; period last week; unread false Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Mom yesterday said 42 Example Lane; similar email has a different address; variant-specific state must satisfy: query Mom; period last week
- Documented coverage response; not proof of actual full sync
- Exactly one Work meeting titled Atlas retrospective occurred last week
- Two synthetic contacts have saved birthday month/day fields
- Work inbox has 4 unread messages spanning several days; variant-specific state must satisfy: account Work; period last week; unread false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0071 · Scoped execution

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

For these tasks, use only the named sources and targets: find last week's Work calendar meeting about the Atlas project; then tell me how far back Wisp can search email; then look up Mom's saved phone number and email address; then show saved birthdays in Contacts over the next 30 days; then summarize only yesterday's unread Work emails. Leave everything else unchanged.

**Required tools:** `get_past_events`, `search_coverage`, `lookup_contact`, `contact_dates`, `summarize_emails`.
**Ordering constraints:** `get_past_events` before `search_coverage`; `search_coverage` before `lookup_contact`; `lookup_contact` before `contact_dates`; `contact_dates` before `summarize_emails`.
**Checks:** past-facing lookup; account Work; query Atlas; do not return future meetings source email; coverage not inbox dump name Mom; resolve uniquely before any dependent contact use days 30; do not infer missing dates or age account Work; day yesterday AND unread true; do not drop date filter Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Exactly one Work meeting titled Atlas retrospective occurred last week
- Documented coverage response; not proof of actual full sync
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Two synthetic contacts have saved birthday month/day fields
- Work inbox has 4 unread messages spanning several days; variant-specific state must satisfy: account Work; day yesterday AND unread true; do not drop date filter

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0072 · Scoped execution

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

For these tasks, use only the named sources and targets: look up Mom's saved phone number and email address; then tell me how far back Wisp can search email; then read Mom's messages from last week word for word; then summarize yesterday's Messages conversations and who may need a reply; then show saved birthdays in Contacts over the next 30 days. Leave everything else unchanged.

**Required tools:** `lookup_contact`, `search_coverage`, `view_messages`, `summarize_messages`, `contact_dates`.
**Ordering constraints:** `lookup_contact` before `search_coverage`; `search_coverage` before `view_messages`; `view_messages` before `summarize_messages`; `summarize_messages` before `contact_dates`.
**Checks:** name Mom; resolve uniquely before any dependent contact use source email; coverage not inbox dump query Mom; period last week day yesterday; Messages only days 30; do not infer missing dates or age Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Documented coverage response; not proof of actual full sync
- Mom yesterday said 42 Example Lane; similar email has a different address; variant-specific state must satisfy: query Mom; period last week
- Synthetic iMessage/SMS conversations include inbound/outbound attribution
- Two synthetic contacts have saved birthday month/day fields

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0073 · Scoped execution

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

For these tasks, use only the named sources and targets: find Route pages I visited last month, and tell me if the retained history is too short; then read the exact text of yesterday's Personal email with subject Route delivery; then find last week's Work calendar meeting about the Atlas project; then summarize last week's Work email, including read and unread messages; then check my Personal calendar for the next seven days. Leave everything else unchanged.

**Required tools:** `search_browser_history`, `view_emails`, `get_past_events`, `summarize_emails`, `get_upcoming`.
**Ordering constraints:** `search_browser_history` before `view_emails`; `view_emails` before `get_past_events`; `get_past_events` before `summarize_emails`; `summarize_emails` before `get_upcoming`.
**Checks:** query Route; period last month; disclose coverage boundary account Personal; day yesterday; query Route delivery past-facing lookup; account Work; query Atlas; do not return future meetings account Work; period last week; unread false account Personal; days 7 Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Browser History opted in; yesterday's sanitized title/path fixture exists; variant-specific state must satisfy: query Route; period last month; disclose coverage boundary
- Unique Message-ID <route-delivery@example.test>; code BOX-4821 appears only in its body; variant-specific state must satisfy: account Personal; day yesterday; query Route delivery
- Exactly one Work meeting titled Atlas retrospective occurred last week
- Work inbox has 4 unread messages spanning several days; variant-specific state must satisfy: account Work; period last week; unread false
- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0074 · Scoped execution

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

For these tasks, use only the named sources and targets: find Route pages I visited last month, and tell me if the retained history is too short; then read the exact text of yesterday's Personal email with subject Route delivery; then tell me how far back Wisp can search email; then read Mom's messages from last week word for word; then look up Mom's saved phone number and email address. Leave everything else unchanged.

**Required tools:** `search_browser_history`, `view_emails`, `search_coverage`, `view_messages`, `lookup_contact`.
**Ordering constraints:** `search_browser_history` before `view_emails`; `view_emails` before `search_coverage`; `search_coverage` before `view_messages`; `view_messages` before `lookup_contact`.
**Checks:** query Route; period last month; disclose coverage boundary account Personal; day yesterday; query Route delivery source email; coverage not inbox dump query Mom; period last week name Mom; resolve uniquely before any dependent contact use Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Browser History opted in; yesterday's sanitized title/path fixture exists; variant-specific state must satisfy: query Route; period last month; disclose coverage boundary
- Unique Message-ID <route-delivery@example.test>; code BOX-4821 appears only in its body; variant-specific state must satisfy: account Personal; day yesterday; query Route delivery
- Documented coverage response; not proof of actual full sync
- Mom yesterday said 42 Example Lane; similar email has a different address; variant-specific state must satisfy: query Mom; period last week
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0075 · Scoped execution

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

For these tasks, use only the named sources and targets: tell me how far back Wisp can search email; then show saved birthdays in Contacts over the next 30 days; then look up Mom's saved phone number and email address; then read the exact packing list from my Notes note titled Route packing; then read Mom's messages from last week word for word. Leave everything else unchanged.

**Required tools:** `search_coverage`, `contact_dates`, `lookup_contact`, `search_notes`, `view_messages`.
**Ordering constraints:** `search_coverage` before `contact_dates`; `contact_dates` before `lookup_contact`; `lookup_contact` before `search_notes`; `search_notes` before `view_messages`.
**Checks:** source email; coverage not inbox dump days 30; do not infer missing dates or age name Mom; resolve uniquely before any dependent contact use query Route packing; raw content; no date unless requested query Mom; period last week Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Documented coverage response; not proof of actual full sync
- Two synthetic contacts have saved birthday month/day fields
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Unique note Route packing with three items; another old note is out of cache
- Mom yesterday said 42 Example Lane; similar email has a different address; variant-specific state must satisfy: query Mom; period last week

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0076 · Scoped execution

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

For these tasks, use only the named sources and targets: summarize last week's Work email, including read and unread messages; then check my Personal calendar for the next seven days; then look up Mom's saved phone number and email address; then summarize yesterday's Messages conversations and who may need a reply; then show saved birthdays in Contacts over the next 30 days. Leave everything else unchanged.

**Required tools:** `summarize_emails`, `get_upcoming`, `lookup_contact`, `summarize_messages`, `contact_dates`.
**Ordering constraints:** `summarize_emails` before `get_upcoming`; `get_upcoming` before `lookup_contact`; `lookup_contact` before `summarize_messages`; `summarize_messages` before `contact_dates`.
**Checks:** account Work; period last week; unread false account Personal; days 7 name Mom; resolve uniquely before any dependent contact use day yesterday; Messages only days 30; do not infer missing dates or age Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Work inbox has 4 unread messages spanning several days; variant-specific state must satisfy: account Work; period last week; unread false
- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Synthetic iMessage/SMS conversations include inbound/outbound attribution
- Two synthetic contacts have saved birthday month/day fields

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0077 · Scoped execution

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

For these tasks, use only the named sources and targets: summarize yesterday's Messages conversations and who may need a reply; then show saved birthdays in Contacts over the next 30 days; then look up Mom's saved phone number and email address; then read the exact packing list from my Notes note titled Route packing; then tell me how far back Wisp can search email. Leave everything else unchanged.

**Required tools:** `summarize_messages`, `contact_dates`, `lookup_contact`, `search_notes`, `search_coverage`.
**Ordering constraints:** `summarize_messages` before `contact_dates`; `contact_dates` before `lookup_contact`; `lookup_contact` before `search_notes`; `search_notes` before `search_coverage`.
**Checks:** day yesterday; Messages only days 30; do not infer missing dates or age name Mom; resolve uniquely before any dependent contact use query Route packing; raw content; no date unless requested source email; coverage not inbox dump Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic iMessage/SMS conversations include inbound/outbound attribution
- Two synthetic contacts have saved birthday month/day fields
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Unique note Route packing with three items; another old note is out of cache
- Documented coverage response; not proof of actual full sync

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0078 · Scoped execution

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

For these tasks, use only the named sources and targets: summarize yesterday's Messages conversations and who may need a reply; then look up Mom's saved phone number and email address; then check my Work calendar for tomorrow and list the reminders separately; then show saved birthdays in Contacts over the next 30 days; then tell me how far back Wisp can search email. Leave everything else unchanged.

**Required tools:** `summarize_messages`, `lookup_contact`, `get_upcoming`, `contact_dates`, `search_coverage`.
**Ordering constraints:** `summarize_messages` before `lookup_contact`; `lookup_contact` before `get_upcoming`; `get_upcoming` before `contact_dates`; `contact_dates` before `search_coverage`.
**Checks:** day yesterday; Messages only name Mom; resolve uniquely before any dependent contact use account Work; include tomorrow; distinguish events from reminders days 30; do not infer missing dates or age source email; coverage not inbox dump Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic iMessage/SMS conversations include inbound/outbound attribution
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder
- Two synthetic contacts have saved birthday month/day fields
- Documented coverage response; not proof of actual full sync

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0079 · Scoped execution

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

For these tasks, use only the named sources and targets: read Mom's messages from last week word for word; then find last week's Work calendar meeting about the Atlas project; then tell me how far back Wisp can search email; then check my Personal calendar for the next seven days; then show saved birthdays in Contacts over the next 30 days. Leave everything else unchanged.

**Required tools:** `view_messages`, `get_past_events`, `search_coverage`, `get_upcoming`, `contact_dates`.
**Ordering constraints:** `view_messages` before `get_past_events`; `get_past_events` before `search_coverage`; `search_coverage` before `get_upcoming`; `get_upcoming` before `contact_dates`.
**Checks:** query Mom; period last week past-facing lookup; account Work; query Atlas; do not return future meetings source email; coverage not inbox dump account Personal; days 7 days 30; do not infer missing dates or age Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Mom yesterday said 42 Example Lane; similar email has a different address; variant-specific state must satisfy: query Mom; period last week
- Exactly one Work meeting titled Atlas retrospective occurred last week
- Documented coverage response; not proof of actual full sync
- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Two synthetic contacts have saved birthday month/day fields

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0080 · Scoped execution

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

For these tasks, use only the named sources and targets: read Mom's messages from last week word for word; then check my Personal calendar for the next seven days; then summarize only yesterday's unread Work emails; then read the exact text of yesterday's Personal email with subject Route delivery; then find Route pages I visited last month, and tell me if the retained history is too short. Leave everything else unchanged.

**Required tools:** `view_messages`, `get_upcoming`, `summarize_emails`, `view_emails`, `search_browser_history`.
**Ordering constraints:** `view_messages` before `get_upcoming`; `get_upcoming` before `summarize_emails`; `summarize_emails` before `view_emails`; `view_emails` before `search_browser_history`.
**Checks:** query Mom; period last week account Personal; days 7 account Work; day yesterday AND unread true; do not drop date filter account Personal; day yesterday; query Route delivery query Route; period last month; disclose coverage boundary Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Mom yesterday said 42 Example Lane; similar email has a different address; variant-specific state must satisfy: query Mom; period last week
- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Work inbox has 4 unread messages spanning several days; variant-specific state must satisfy: account Work; day yesterday AND unread true; do not drop date filter
- Unique Message-ID <route-delivery@example.test>; code BOX-4821 appears only in its body; variant-specific state must satisfy: account Personal; day yesterday; query Route delivery
- Browser History opted in; yesterday's sanitized title/path fixture exists; variant-specific state must satisfy: query Route; period last month; disclose coverage boundary

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0081 · Late constraints

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Show saved birthdays in Contacts over the next 30 days. Tell me how far back Wisp can search email. Read the exact delivery code in the Work email with subject Route delivery. Look up Mom's saved phone number and email address. Find last week's Work calendar meeting about the Atlas project. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `contact_dates`, `search_coverage`, `view_emails`, `lookup_contact`, `get_past_events`.
**Checks:** days 30; do not infer missing dates or age source email; coverage not inbox dump query Route delivery; account Work; raw body; no summary substitution name Mom; resolve uniquely before any dependent contact use past-facing lookup; account Work; query Atlas; do not return future meetings Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Two synthetic contacts have saved birthday month/day fields
- Documented coverage response; not proof of actual full sync
- Unique Message-ID <route-delivery@example.test>; code BOX-4821 appears only in its body
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Exactly one Work meeting titled Atlas retrospective occurred last week

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0082 · Late constraints

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Show saved birthdays in Contacts over the next 30 days. Read the exact text of yesterday's Personal email with subject Route delivery. Look up Mom's saved phone number and email address. Tell me how far back Wisp can search email. Find Route pages I visited last month, and tell me if the retained history is too short. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `contact_dates`, `view_emails`, `lookup_contact`, `search_coverage`, `search_browser_history`.
**Checks:** days 30; do not infer missing dates or age account Personal; day yesterday; query Route delivery name Mom; resolve uniquely before any dependent contact use source email; coverage not inbox dump query Route; period last month; disclose coverage boundary Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Two synthetic contacts have saved birthday month/day fields
- Unique Message-ID <route-delivery@example.test>; code BOX-4821 appears only in its body; variant-specific state must satisfy: account Personal; day yesterday; query Route delivery
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Documented coverage response; not proof of actual full sync
- Browser History opted in; yesterday's sanitized title/path fixture exists; variant-specific state must satisfy: query Route; period last month; disclose coverage boundary

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0083 · Late constraints

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Look up Mom's saved phone number and email address. Find last week's Work calendar meeting about the Atlas project. Tell me how far back Wisp can search email. Check my Personal calendar for the next seven days. Show saved birthdays in Contacts over the next 30 days. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `lookup_contact`, `get_past_events`, `search_coverage`, `get_upcoming`, `contact_dates`.
**Checks:** name Mom; resolve uniquely before any dependent contact use past-facing lookup; account Work; query Atlas; do not return future meetings source email; coverage not inbox dump account Personal; days 7 days 30; do not infer missing dates or age Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Exactly one Work meeting titled Atlas retrospective occurred last week
- Documented coverage response; not proof of actual full sync
- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Two synthetic contacts have saved birthday month/day fields

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0084 · Late constraints

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Tell me how far back Wisp can search email. Look up Mom's saved phone number and email address. Show saved birthdays in Contacts over the next 30 days. Summarize yesterday's Messages conversations and who may need a reply. Read the exact text of yesterday's Personal email with subject Route delivery. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `search_coverage`, `lookup_contact`, `contact_dates`, `summarize_messages`, `view_emails`.
**Checks:** source email; coverage not inbox dump name Mom; resolve uniquely before any dependent contact use days 30; do not infer missing dates or age day yesterday; Messages only account Personal; day yesterday; query Route delivery Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Documented coverage response; not proof of actual full sync
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Two synthetic contacts have saved birthday month/day fields
- Synthetic iMessage/SMS conversations include inbound/outbound attribution
- Unique Message-ID <route-delivery@example.test>; code BOX-4821 appears only in its body; variant-specific state must satisfy: account Personal; day yesterday; query Route delivery

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0085 · Late constraints

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Tell me how far back Wisp can search email. Read the exact packing list from my Notes note titled Route packing. Read the exact text of yesterday's Personal email with subject Route delivery. Look up Mom's saved phone number and email address. Show saved birthdays in Contacts over the next 30 days. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `search_coverage`, `search_notes`, `view_emails`, `lookup_contact`, `contact_dates`.
**Checks:** source email; coverage not inbox dump query Route packing; raw content; no date unless requested account Personal; day yesterday; query Route delivery name Mom; resolve uniquely before any dependent contact use days 30; do not infer missing dates or age Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Documented coverage response; not proof of actual full sync
- Unique note Route packing with three items; another old note is out of cache
- Unique Message-ID <route-delivery@example.test>; code BOX-4821 appears only in its body; variant-specific state must satisfy: account Personal; day yesterday; query Route delivery
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Two synthetic contacts have saved birthday month/day fields

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0086 · Late constraints

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Read the exact packing list from my Notes note titled Route packing. Find last week's Work calendar meeting about the Atlas project. Tell me how far back Wisp can search email. Summarize yesterday's Messages conversations and who may need a reply. Summarize last week's Work email, including read and unread messages. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `search_notes`, `get_past_events`, `search_coverage`, `summarize_messages`, `summarize_emails`.
**Checks:** query Route packing; raw content; no date unless requested past-facing lookup; account Work; query Atlas; do not return future meetings source email; coverage not inbox dump day yesterday; Messages only account Work; period last week; unread false Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Unique note Route packing with three items; another old note is out of cache
- Exactly one Work meeting titled Atlas retrospective occurred last week
- Documented coverage response; not proof of actual full sync
- Synthetic iMessage/SMS conversations include inbound/outbound attribution
- Work inbox has 4 unread messages spanning several days; variant-specific state must satisfy: account Work; period last week; unread false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0087 · Late constraints

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Read the exact packing list from my Notes note titled Route packing. Find Route pages I visited last month, and tell me if the retained history is too short. Check my Personal calendar for the next seven days. Summarize yesterday's Messages conversations and who may need a reply. Look up Mom's saved phone number and email address. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `search_notes`, `search_browser_history`, `get_upcoming`, `summarize_messages`, `lookup_contact`.
**Checks:** query Route packing; raw content; no date unless requested query Route; period last month; disclose coverage boundary account Personal; days 7 day yesterday; Messages only name Mom; resolve uniquely before any dependent contact use Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Unique note Route packing with three items; another old note is out of cache
- Browser History opted in; yesterday's sanitized title/path fixture exists; variant-specific state must satisfy: query Route; period last month; disclose coverage boundary
- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Synthetic iMessage/SMS conversations include inbound/outbound attribution
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0088 · Late constraints

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Read the exact packing list from my Notes note titled Route packing. Tell me how far back Wisp can search email. Find Route pages I visited last month, and tell me if the retained history is too short. Read the exact text of yesterday's Personal email with subject Route delivery. Check my Personal calendar for the next seven days. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `search_notes`, `search_coverage`, `search_browser_history`, `view_emails`, `get_upcoming`.
**Checks:** query Route packing; raw content; no date unless requested source email; coverage not inbox dump query Route; period last month; disclose coverage boundary account Personal; day yesterday; query Route delivery account Personal; days 7 Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Unique note Route packing with three items; another old note is out of cache
- Documented coverage response; not proof of actual full sync
- Browser History opted in; yesterday's sanitized title/path fixture exists; variant-specific state must satisfy: query Route; period last month; disclose coverage boundary
- Unique Message-ID <route-delivery@example.test>; code BOX-4821 appears only in its body; variant-specific state must satisfy: account Personal; day yesterday; query Route delivery
- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0089 · Late constraints

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Summarize yesterday's Messages conversations and who may need a reply. Check my Personal calendar for the next seven days. Read the exact packing list from my Notes note titled Route packing. Look up Mom's saved phone number and email address. Find last week's Work calendar meeting about the Atlas project. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `summarize_messages`, `get_upcoming`, `search_notes`, `lookup_contact`, `get_past_events`.
**Checks:** day yesterday; Messages only account Personal; days 7 query Route packing; raw content; no date unless requested name Mom; resolve uniquely before any dependent contact use past-facing lookup; account Work; query Atlas; do not return future meetings Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic iMessage/SMS conversations include inbound/outbound attribution
- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Unique note Route packing with three items; another old note is out of cache
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Exactly one Work meeting titled Atlas retrospective occurred last week

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0090 · Late constraints

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Read Mom's messages from last week word for word. Find last week's Work calendar meeting about the Atlas project. Find Route pages I visited last month, and tell me if the retained history is too short. Look up Mom's saved phone number and email address. Summarize yesterday's Messages conversations and who may need a reply. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `view_messages`, `get_past_events`, `search_browser_history`, `lookup_contact`, `summarize_messages`.
**Checks:** query Mom; period last week past-facing lookup; account Work; query Atlas; do not return future meetings query Route; period last month; disclose coverage boundary name Mom; resolve uniquely before any dependent contact use day yesterday; Messages only Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Mom yesterday said 42 Example Lane; similar email has a different address; variant-specific state must satisfy: query Mom; period last week
- Exactly one Work meeting titled Atlas retrospective occurred last week
- Browser History opted in; yesterday's sanitized title/path fixture exists; variant-specific state must satisfy: query Route; period last month; disclose coverage boundary
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Synthetic iMessage/SMS conversations include inbound/outbound attribution

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0091 · Colloquial with interruptions

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Could you find last week's Work calendar meeting about the Atlas project; then summarize last week's Work email, including read and unread messages; then check my Personal calendar for the next seven days; then read the exact text of yesterday's Personal email with subject Route delivery; then show saved birthdays in Contacts over the next 30 days? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `get_past_events`, `summarize_emails`, `get_upcoming`, `view_emails`, `contact_dates`.
**Ordering constraints:** `get_past_events` before `summarize_emails`; `summarize_emails` before `get_upcoming`; `get_upcoming` before `view_emails`; `view_emails` before `contact_dates`.
**Checks:** past-facing lookup; account Work; query Atlas; do not return future meetings account Work; period last week; unread false account Personal; days 7 account Personal; day yesterday; query Route delivery days 30; do not infer missing dates or age Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Exactly one Work meeting titled Atlas retrospective occurred last week
- Work inbox has 4 unread messages spanning several days; variant-specific state must satisfy: account Work; period last week; unread false
- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Unique Message-ID <route-delivery@example.test>; code BOX-4821 appears only in its body; variant-specific state must satisfy: account Personal; day yesterday; query Route delivery
- Two synthetic contacts have saved birthday month/day fields

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0092 · Colloquial with interruptions

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Could you find last week's Work calendar meeting about the Atlas project; then summarize yesterday's Messages conversations and who may need a reply; then show saved birthdays in Contacts over the next 30 days; then look up Mom's saved phone number and email address; then read the exact packing list from my Notes note titled Route packing? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `get_past_events`, `summarize_messages`, `contact_dates`, `lookup_contact`, `search_notes`.
**Ordering constraints:** `get_past_events` before `summarize_messages`; `summarize_messages` before `contact_dates`; `contact_dates` before `lookup_contact`; `lookup_contact` before `search_notes`.
**Checks:** past-facing lookup; account Work; query Atlas; do not return future meetings day yesterday; Messages only days 30; do not infer missing dates or age name Mom; resolve uniquely before any dependent contact use query Route packing; raw content; no date unless requested Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Exactly one Work meeting titled Atlas retrospective occurred last week
- Synthetic iMessage/SMS conversations include inbound/outbound attribution
- Two synthetic contacts have saved birthday month/day fields
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Unique note Route packing with three items; another old note is out of cache

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0093 · Colloquial with interruptions

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Could you find last week's Work calendar meeting about the Atlas project; then read Mom's messages from last week word for word; then find Route pages I visited last month, and tell me if the retained history is too short; then check my Personal calendar for the next seven days; then show saved birthdays in Contacts over the next 30 days? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `get_past_events`, `view_messages`, `search_browser_history`, `get_upcoming`, `contact_dates`.
**Ordering constraints:** `get_past_events` before `view_messages`; `view_messages` before `search_browser_history`; `search_browser_history` before `get_upcoming`; `get_upcoming` before `contact_dates`.
**Checks:** past-facing lookup; account Work; query Atlas; do not return future meetings query Mom; period last week query Route; period last month; disclose coverage boundary account Personal; days 7 days 30; do not infer missing dates or age Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Exactly one Work meeting titled Atlas retrospective occurred last week
- Mom yesterday said 42 Example Lane; similar email has a different address; variant-specific state must satisfy: query Mom; period last week
- Browser History opted in; yesterday's sanitized title/path fixture exists; variant-specific state must satisfy: query Route; period last month; disclose coverage boundary
- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Two synthetic contacts have saved birthday month/day fields

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0094 · Colloquial with interruptions

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Could you look up Mom's saved phone number and email address; then show saved birthdays in Contacts over the next 30 days; then read the exact packing list from my Notes note titled Route packing; then tell me how far back Wisp can search email; then check my Personal calendar for the next seven days? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `lookup_contact`, `contact_dates`, `search_notes`, `search_coverage`, `get_upcoming`.
**Ordering constraints:** `lookup_contact` before `contact_dates`; `contact_dates` before `search_notes`; `search_notes` before `search_coverage`; `search_coverage` before `get_upcoming`.
**Checks:** name Mom; resolve uniquely before any dependent contact use days 30; do not infer missing dates or age query Route packing; raw content; no date unless requested source email; coverage not inbox dump account Personal; days 7 Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Two synthetic contacts have saved birthday month/day fields
- Unique note Route packing with three items; another old note is out of cache
- Documented coverage response; not proof of actual full sync
- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0095 · Colloquial with interruptions

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Could you look up Mom's saved phone number and email address; then tell me how far back Wisp can search email; then show saved birthdays in Contacts over the next 30 days; then find last week's Work calendar meeting about the Atlas project; then summarize my unread Work email without imposing a date range? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `lookup_contact`, `search_coverage`, `contact_dates`, `get_past_events`, `summarize_emails`.
**Ordering constraints:** `lookup_contact` before `search_coverage`; `search_coverage` before `contact_dates`; `contact_dates` before `get_past_events`; `get_past_events` before `summarize_emails`.
**Checks:** name Mom; resolve uniquely before any dependent contact use source email; coverage not inbox dump days 30; do not infer missing dates or age past-facing lookup; account Work; query Atlas; do not return future meetings unread true; account Work; no day/period Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Documented coverage response; not proof of actual full sync
- Two synthetic contacts have saved birthday month/day fields
- Exactly one Work meeting titled Atlas retrospective occurred last week
- Work inbox has 4 unread messages spanning several days

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0096 · Colloquial with interruptions

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Could you look up Mom's saved phone number and email address; then summarize yesterday's Messages conversations and who may need a reply; then tell me how far back Wisp can search email; then show saved birthdays in Contacts over the next 30 days; then read Mom's messages from last week word for word? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `lookup_contact`, `summarize_messages`, `search_coverage`, `contact_dates`, `view_messages`.
**Ordering constraints:** `lookup_contact` before `summarize_messages`; `summarize_messages` before `search_coverage`; `search_coverage` before `contact_dates`; `contact_dates` before `view_messages`.
**Checks:** name Mom; resolve uniquely before any dependent contact use day yesterday; Messages only source email; coverage not inbox dump days 30; do not infer missing dates or age query Mom; period last week Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Synthetic iMessage/SMS conversations include inbound/outbound attribution
- Documented coverage response; not proof of actual full sync
- Two synthetic contacts have saved birthday month/day fields
- Mom yesterday said 42 Example Lane; similar email has a different address; variant-specific state must satisfy: query Mom; period last week

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0097 · Colloquial with interruptions

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Could you tell me how far back Wisp can search email; then check my Personal calendar for the next seven days; then read Mom's messages from last week word for word; then read the exact text of yesterday's Personal email with subject Route delivery; then summarize last week's Work email, including read and unread messages? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `search_coverage`, `get_upcoming`, `view_messages`, `view_emails`, `summarize_emails`.
**Ordering constraints:** `search_coverage` before `get_upcoming`; `get_upcoming` before `view_messages`; `view_messages` before `view_emails`; `view_emails` before `summarize_emails`.
**Checks:** source email; coverage not inbox dump account Personal; days 7 query Mom; period last week account Personal; day yesterday; query Route delivery account Work; period last week; unread false Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Documented coverage response; not proof of actual full sync
- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Mom yesterday said 42 Example Lane; similar email has a different address; variant-specific state must satisfy: query Mom; period last week
- Unique Message-ID <route-delivery@example.test>; code BOX-4821 appears only in its body; variant-specific state must satisfy: account Personal; day yesterday; query Route delivery
- Work inbox has 4 unread messages spanning several days; variant-specific state must satisfy: account Work; period last week; unread false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0098 · Colloquial with interruptions

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Could you read the exact packing list from my Notes note titled Route packing; then show saved birthdays in Contacts over the next 30 days; then look up Mom's saved phone number and email address; then summarize only yesterday's unread Work emails; then tell me how far back Wisp can search email? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `search_notes`, `contact_dates`, `lookup_contact`, `summarize_emails`, `search_coverage`.
**Ordering constraints:** `search_notes` before `contact_dates`; `contact_dates` before `lookup_contact`; `lookup_contact` before `summarize_emails`; `summarize_emails` before `search_coverage`.
**Checks:** query Route packing; raw content; no date unless requested days 30; do not infer missing dates or age name Mom; resolve uniquely before any dependent contact use account Work; day yesterday AND unread true; do not drop date filter source email; coverage not inbox dump Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Unique note Route packing with three items; another old note is out of cache
- Two synthetic contacts have saved birthday month/day fields
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Work inbox has 4 unread messages spanning several days; variant-specific state must satisfy: account Work; day yesterday AND unread true; do not drop date filter
- Documented coverage response; not proof of actual full sync

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0099 · Colloquial with interruptions

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Could you read the exact packing list from my Notes note titled Route packing; then look up Mom's saved phone number and email address; then read Mom's messages from last week word for word; then show saved birthdays in Contacts over the next 30 days; then summarize yesterday's Messages conversations and who may need a reply? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `search_notes`, `lookup_contact`, `view_messages`, `contact_dates`, `summarize_messages`.
**Ordering constraints:** `search_notes` before `lookup_contact`; `lookup_contact` before `view_messages`; `view_messages` before `contact_dates`; `contact_dates` before `summarize_messages`.
**Checks:** query Route packing; raw content; no date unless requested name Mom; resolve uniquely before any dependent contact use query Mom; period last week days 30; do not infer missing dates or age day yesterday; Messages only Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Unique note Route packing with three items; another old note is out of cache
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Mom yesterday said 42 Example Lane; similar email has a different address; variant-specific state must satisfy: query Mom; period last week
- Two synthetic contacts have saved birthday month/day fields
- Synthetic iMessage/SMS conversations include inbound/outbound attribution

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0100 · Colloquial with interruptions

**Focus:** Preserve named accounts, date windows, source attribution, and exact wording.

**Prompt:**

Could you read Mom's messages from last week word for word; then show saved birthdays in Contacts over the next 30 days; then look up Mom's saved phone number and email address; then tell me how far back Wisp can search email; then summarize last week's Work email, including read and unread messages? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `view_messages`, `contact_dates`, `lookup_contact`, `search_coverage`, `summarize_emails`.
**Ordering constraints:** `view_messages` before `contact_dates`; `contact_dates` before `lookup_contact`; `lookup_contact` before `search_coverage`; `search_coverage` before `summarize_emails`.
**Checks:** query Mom; period last week days 30; do not infer missing dates or age name Mom; resolve uniquely before any dependent contact use source email; coverage not inbox dump account Work; period last week; unread false Preserve named accounts, date windows, source attribution, and exact wording.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Mom yesterday said 42 Example Lane; similar email has a different address; variant-specific state must satisfy: query Mom; period last week
- Two synthetic contacts have saved birthday month/day fields
- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Documented coverage response; not proof of actual full sync
- Work inbox has 4 unread messages spanning several days; variant-specific state must satisfy: account Work; period last week; unread false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.
