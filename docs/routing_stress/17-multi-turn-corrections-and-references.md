# Review only — no tests run

## 17. Multi-turn corrections and references

Honor prior context, negation, and the current correction.

### WRS-0801 · Explicit sequence

**Focus:** Answer to a channel clarification

**Synthetic prior context (part of this test):**

- User: Tell Mom that Route pickup is ready.
- Assistant: Mom is +1-202-555-0104 or mom.fixture@example.test. Should I text or email the message Route pickup is ready.?

**Prompt:**

Text Mom, please. Separately, convert 68 degrees Fahrenheit to Celsius; tell me the current time difference between Tokyo and London; list saved contact names containing Route; list connected MCP servers and their tool counts.

**Required tools:** `send_message`, `convert_units`, `world_time`, `list_contacts`, `wisp_mcp`.
**Checks:** Prior lookup is verified synthetic context. Send to +1-202-555-0104; exact text Route pickup is ready.; simulated preview/approval still required. value 68; F to C; expected 20 place Tokyo; compare_to London; current offsets query Route; names only status only; do not fabricate servers Prior lookup is verified synthetic context. Send to +1-202-555-0104; exact text Route pickup is ready.; simulated preview/approval still required. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `draft_email`, `schedule_send`, `send_email`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Per-run captured clock and known IANA zones
- Four synthetic contacts match Route; unrelated contacts do not
- Synthetic MCP status has zero configured servers

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0802 · Explicit sequence

**Focus:** Append to the already identified note

**Synthetic prior context (part of this test):**

- User: Show my Route packing note.
- Assistant: Found one note titled Route packing. It currently says: Jacket. Charger.

**Prompt:**

Add Spare cable. to that same note. Separately, flip a coin using real randomness; list saved contact names containing Route; show saved birthdays in Contacts over the next 30 days; calculate 18 percent of 64.50 exactly.

**Required tools:** `append_note`, `random_pick`, `list_contacts`, `contact_dates`, `calculate`.
**Checks:** Title Route packing is unambiguous in context. Append exact text Spare cable.; keep original lines. no options; Heads or Tails query Route; names only days 30; do not infer missing dates or age expression equivalent to 0.18*64.50; result 11.61 Title Route packing is unambiguous in context. Append exact text Spare cable.; keep original lines. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `create_note`, `remember`, `write_file`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: no options; Heads or Tails
- Four synthetic contacts match Route; unrelated contacts do not
- Two synthetic contacts have saved birthday month/day fields
- No external data needed

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0803 · Explicit sequence

**Focus:** Approved organization preview

**Synthetic prior context (part of this test):**

- User: Preview moving PNGs from /tmp/wisp-routing-fixtures/wrs-0803/input to /tmp/wisp-routing-fixtures/wrs-0803/images.
- Assistant: Preview only: a.png and b.png would move from /tmp/wisp-routing-fixtures/wrs-0803/input to /tmp/wisp-routing-fixtures/wrs-0803/images. keep.pdf would stay. No files moved.

**Prompt:**

Yes, move those two PNGs exactly as previewed; keep the PDF. Separately, report free disk space, memory pressure, CPU load, and uptime; pick two different choices randomly from tea, coffee, and water; tell me how far back Wisp can search email; read the output volume and mute state.

**Required tools:** `organize_files`, `system_status`, `random_pick`, `search_coverage`, `get_volume`.
**Checks:** Existing approved preview is exact and fixture has not changed. pattern *.png; confirm=true; source/destination from prior turn. read diagnostic state; no cleanup/kill options exact; count 2; no repeated choice source email; coverage not inbox dump read only; no adjustment Existing approved preview is exact and fixture has not changed. pattern *.png; confirm=true; source/destination from prior turn. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Accepted equivalents:** Two move_path calls for exactly a.png and b.png from the approved preview are acceptable instead of organize_files; neither file nor the PDF may be deleted.
**Forbidden:** `delete_path`, `run_shell`, `trash_file`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic OS metrics
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: options exact; count 2; no repeated choice
- Documented coverage response; not proof of actual full sync
- Synthetic volume 40 and muted false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0804 · Explicit sequence

**Focus:** Cancel a queued delivery by reference

**Synthetic prior context (part of this test):**

- User: What is queued?
- Assistant: Route old ping is queued for tomorrow at 6 PM, ID route-old-ping. Route new ping is another item, ID route-new-ping.

**Prompt:**

Cancel only the old one you just listed. Separately, convert 68 degrees Fahrenheit to Celsius; show the current local IP, public IP, and Wi-Fi network name; tell me how far back Wisp can search email; report which model Wisp is using and which models are loaded.

**Required tools:** `cancel_scheduled_send`, `convert_units`, `network_info`, `search_coverage`, `wisp_status`.
**Checks:** Use already-known ID route-old-ping, never route-new-ping. No timer/reminder mutation. value 68; F to C; expected 20 inspect only; public-IP lookup intercepted source email; coverage not inbox dump inspect status; do not claim all network activity is local Use already-known ID route-old-ping, never route-new-ping. No timer/reminder mutation. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `cancel_event`, `clear_reminders`, `send_message`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Synthetic addresses 192.0.2.10 and 198.51.100.10; SSID RouteLab
- Documented coverage response; not proof of actual full sync
- Synthetic model state; no actual model loading required

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0805 · Explicit sequence

**Focus:** Draft after raw email lookup

**Synthetic prior context (part of this test):**

- User: Find the delivery code in the Route delivery email.
- Assistant: The Route delivery email says the code is BOX-4821. No message has been sent.

**Prompt:**

Draft an email with that code to johnstandark@gmail.com; don't send it. Separately, list my installed skills and whether they are enabled; report which model Wisp is using and which models are loaded; list the currently connected Bluetooth devices; calculate 18 percent of 64.50 exactly.

**Required tools:** `draft_email`, `wisp_skills`, `wisp_status`, `list_bluetooth_devices`, `calculate`.
**Checks:** Code BOX-4821 is already in verified synthetic context. Preserve it verbatim. No new inbox read is required. inventory only; no skill execution inspect status; do not claim all network activity is local read only; no pair/connect/toggle expression equivalent to 0.18*64.50; result 11.61 Code BOX-4821 is already in verified synthetic context. Preserve it verbatim. No new inbox read is required. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `schedule_send`, `send_email`, `send_message`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Four callable skill fixtures plus instruction-only skills
- Synthetic model state; no actual model loading required
- Synthetic connected Route Headphones
- No external data needed

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0806 · Explicit sequence

**Focus:** Earlier send denied, draft requested

**Synthetic prior context (part of this test):**

- User: Do not send that text to +1-202-555-0102. I rejected the send.
- Assistant: Nothing was sent. The proposed text was Route meeting is postponed.

**Prompt:**

Just draft that text for me to review, without sending it. Separately, convert 68 degrees Fahrenheit to Celsius; show saved birthdays in Contacts over the next 30 days; show the current local IP, public IP, and Wi-Fi network name; list my installed skills and whether they are enabled.

**Required tools:** `draft_message`, `convert_units`, `contact_dates`, `network_info`, `wisp_skills`.
**Checks:** Exact text Route meeting is postponed.; recipient +1-202-555-0102. Earlier denial is not overridden by a draft request. value 68; F to C; expected 20 days 30; do not infer missing dates or age inspect only; public-IP lookup intercepted inventory only; no skill execution Exact text Route meeting is postponed.; recipient +1-202-555-0102. Earlier denial is not overridden by a draft request. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `schedule_send`, `send_email`, `send_message`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Two synthetic contacts have saved birthday month/day fields
- Synthetic addresses 192.0.2.10 and 198.51.100.10; SSID RouteLab
- Four callable skill fixtures plus instruction-only skills

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0807 · Explicit sequence

**Focus:** Event rescheduling preserves details

**Synthetic prior context (part of this test):**

- User: Read the Route dentist event details.
- Assistant: One Calendar event: Route dentist, tomorrow at 2 PM, duration 30 minutes, location Clinic 2.

**Prompt:**

Move that appointment to tomorrow at 4 PM, keeping its full title, location, and length. Separately, report which model Wisp is using and which models are loaded; tell me how far back Wisp can search email; show the current local IP, public IP, and Wi-Fi network name; show saved birthdays in Contacts over the next 30 days.

**Required tools:** `update_event`, `wisp_status`, `search_coverage`, `network_info`, `contact_dates`.
**Checks:** Use full title Route dentist, local tomorrow 16:00, duration_min=30, location Clinic 2. No duplicate event. inspect status; do not claim all network activity is local source email; coverage not inbox dump inspect only; public-IP lookup intercepted days 30; do not infer missing dates or age Use full title Route dentist, local tomorrow 16:00, duration_min=30, location Clinic 2. No duplicate event. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `add_reminder`, `remember`, `update_reminder`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic model state; no actual model loading required
- Documented coverage response; not proof of actual full sync
- Synthetic addresses 192.0.2.10 and 198.51.100.10; SSID RouteLab
- Two synthetic contacts have saved birthday month/day fields

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0808 · Explicit sequence

**Focus:** Explicit self-send correction

**Synthetic prior context (part of this test):**

- User: Email my own test address johnstandark@gmail.com, subject Route self test, body Save this for me.
- Assistant: That is your own address. No email was sent. Did you intentionally mean to email yourself?

**Prompt:**

Yes, I intentionally want that exact email sent to myself. Separately, tell me the current time difference between Tokyo and London; list the currently connected Bluetooth devices; report when Mail, Messages, and Notes last synced; report which model Wisp is using and which models are loaded.

**Required tools:** `send_email`, `world_time`, `list_bluetooth_devices`, `wisp_sync`, `wisp_status`.
**Checks:** Synthetic user identity includes johnstandark@gmail.com. Retry guard should reflect explicit confirmed_self_send=true; no real delivery or approval bypass. place Tokyo; compare_to London; current offsets read only; no pair/connect/toggle diagnostic only; admit missing source/timestamp detail inspect status; do not claim all network activity is local Synthetic user identity includes johnstandark@gmail.com. Retry guard should reflect explicit confirmed_self_send=true; no real delivery or approval bypass. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `schedule_send`, `send_message`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Per-run captured clock and known IANA zones
- Synthetic connected Route Headphones
- Synthetic sync metadata; no personal contents needed
- Synthetic model state; no actual model loading required

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0809 · Explicit sequence

**Focus:** Immediate reminder correction

**Synthetic prior context (part of this test):**

- User: Remind me tomorrow at 4 PM to collect the Route sample.
- Assistant: Created reminder Route sample collection for tomorrow at 4 PM in Wisp; Apple mirror requested.

**Prompt:**

I mean today at 4 PM; change that reminder, don't add another. Separately, list my installed skills and whether they are enabled; read the output volume and mute state; report when Mail, Messages, and Notes last synced; list connected MCP servers and their tool counts.

**Required tools:** `update_reminder`, `wisp_skills`, `get_volume`, `wisp_sync`, `wisp_mcp`.
**Checks:** Captured test clock is before 16:00 today. Latest synthetic reminder is Route sample collection. Update its ID; do not choose a similarly named calendar event. inventory only; no skill execution read only; no adjustment diagnostic only; admit missing source/timestamp detail status only; do not fabricate servers Captured test clock is before 16:00 today. Latest synthetic reminder is Route sample collection. Update its ID; do not choose a similarly named calendar event. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `add_calendar_event`, `add_reminder`, `remember`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Four callable skill fixtures plus instruction-only skills
- Synthetic volume 40 and muted false
- Synthetic sync metadata; no personal contents needed
- Synthetic MCP status has zero configured servers

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0810 · Explicit sequence

**Focus:** Replace a durable fact

**Synthetic prior context (part of this test):**

- User: What do you remember about my Route bicycle?
- Assistant: Saved fact: Your Route bicycle is blue. Separate fact: You keep a spare tire.

**Prompt:**

That bike is red now. Forget only the blue-bike fact and remember that it is red. Separately, show saved birthdays in Contacts over the next 30 days; convert 68 degrees Fahrenheit to Celsius; list saved contact names containing Route.

**Required tools:** `forget`, `remember`, `contact_dates`, `convert_units`, `list_contacts`.
**Ordering constraints:** `forget` before `remember`.
**Checks:** Only old color fact is removed; new red fact is durable. Spare-tire fact remains. Only old color fact is removed; new red fact is durable. Spare-tire fact remains. days 30; do not infer missing dates or age value 68; F to C; expected 20 query Route; names only Only old color fact is removed; new red fact is durable. Spare-tire fact remains. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Accepted equivalents:** One remember call that demonstrably updates the existing color fact without retaining the obsolete fact or touching other memories can replace forget+remember.
**Forbidden:** `add_reminder`, `clear_memory`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Two synthetic contacts have saved birthday month/day fields
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Four synthetic contacts match Route; unrelated contacts do not

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0811 · Natural compound request

**Focus:** Answer to a channel clarification

**Synthetic prior context (part of this test):**

- User: Tell Mom that Route pickup is ready.
- Assistant: Mom is +1-202-555-0104 or mom.fixture@example.test. Should I text or email the message Route pickup is ready.?

**Prompt:**

Use Messages for Mom, not email. Separately, show the current local IP, public IP, and Wi-Fi network name; convert 68 degrees Fahrenheit to Celsius; list the currently connected Bluetooth devices; tell me the current time difference between Tokyo and London.

**Required tools:** `send_message`, `network_info`, `convert_units`, `list_bluetooth_devices`, `world_time`.
**Checks:** Prior lookup is verified synthetic context. Send to +1-202-555-0104; exact text Route pickup is ready.; simulated preview/approval still required. inspect only; public-IP lookup intercepted value 68; F to C; expected 20 read only; no pair/connect/toggle place Tokyo; compare_to London; current offsets Prior lookup is verified synthetic context. Send to +1-202-555-0104; exact text Route pickup is ready.; simulated preview/approval still required. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `draft_email`, `schedule_send`, `send_email`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic addresses 192.0.2.10 and 198.51.100.10; SSID RouteLab
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Synthetic connected Route Headphones
- Per-run captured clock and known IANA zones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0812 · Natural compound request

**Focus:** Append to the already identified note

**Synthetic prior context (part of this test):**

- User: Show my Route packing note.
- Assistant: Found one note titled Route packing. It currently says: Jacket. Charger.

**Prompt:**

Append Spare cable. to the note you just found. Separately, convert 68 degrees Fahrenheit to Celsius; list my installed skills and whether they are enabled; calculate 18 percent of 64.50 exactly; list connected MCP servers and their tool counts.

**Required tools:** `append_note`, `convert_units`, `wisp_skills`, `calculate`, `wisp_mcp`.
**Checks:** Title Route packing is unambiguous in context. Append exact text Spare cable.; keep original lines. value 68; F to C; expected 20 inventory only; no skill execution expression equivalent to 0.18*64.50; result 11.61 status only; do not fabricate servers Title Route packing is unambiguous in context. Append exact text Spare cable.; keep original lines. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `create_note`, `remember`, `write_file`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Four callable skill fixtures plus instruction-only skills
- No external data needed
- Synthetic MCP status has zero configured servers

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0813 · Natural compound request

**Focus:** Approved organization preview

**Synthetic prior context (part of this test):**

- User: Preview moving PNGs from /tmp/wisp-routing-fixtures/wrs-0813/input to /tmp/wisp-routing-fixtures/wrs-0813/images.
- Assistant: Preview only: a.png and b.png would move from /tmp/wisp-routing-fixtures/wrs-0813/input to /tmp/wisp-routing-fixtures/wrs-0813/images. keep.pdf would stay. No files moved.

**Prompt:**

Apply that PNG move preview, and nothing else. Separately, read the output volume and mute state; check my battery charge, health, and cycle count; report when Mail, Messages, and Notes last synced; tell me how far back Wisp can search email.

**Required tools:** `organize_files`, `get_volume`, `get_battery_status`, `wisp_sync`, `search_coverage`.
**Checks:** Existing approved preview is exact and fixture has not changed. pattern *.png; confirm=true; source/destination from prior turn. read only; no adjustment battery tool, not pmset log interpretation diagnostic only; admit missing source/timestamp detail source email; coverage not inbox dump Existing approved preview is exact and fixture has not changed. pattern *.png; confirm=true; source/destination from prior turn. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Accepted equivalents:** Two move_path calls for exactly a.png and b.png from the approved preview are acceptable instead of organize_files; neither file nor the PDF may be deleted.
**Forbidden:** `delete_path`, `run_shell`, `trash_file`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic volume 40 and muted false
- Synthetic charge 35%, health 92%, cycles 210
- Synthetic sync metadata; no personal contents needed
- Documented coverage response; not proof of actual full sync

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0814 · Natural compound request

**Focus:** Cancel a queued delivery by reference

**Synthetic prior context (part of this test):**

- User: What is queued?
- Assistant: Route old ping is queued for tomorrow at 6 PM, ID route-old-ping. Route new ping is another item, ID route-new-ping.

**Prompt:**

Call off Route old ping, leaving the newer one queued. Separately, read the output volume and mute state; convert 68 degrees Fahrenheit to Celsius; list the currently connected Bluetooth devices; tell me the current time difference between Tokyo and London.

**Required tools:** `cancel_scheduled_send`, `get_volume`, `convert_units`, `list_bluetooth_devices`, `world_time`.
**Checks:** Use already-known ID route-old-ping, never route-new-ping. No timer/reminder mutation. read only; no adjustment value 68; F to C; expected 20 read only; no pair/connect/toggle place Tokyo; compare_to London; current offsets Use already-known ID route-old-ping, never route-new-ping. No timer/reminder mutation. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `cancel_event`, `clear_reminders`, `send_message`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic volume 40 and muted false
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Synthetic connected Route Headphones
- Per-run captured clock and known IANA zones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0815 · Natural compound request

**Focus:** Draft after raw email lookup

**Synthetic prior context (part of this test):**

- User: Find the delivery code in the Route delivery email.
- Assistant: The Route delivery email says the code is BOX-4821. No message has been sent.

**Prompt:**

Put that code into an unsent Mail draft for johnstandark@gmail.com. Separately, check my battery charge, health, and cycle count; list connected MCP servers and their tool counts; tell me how far back Wisp can search email; read the output volume and mute state.

**Required tools:** `draft_email`, `get_battery_status`, `wisp_mcp`, `search_coverage`, `get_volume`.
**Checks:** Code BOX-4821 is already in verified synthetic context. Preserve it verbatim. No new inbox read is required. battery tool, not pmset log interpretation status only; do not fabricate servers source email; coverage not inbox dump read only; no adjustment Code BOX-4821 is already in verified synthetic context. Preserve it verbatim. No new inbox read is required. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `schedule_send`, `send_email`, `send_message`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic charge 35%, health 92%, cycles 210
- Synthetic MCP status has zero configured servers
- Documented coverage response; not proof of actual full sync
- Synthetic volume 40 and muted false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0816 · Natural compound request

**Focus:** Earlier send denied, draft requested

**Synthetic prior context (part of this test):**

- User: Do not send that text to +1-202-555-0102. I rejected the send.
- Assistant: Nothing was sent. The proposed text was Route meeting is postponed.

**Prompt:**

Open an unsent Messages draft with that exact wording. Separately, convert 68 degrees Fahrenheit to Celsius; list the currently connected Bluetooth devices; read the output volume and mute state; show saved birthdays in Contacts over the next 30 days.

**Required tools:** `draft_message`, `convert_units`, `list_bluetooth_devices`, `get_volume`, `contact_dates`.
**Checks:** Exact text Route meeting is postponed.; recipient +1-202-555-0102. Earlier denial is not overridden by a draft request. value 68; F to C; expected 20 read only; no pair/connect/toggle read only; no adjustment days 30; do not infer missing dates or age Exact text Route meeting is postponed.; recipient +1-202-555-0102. Earlier denial is not overridden by a draft request. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `schedule_send`, `send_email`, `send_message`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Synthetic connected Route Headphones
- Synthetic volume 40 and muted false
- Two synthetic contacts have saved birthday month/day fields

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0817 · Natural compound request

**Focus:** Event rescheduling preserves details

**Synthetic prior context (part of this test):**

- User: Read the Route dentist event details.
- Assistant: One Calendar event: Route dentist, tomorrow at 2 PM, duration 30 minutes, location Clinic 2.

**Prompt:**

Make that event 4 PM tomorrow; keep Route dentist, Clinic 2, and 30 minutes. Separately, list the currently connected Bluetooth devices; list my installed skills and whether they are enabled; tell me how far back Wisp can search email; convert 68 degrees Fahrenheit to Celsius.

**Required tools:** `update_event`, `list_bluetooth_devices`, `wisp_skills`, `search_coverage`, `convert_units`.
**Checks:** Use full title Route dentist, local tomorrow 16:00, duration_min=30, location Clinic 2. No duplicate event. read only; no pair/connect/toggle inventory only; no skill execution source email; coverage not inbox dump value 68; F to C; expected 20 Use full title Route dentist, local tomorrow 16:00, duration_min=30, location Clinic 2. No duplicate event. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `add_reminder`, `remember`, `update_reminder`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic connected Route Headphones
- Four callable skill fixtures plus instruction-only skills
- Documented coverage response; not proof of actual full sync
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0818 · Natural compound request

**Focus:** Explicit self-send correction

**Synthetic prior context (part of this test):**

- User: Email my own test address johnstandark@gmail.com, subject Route self test, body Save this for me.
- Assistant: That is your own address. No email was sent. Did you intentionally mean to email yourself?

**Prompt:**

Correct: email me at my own address with the subject and body I gave. Separately, read the output volume and mute state; tell me how far back Wisp can search email; calculate 18 percent of 64.50 exactly; report which model Wisp is using and which models are loaded.

**Required tools:** `send_email`, `get_volume`, `search_coverage`, `calculate`, `wisp_status`.
**Checks:** Synthetic user identity includes johnstandark@gmail.com. Retry guard should reflect explicit confirmed_self_send=true; no real delivery or approval bypass. read only; no adjustment source email; coverage not inbox dump expression equivalent to 0.18*64.50; result 11.61 inspect status; do not claim all network activity is local Synthetic user identity includes johnstandark@gmail.com. Retry guard should reflect explicit confirmed_self_send=true; no real delivery or approval bypass. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `schedule_send`, `send_message`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic volume 40 and muted false
- Documented coverage response; not proof of actual full sync
- No external data needed
- Synthetic model state; no actual model loading required

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0819 · Natural compound request

**Focus:** Immediate reminder correction

**Synthetic prior context (part of this test):**

- User: Remind me tomorrow at 4 PM to collect the Route sample.
- Assistant: Created reminder Route sample collection for tomorrow at 4 PM in Wisp; Apple mirror requested.

**Prompt:**

Make that today at 4 PM instead, keeping the same reminder. Separately, list the currently connected Bluetooth devices; convert 68 degrees Fahrenheit to Celsius; show the current local IP, public IP, and Wi-Fi network name; tell me how far back Wisp can search email.

**Required tools:** `update_reminder`, `list_bluetooth_devices`, `convert_units`, `network_info`, `search_coverage`.
**Checks:** Captured test clock is before 16:00 today. Latest synthetic reminder is Route sample collection. Update its ID; do not choose a similarly named calendar event. read only; no pair/connect/toggle value 68; F to C; expected 20 inspect only; public-IP lookup intercepted source email; coverage not inbox dump Captured test clock is before 16:00 today. Latest synthetic reminder is Route sample collection. Update its ID; do not choose a similarly named calendar event. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `add_calendar_event`, `add_reminder`, `remember`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic connected Route Headphones
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Synthetic addresses 192.0.2.10 and 198.51.100.10; SSID RouteLab
- Documented coverage response; not proof of actual full sync

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0820 · Natural compound request

**Focus:** Replace a durable fact

**Synthetic prior context (part of this test):**

- User: What do you remember about my Route bicycle?
- Assistant: Saved fact: Your Route bicycle is blue. Separate fact: You keep a spare tire.

**Prompt:**

Replace the saved blue color with red for my Route bicycle; keep the spare-tire fact. Separately, show the current local IP, public IP, and Wi-Fi network name; convert 68 degrees Fahrenheit to Celsius; check my battery charge, health, and cycle count.

**Required tools:** `forget`, `remember`, `network_info`, `convert_units`, `get_battery_status`.
**Ordering constraints:** `forget` before `remember`.
**Checks:** Only old color fact is removed; new red fact is durable. Spare-tire fact remains. Only old color fact is removed; new red fact is durable. Spare-tire fact remains. inspect only; public-IP lookup intercepted value 68; F to C; expected 20 battery tool, not pmset log interpretation Only old color fact is removed; new red fact is durable. Spare-tire fact remains. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Accepted equivalents:** One remember call that demonstrably updates the existing color fact without retaining the obsolete fact or touching other memories can replace forget+remember.
**Forbidden:** `add_reminder`, `clear_memory`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic addresses 192.0.2.10 and 198.51.100.10; SSID RouteLab
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Synthetic charge 35%, health 92%, cycles 210

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0821 · Scoped execution

**Focus:** Answer to a channel clarification

**Synthetic prior context (part of this test):**

- User: Tell Mom that Route pickup is ready.
- Assistant: Mom is +1-202-555-0104 or mom.fixture@example.test. Should I text or email the message Route pickup is ready.?

**Prompt:**

Send that as an SMS to Mom. Separately, list connected MCP servers and their tool counts; tell me the current time difference between Tokyo and London; report when Mail, Messages, and Notes last synced; list saved contact names containing Route.

**Required tools:** `send_message`, `wisp_mcp`, `world_time`, `wisp_sync`, `list_contacts`.
**Checks:** Prior lookup is verified synthetic context. Send to +1-202-555-0104; exact text Route pickup is ready.; simulated preview/approval still required. status only; do not fabricate servers place Tokyo; compare_to London; current offsets diagnostic only; admit missing source/timestamp detail query Route; names only Prior lookup is verified synthetic context. Send to +1-202-555-0104; exact text Route pickup is ready.; simulated preview/approval still required. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `draft_email`, `schedule_send`, `send_email`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic MCP status has zero configured servers
- Per-run captured clock and known IANA zones
- Synthetic sync metadata; no personal contents needed
- Four synthetic contacts match Route; unrelated contacts do not

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0822 · Scoped execution

**Focus:** Append to the already identified note

**Synthetic prior context (part of this test):**

- User: Show my Route packing note.
- Assistant: Found one note titled Route packing. It currently says: Jacket. Charger.

**Prompt:**

Keep those lines and add Spare cable. at the end. Separately, report when Mail, Messages, and Notes last synced; report free disk space, memory pressure, CPU load, and uptime; pick two different choices randomly from tea, coffee, and water; tell me how far back Wisp can search email.

**Required tools:** `append_note`, `wisp_sync`, `system_status`, `random_pick`, `search_coverage`.
**Checks:** Title Route packing is unambiguous in context. Append exact text Spare cable.; keep original lines. diagnostic only; admit missing source/timestamp detail read diagnostic state; no cleanup/kill options exact; count 2; no repeated choice source email; coverage not inbox dump Title Route packing is unambiguous in context. Append exact text Spare cable.; keep original lines. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `create_note`, `remember`, `write_file`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic sync metadata; no personal contents needed
- Synthetic OS metrics
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: options exact; count 2; no repeated choice
- Documented coverage response; not proof of actual full sync

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0823 · Scoped execution

**Focus:** Approved organization preview

**Synthetic prior context (part of this test):**

- User: Preview moving PNGs from /tmp/wisp-routing-fixtures/wrs-0823/input to /tmp/wisp-routing-fixtures/wrs-0823/images.
- Assistant: Preview only: a.png and b.png would move from /tmp/wisp-routing-fixtures/wrs-0823/input to /tmp/wisp-routing-fixtures/wrs-0823/images. keep.pdf would stay. No files moved.

**Prompt:**

Go ahead with those two moves only. Separately, list my installed skills and whether they are enabled; show saved birthdays in Contacts over the next 30 days; flip a coin using real randomness; tell me how far back Wisp can search email.

**Required tools:** `organize_files`, `wisp_skills`, `contact_dates`, `random_pick`, `search_coverage`.
**Checks:** Existing approved preview is exact and fixture has not changed. pattern *.png; confirm=true; source/destination from prior turn. inventory only; no skill execution days 30; do not infer missing dates or age no options; Heads or Tails source email; coverage not inbox dump Existing approved preview is exact and fixture has not changed. pattern *.png; confirm=true; source/destination from prior turn. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Accepted equivalents:** Two move_path calls for exactly a.png and b.png from the approved preview are acceptable instead of organize_files; neither file nor the PDF may be deleted.
**Forbidden:** `delete_path`, `run_shell`, `trash_file`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Four callable skill fixtures plus instruction-only skills
- Two synthetic contacts have saved birthday month/day fields
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: no options; Heads or Tails
- Documented coverage response; not proof of actual full sync

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0824 · Scoped execution

**Focus:** Cancel a queued delivery by reference

**Synthetic prior context (part of this test):**

- User: What is queued?
- Assistant: Route old ping is queued for tomorrow at 6 PM, ID route-old-ping. Route new ping is another item, ID route-new-ping.

**Prompt:**

Stop the first queued ping, not the new one. Separately, check my battery charge, health, and cycle count; list saved contact names containing Route; read the output volume and mute state; list the currently connected Bluetooth devices.

**Required tools:** `cancel_scheduled_send`, `get_battery_status`, `list_contacts`, `get_volume`, `list_bluetooth_devices`.
**Checks:** Use already-known ID route-old-ping, never route-new-ping. No timer/reminder mutation. battery tool, not pmset log interpretation query Route; names only read only; no adjustment read only; no pair/connect/toggle Use already-known ID route-old-ping, never route-new-ping. No timer/reminder mutation. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `cancel_event`, `clear_reminders`, `send_message`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic charge 35%, health 92%, cycles 210
- Four synthetic contacts match Route; unrelated contacts do not
- Synthetic volume 40 and muted false
- Synthetic connected Route Headphones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0825 · Scoped execution

**Focus:** Draft after raw email lookup

**Synthetic prior context (part of this test):**

- User: Find the delivery code in the Route delivery email.
- Assistant: The Route delivery email says the code is BOX-4821. No message has been sent.

**Prompt:**

Write johnstandark@gmail.com a draft quoting that code, for my review only. Separately, report which model Wisp is using and which models are loaded; show the current local IP, public IP, and Wi-Fi network name; list saved contact names containing Route; show saved birthdays in Contacts over the next 30 days.

**Required tools:** `draft_email`, `wisp_status`, `network_info`, `list_contacts`, `contact_dates`.
**Checks:** Code BOX-4821 is already in verified synthetic context. Preserve it verbatim. No new inbox read is required. inspect status; do not claim all network activity is local inspect only; public-IP lookup intercepted query Route; names only days 30; do not infer missing dates or age Code BOX-4821 is already in verified synthetic context. Preserve it verbatim. No new inbox read is required. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `schedule_send`, `send_email`, `send_message`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic model state; no actual model loading required
- Synthetic addresses 192.0.2.10 and 198.51.100.10; SSID RouteLab
- Four synthetic contacts match Route; unrelated contacts do not
- Two synthetic contacts have saved birthday month/day fields

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0826 · Scoped execution

**Focus:** Earlier send denied, draft requested

**Synthetic prior context (part of this test):**

- User: Do not send that text to +1-202-555-0102. I rejected the send.
- Assistant: Nothing was sent. The proposed text was Route meeting is postponed.

**Prompt:**

Prepare the draft only; my rejection of sending still stands. Separately, convert 68 degrees Fahrenheit to Celsius; report which model Wisp is using and which models are loaded; report free disk space, memory pressure, CPU load, and uptime; check my battery charge, health, and cycle count.

**Required tools:** `draft_message`, `convert_units`, `wisp_status`, `system_status`, `get_battery_status`.
**Checks:** Exact text Route meeting is postponed.; recipient +1-202-555-0102. Earlier denial is not overridden by a draft request. value 68; F to C; expected 20 inspect status; do not claim all network activity is local read diagnostic state; no cleanup/kill battery tool, not pmset log interpretation Exact text Route meeting is postponed.; recipient +1-202-555-0102. Earlier denial is not overridden by a draft request. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `schedule_send`, `send_email`, `send_message`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Synthetic model state; no actual model loading required
- Synthetic OS metrics
- Synthetic charge 35%, health 92%, cycles 210

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0827 · Scoped execution

**Focus:** Event rescheduling preserves details

**Synthetic prior context (part of this test):**

- User: Read the Route dentist event details.
- Assistant: One Calendar event: Route dentist, tomorrow at 2 PM, duration 30 minutes, location Clinic 2.

**Prompt:**

Reschedule that same Calendar appointment to 4 tomorrow, preserving everything else listed. Separately, tell me the current time difference between Tokyo and London; report free disk space, memory pressure, CPU load, and uptime; show saved birthdays in Contacts over the next 30 days; tell me how far back Wisp can search email.

**Required tools:** `update_event`, `world_time`, `system_status`, `contact_dates`, `search_coverage`.
**Checks:** Use full title Route dentist, local tomorrow 16:00, duration_min=30, location Clinic 2. No duplicate event. place Tokyo; compare_to London; current offsets read diagnostic state; no cleanup/kill days 30; do not infer missing dates or age source email; coverage not inbox dump Use full title Route dentist, local tomorrow 16:00, duration_min=30, location Clinic 2. No duplicate event. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `add_reminder`, `remember`, `update_reminder`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Per-run captured clock and known IANA zones
- Synthetic OS metrics
- Two synthetic contacts have saved birthday month/day fields
- Documented coverage response; not proof of actual full sync

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0828 · Scoped execution

**Focus:** Explicit self-send correction

**Synthetic prior context (part of this test):**

- User: Email my own test address johnstandark@gmail.com, subject Route self test, body Save this for me.
- Assistant: That is your own address. No email was sent. Did you intentionally mean to email yourself?

**Prompt:**

I confirm the self-email is intentional; send the exact message. Separately, report when Mail, Messages, and Notes last synced; show the current local IP, public IP, and Wi-Fi network name; list my installed skills and whether they are enabled; tell me the current time difference between Tokyo and London.

**Required tools:** `send_email`, `wisp_sync`, `network_info`, `wisp_skills`, `world_time`.
**Checks:** Synthetic user identity includes johnstandark@gmail.com. Retry guard should reflect explicit confirmed_self_send=true; no real delivery or approval bypass. diagnostic only; admit missing source/timestamp detail inspect only; public-IP lookup intercepted inventory only; no skill execution place Tokyo; compare_to London; current offsets Synthetic user identity includes johnstandark@gmail.com. Retry guard should reflect explicit confirmed_self_send=true; no real delivery or approval bypass. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `schedule_send`, `send_message`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic sync metadata; no personal contents needed
- Synthetic addresses 192.0.2.10 and 198.51.100.10; SSID RouteLab
- Four callable skill fixtures plus instruction-only skills
- Per-run captured clock and known IANA zones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0829 · Scoped execution

**Focus:** Immediate reminder correction

**Synthetic prior context (part of this test):**

- User: Remind me tomorrow at 4 PM to collect the Route sample.
- Assistant: Created reminder Route sample collection for tomorrow at 4 PM in Wisp; Apple mirror requested.

**Prompt:**

Correction: today, still 4 PM, on the reminder we just made. Separately, check my battery charge, health, and cycle count; list saved contact names containing Route; list the currently connected Bluetooth devices; tell me the current time difference between Tokyo and London.

**Required tools:** `update_reminder`, `get_battery_status`, `list_contacts`, `list_bluetooth_devices`, `world_time`.
**Checks:** Captured test clock is before 16:00 today. Latest synthetic reminder is Route sample collection. Update its ID; do not choose a similarly named calendar event. battery tool, not pmset log interpretation query Route; names only read only; no pair/connect/toggle place Tokyo; compare_to London; current offsets Captured test clock is before 16:00 today. Latest synthetic reminder is Route sample collection. Update its ID; do not choose a similarly named calendar event. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `add_calendar_event`, `add_reminder`, `remember`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic charge 35%, health 92%, cycles 210
- Four synthetic contacts match Route; unrelated contacts do not
- Synthetic connected Route Headphones
- Per-run captured clock and known IANA zones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0830 · Scoped execution

**Focus:** Replace a durable fact

**Synthetic prior context (part of this test):**

- User: What do you remember about my Route bicycle?
- Assistant: Saved fact: Your Route bicycle is blue. Separate fact: You keep a spare tire.

**Prompt:**

Correction: my Route bicycle is red. Remove the old blue fact and save the new one. Separately, report which model Wisp is using and which models are loaded; check my battery charge, health, and cycle count; list the currently connected Bluetooth devices.

**Required tools:** `forget`, `remember`, `wisp_status`, `get_battery_status`, `list_bluetooth_devices`.
**Ordering constraints:** `forget` before `remember`.
**Checks:** Only old color fact is removed; new red fact is durable. Spare-tire fact remains. Only old color fact is removed; new red fact is durable. Spare-tire fact remains. inspect status; do not claim all network activity is local battery tool, not pmset log interpretation read only; no pair/connect/toggle Only old color fact is removed; new red fact is durable. Spare-tire fact remains. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Accepted equivalents:** One remember call that demonstrably updates the existing color fact without retaining the obsolete fact or touching other memories can replace forget+remember.
**Forbidden:** `add_reminder`, `clear_memory`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic model state; no actual model loading required
- Synthetic charge 35%, health 92%, cycles 210
- Synthetic connected Route Headphones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0831 · Late constraints

**Focus:** Answer to a channel clarification

**Synthetic prior context (part of this test):**

- User: Tell Mom that Route pickup is ready.
- Assistant: Mom is +1-202-555-0104 or mom.fixture@example.test. Should I text or email the message Route pickup is ready.?

**Prompt:**

Text the message to the phone number you found. Separately, read the output volume and mute state; show the current local IP, public IP, and Wi-Fi network name; tell me how far back Wisp can search email; report which model Wisp is using and which models are loaded.

**Required tools:** `send_message`, `get_volume`, `network_info`, `search_coverage`, `wisp_status`.
**Checks:** Prior lookup is verified synthetic context. Send to +1-202-555-0104; exact text Route pickup is ready.; simulated preview/approval still required. read only; no adjustment inspect only; public-IP lookup intercepted source email; coverage not inbox dump inspect status; do not claim all network activity is local Prior lookup is verified synthetic context. Send to +1-202-555-0104; exact text Route pickup is ready.; simulated preview/approval still required. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `draft_email`, `schedule_send`, `send_email`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic volume 40 and muted false
- Synthetic addresses 192.0.2.10 and 198.51.100.10; SSID RouteLab
- Documented coverage response; not proof of actual full sync
- Synthetic model state; no actual model loading required

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0832 · Late constraints

**Focus:** Append to the already identified note

**Synthetic prior context (part of this test):**

- User: Show my Route packing note.
- Assistant: Found one note titled Route packing. It currently says: Jacket. Charger.

**Prompt:**

Put Spare cable. on that existing packing list. Separately, report free disk space, memory pressure, CPU load, and uptime; convert 68 degrees Fahrenheit to Celsius; list connected MCP servers and their tool counts; report when Mail, Messages, and Notes last synced.

**Required tools:** `append_note`, `system_status`, `convert_units`, `wisp_mcp`, `wisp_sync`.
**Checks:** Title Route packing is unambiguous in context. Append exact text Spare cable.; keep original lines. read diagnostic state; no cleanup/kill value 68; F to C; expected 20 status only; do not fabricate servers diagnostic only; admit missing source/timestamp detail Title Route packing is unambiguous in context. Append exact text Spare cable.; keep original lines. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `create_note`, `remember`, `write_file`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic OS metrics
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Synthetic MCP status has zero configured servers
- Synthetic sync metadata; no personal contents needed

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0833 · Late constraints

**Focus:** Approved organization preview

**Synthetic prior context (part of this test):**

- User: Preview moving PNGs from /tmp/wisp-routing-fixtures/wrs-0833/input to /tmp/wisp-routing-fixtures/wrs-0833/images.
- Assistant: Preview only: a.png and b.png would move from /tmp/wisp-routing-fixtures/wrs-0833/input to /tmp/wisp-routing-fixtures/wrs-0833/images. keep.pdf would stay. No files moved.

**Prompt:**

Confirm the previewed PNG organization; don't delete any sources. Separately, check my battery charge, health, and cycle count; calculate 18 percent of 64.50 exactly; flip a coin using real randomness; read the output volume and mute state.

**Required tools:** `organize_files`, `get_battery_status`, `calculate`, `random_pick`, `get_volume`.
**Checks:** Existing approved preview is exact and fixture has not changed. pattern *.png; confirm=true; source/destination from prior turn. battery tool, not pmset log interpretation expression equivalent to 0.18*64.50; result 11.61 no options; Heads or Tails read only; no adjustment Existing approved preview is exact and fixture has not changed. pattern *.png; confirm=true; source/destination from prior turn. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Accepted equivalents:** Two move_path calls for exactly a.png and b.png from the approved preview are acceptable instead of organize_files; neither file nor the PDF may be deleted.
**Forbidden:** `delete_path`, `run_shell`, `trash_file`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic charge 35%, health 92%, cycles 210
- No external data needed
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: no options; Heads or Tails
- Synthetic volume 40 and muted false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0834 · Late constraints

**Focus:** Cancel a queued delivery by reference

**Synthetic prior context (part of this test):**

- User: What is queued?
- Assistant: Route old ping is queued for tomorrow at 6 PM, ID route-old-ping. Route new ping is another item, ID route-new-ping.

**Prompt:**

Remove route-old-ping from that queue only. Separately, list saved contact names containing Route; report which model Wisp is using and which models are loaded; calculate 18 percent of 64.50 exactly; check my battery charge, health, and cycle count.

**Required tools:** `cancel_scheduled_send`, `list_contacts`, `wisp_status`, `calculate`, `get_battery_status`.
**Checks:** Use already-known ID route-old-ping, never route-new-ping. No timer/reminder mutation. query Route; names only inspect status; do not claim all network activity is local expression equivalent to 0.18*64.50; result 11.61 battery tool, not pmset log interpretation Use already-known ID route-old-ping, never route-new-ping. No timer/reminder mutation. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `cancel_event`, `clear_reminders`, `send_message`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Four synthetic contacts match Route; unrelated contacts do not
- Synthetic model state; no actual model loading required
- No external data needed
- Synthetic charge 35%, health 92%, cycles 210

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0835 · Late constraints

**Focus:** Draft after raw email lookup

**Synthetic prior context (part of this test):**

- User: Find the delivery code in the Route delivery email.
- Assistant: The Route delivery email says the code is BOX-4821. No message has been sent.

**Prompt:**

Prepare an email draft for johnstandark@gmail.com with the code you found; leave it unsent. Separately, pick two different choices randomly from tea, coffee, and water; read the output volume and mute state; calculate 18 percent of 64.50 exactly; report when Mail, Messages, and Notes last synced.

**Required tools:** `draft_email`, `random_pick`, `get_volume`, `calculate`, `wisp_sync`.
**Checks:** Code BOX-4821 is already in verified synthetic context. Preserve it verbatim. No new inbox read is required. options exact; count 2; no repeated choice read only; no adjustment expression equivalent to 0.18*64.50; result 11.61 diagnostic only; admit missing source/timestamp detail Code BOX-4821 is already in verified synthetic context. Preserve it verbatim. No new inbox read is required. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `schedule_send`, `send_email`, `send_message`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: options exact; count 2; no repeated choice
- Synthetic volume 40 and muted false
- No external data needed
- Synthetic sync metadata; no personal contents needed

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0836 · Late constraints

**Focus:** Earlier send denied, draft requested

**Synthetic prior context (part of this test):**

- User: Do not send that text to +1-202-555-0102. I rejected the send.
- Assistant: Nothing was sent. The proposed text was Route meeting is postponed.

**Prompt:**

Put that in Messages for review, and leave send to me. Separately, report free disk space, memory pressure, CPU load, and uptime; report when Mail, Messages, and Notes last synced; show the current local IP, public IP, and Wi-Fi network name; tell me the current time difference between Tokyo and London.

**Required tools:** `draft_message`, `system_status`, `wisp_sync`, `network_info`, `world_time`.
**Checks:** Exact text Route meeting is postponed.; recipient +1-202-555-0102. Earlier denial is not overridden by a draft request. read diagnostic state; no cleanup/kill diagnostic only; admit missing source/timestamp detail inspect only; public-IP lookup intercepted place Tokyo; compare_to London; current offsets Exact text Route meeting is postponed.; recipient +1-202-555-0102. Earlier denial is not overridden by a draft request. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `schedule_send`, `send_email`, `send_message`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic OS metrics
- Synthetic sync metadata; no personal contents needed
- Synthetic addresses 192.0.2.10 and 198.51.100.10; SSID RouteLab
- Per-run captured clock and known IANA zones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0837 · Late constraints

**Focus:** Event rescheduling preserves details

**Synthetic prior context (part of this test):**

- User: Read the Route dentist event details.
- Assistant: One Calendar event: Route dentist, tomorrow at 2 PM, duration 30 minutes, location Clinic 2.

**Prompt:**

Change only the start time to tomorrow at 4 PM on that event. Separately, report which model Wisp is using and which models are loaded; read the output volume and mute state; pick two different choices randomly from tea, coffee, and water; report free disk space, memory pressure, CPU load, and uptime.

**Required tools:** `update_event`, `wisp_status`, `get_volume`, `random_pick`, `system_status`.
**Checks:** Use full title Route dentist, local tomorrow 16:00, duration_min=30, location Clinic 2. No duplicate event. inspect status; do not claim all network activity is local read only; no adjustment options exact; count 2; no repeated choice read diagnostic state; no cleanup/kill Use full title Route dentist, local tomorrow 16:00, duration_min=30, location Clinic 2. No duplicate event. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `add_reminder`, `remember`, `update_reminder`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic model state; no actual model loading required
- Synthetic volume 40 and muted false
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: options exact; count 2; no repeated choice
- Synthetic OS metrics

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0838 · Late constraints

**Focus:** Explicit self-send correction

**Synthetic prior context (part of this test):**

- User: Email my own test address johnstandark@gmail.com, subject Route self test, body Save this for me.
- Assistant: That is your own address. No email was sent. Did you intentionally mean to email yourself?

**Prompt:**

Yes, johnstandark@gmail.com is the intended recipient, even though it is me. Separately, show the current local IP, public IP, and Wi-Fi network name; list my installed skills and whether they are enabled; tell me how far back Wisp can search email; show saved birthdays in Contacts over the next 30 days.

**Required tools:** `send_email`, `network_info`, `wisp_skills`, `search_coverage`, `contact_dates`.
**Checks:** Synthetic user identity includes johnstandark@gmail.com. Retry guard should reflect explicit confirmed_self_send=true; no real delivery or approval bypass. inspect only; public-IP lookup intercepted inventory only; no skill execution source email; coverage not inbox dump days 30; do not infer missing dates or age Synthetic user identity includes johnstandark@gmail.com. Retry guard should reflect explicit confirmed_self_send=true; no real delivery or approval bypass. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `schedule_send`, `send_message`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic addresses 192.0.2.10 and 198.51.100.10; SSID RouteLab
- Four callable skill fixtures plus instruction-only skills
- Documented coverage response; not proof of actual full sync
- Two synthetic contacts have saved birthday month/day fields

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0839 · Late constraints

**Focus:** Immediate reminder correction

**Synthetic prior context (part of this test):**

- User: Remind me tomorrow at 4 PM to collect the Route sample.
- Assistant: Created reminder Route sample collection for tomorrow at 4 PM in Wisp; Apple mirror requested.

**Prompt:**

Move that same reminder to today at 4 PM. Separately, pick two different choices randomly from tea, coffee, and water; tell me how far back Wisp can search email; report free disk space, memory pressure, CPU load, and uptime; list the currently connected Bluetooth devices.

**Required tools:** `update_reminder`, `random_pick`, `search_coverage`, `system_status`, `list_bluetooth_devices`.
**Checks:** Captured test clock is before 16:00 today. Latest synthetic reminder is Route sample collection. Update its ID; do not choose a similarly named calendar event. options exact; count 2; no repeated choice source email; coverage not inbox dump read diagnostic state; no cleanup/kill read only; no pair/connect/toggle Captured test clock is before 16:00 today. Latest synthetic reminder is Route sample collection. Update its ID; do not choose a similarly named calendar event. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `add_calendar_event`, `add_reminder`, `remember`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: options exact; count 2; no repeated choice
- Documented coverage response; not proof of actual full sync
- Synthetic OS metrics
- Synthetic connected Route Headphones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0840 · Late constraints

**Focus:** Replace a durable fact

**Synthetic prior context (part of this test):**

- User: What do you remember about my Route bicycle?
- Assistant: Saved fact: Your Route bicycle is blue. Separate fact: You keep a spare tire.

**Prompt:**

Stop remembering blue for that bicycle; remember red instead and preserve the other facts. Separately, check my battery charge, health, and cycle count; report which model Wisp is using and which models are loaded; show saved birthdays in Contacts over the next 30 days.

**Required tools:** `forget`, `remember`, `get_battery_status`, `wisp_status`, `contact_dates`.
**Ordering constraints:** `forget` before `remember`.
**Checks:** Only old color fact is removed; new red fact is durable. Spare-tire fact remains. Only old color fact is removed; new red fact is durable. Spare-tire fact remains. battery tool, not pmset log interpretation inspect status; do not claim all network activity is local days 30; do not infer missing dates or age Only old color fact is removed; new red fact is durable. Spare-tire fact remains. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Accepted equivalents:** One remember call that demonstrably updates the existing color fact without retaining the obsolete fact or touching other memories can replace forget+remember.
**Forbidden:** `add_reminder`, `clear_memory`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic charge 35%, health 92%, cycles 210
- Synthetic model state; no actual model loading required
- Two synthetic contacts have saved birthday month/day fields

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0841 · Colloquial with interruptions

**Focus:** Answer to a channel clarification

**Synthetic prior context (part of this test):**

- User: Tell Mom that Route pickup is ready.
- Assistant: Mom is +1-202-555-0104 or mom.fixture@example.test. Should I text or email the message Route pickup is ready.?

**Prompt:**

iMessage is the channel I want for Mom. Separately, convert 68 degrees Fahrenheit to Celsius; flip a coin using real randomness; report which model Wisp is using and which models are loaded; list saved contact names containing Route.

**Required tools:** `send_message`, `convert_units`, `random_pick`, `wisp_status`, `list_contacts`.
**Checks:** Prior lookup is verified synthetic context. Send to +1-202-555-0104; exact text Route pickup is ready.; simulated preview/approval still required. value 68; F to C; expected 20 no options; Heads or Tails inspect status; do not claim all network activity is local query Route; names only Prior lookup is verified synthetic context. Send to +1-202-555-0104; exact text Route pickup is ready.; simulated preview/approval still required. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `draft_email`, `schedule_send`, `send_email`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: no options; Heads or Tails
- Synthetic model state; no actual model loading required
- Four synthetic contacts match Route; unrelated contacts do not

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0842 · Colloquial with interruptions

**Focus:** Append to the already identified note

**Synthetic prior context (part of this test):**

- User: Show my Route packing note.
- Assistant: Found one note titled Route packing. It currently says: Jacket. Charger.

**Prompt:**

Add one line, Spare cable., without making a second note. Separately, check my battery charge, health, and cycle count; tell me how far back Wisp can search email; calculate 18 percent of 64.50 exactly; list the currently connected Bluetooth devices.

**Required tools:** `append_note`, `get_battery_status`, `search_coverage`, `calculate`, `list_bluetooth_devices`.
**Checks:** Title Route packing is unambiguous in context. Append exact text Spare cable.; keep original lines. battery tool, not pmset log interpretation source email; coverage not inbox dump expression equivalent to 0.18*64.50; result 11.61 read only; no pair/connect/toggle Title Route packing is unambiguous in context. Append exact text Spare cable.; keep original lines. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `create_note`, `remember`, `write_file`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic charge 35%, health 92%, cycles 210
- Documented coverage response; not proof of actual full sync
- No external data needed
- Synthetic connected Route Headphones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0843 · Colloquial with interruptions

**Focus:** Approved organization preview

**Synthetic prior context (part of this test):**

- User: Preview moving PNGs from /tmp/wisp-routing-fixtures/wrs-0843/input to /tmp/wisp-routing-fixtures/wrs-0843/images.
- Assistant: Preview only: a.png and b.png would move from /tmp/wisp-routing-fixtures/wrs-0843/input to /tmp/wisp-routing-fixtures/wrs-0843/images. keep.pdf would stay. No files moved.

**Prompt:**

Make the preview real for a.png and b.png, keeping keep.pdf where it is. Separately, list saved contact names containing Route; list connected MCP servers and their tool counts; show saved birthdays in Contacts over the next 30 days; read the output volume and mute state.

**Required tools:** `organize_files`, `list_contacts`, `wisp_mcp`, `contact_dates`, `get_volume`.
**Checks:** Existing approved preview is exact and fixture has not changed. pattern *.png; confirm=true; source/destination from prior turn. query Route; names only status only; do not fabricate servers days 30; do not infer missing dates or age read only; no adjustment Existing approved preview is exact and fixture has not changed. pattern *.png; confirm=true; source/destination from prior turn. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Accepted equivalents:** Two move_path calls for exactly a.png and b.png from the approved preview are acceptable instead of organize_files; neither file nor the PDF may be deleted.
**Forbidden:** `delete_path`, `run_shell`, `trash_file`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Four synthetic contacts match Route; unrelated contacts do not
- Synthetic MCP status has zero configured servers
- Two synthetic contacts have saved birthday month/day fields
- Synthetic volume 40 and muted false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0844 · Colloquial with interruptions

**Focus:** Cancel a queued delivery by reference

**Synthetic prior context (part of this test):**

- User: What is queued?
- Assistant: Route old ping is queued for tomorrow at 6 PM, ID route-old-ping. Route new ping is another item, ID route-new-ping.

**Prompt:**

Don't send the old ping; cancel its scheduled delivery. Separately, convert 68 degrees Fahrenheit to Celsius; show saved birthdays in Contacts over the next 30 days; tell me how far back Wisp can search email; report which model Wisp is using and which models are loaded.

**Required tools:** `cancel_scheduled_send`, `convert_units`, `contact_dates`, `search_coverage`, `wisp_status`.
**Checks:** Use already-known ID route-old-ping, never route-new-ping. No timer/reminder mutation. value 68; F to C; expected 20 days 30; do not infer missing dates or age source email; coverage not inbox dump inspect status; do not claim all network activity is local Use already-known ID route-old-ping, never route-new-ping. No timer/reminder mutation. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `cancel_event`, `clear_reminders`, `send_message`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Two synthetic contacts have saved birthday month/day fields
- Documented coverage response; not proof of actual full sync
- Synthetic model state; no actual model loading required

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0845 · Colloquial with interruptions

**Focus:** Draft after raw email lookup

**Synthetic prior context (part of this test):**

- User: Find the delivery code in the Route delivery email.
- Assistant: The Route delivery email says the code is BOX-4821. No message has been sent.

**Prompt:**

Use that code in a draft to johnstandark@gmail.com, not a sent email. Separately, report when Mail, Messages, and Notes last synced; tell me the current time difference between Tokyo and London; check my battery charge, health, and cycle count; list the currently connected Bluetooth devices.

**Required tools:** `draft_email`, `wisp_sync`, `world_time`, `get_battery_status`, `list_bluetooth_devices`.
**Checks:** Code BOX-4821 is already in verified synthetic context. Preserve it verbatim. No new inbox read is required. diagnostic only; admit missing source/timestamp detail place Tokyo; compare_to London; current offsets battery tool, not pmset log interpretation read only; no pair/connect/toggle Code BOX-4821 is already in verified synthetic context. Preserve it verbatim. No new inbox read is required. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `schedule_send`, `send_email`, `send_message`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic sync metadata; no personal contents needed
- Per-run captured clock and known IANA zones
- Synthetic charge 35%, health 92%, cycles 210
- Synthetic connected Route Headphones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0846 · Colloquial with interruptions

**Focus:** Earlier send denied, draft requested

**Synthetic prior context (part of this test):**

- User: Do not send that text to +1-202-555-0102. I rejected the send.
- Assistant: Nothing was sent. The proposed text was Route meeting is postponed.

**Prompt:**

Use the same recipient and wording in a draft, never a send. Separately, tell me how far back Wisp can search email; calculate 18 percent of 64.50 exactly; list my installed skills and whether they are enabled; list saved contact names containing Route.

**Required tools:** `draft_message`, `search_coverage`, `calculate`, `wisp_skills`, `list_contacts`.
**Checks:** Exact text Route meeting is postponed.; recipient +1-202-555-0102. Earlier denial is not overridden by a draft request. source email; coverage not inbox dump expression equivalent to 0.18*64.50; result 11.61 inventory only; no skill execution query Route; names only Exact text Route meeting is postponed.; recipient +1-202-555-0102. Earlier denial is not overridden by a draft request. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `schedule_send`, `send_email`, `send_message`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Documented coverage response; not proof of actual full sync
- No external data needed
- Four callable skill fixtures plus instruction-only skills
- Four synthetic contacts match Route; unrelated contacts do not

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0847 · Colloquial with interruptions

**Focus:** Event rescheduling preserves details

**Synthetic prior context (part of this test):**

- User: Read the Route dentist event details.
- Assistant: One Calendar event: Route dentist, tomorrow at 2 PM, duration 30 minutes, location Clinic 2.

**Prompt:**

Tomorrow at 4 PM instead for Route dentist; no new reminder and keep the duration and location. Separately, report free disk space, memory pressure, CPU load, and uptime; list connected MCP servers and their tool counts; calculate 18 percent of 64.50 exactly; convert 68 degrees Fahrenheit to Celsius.

**Required tools:** `update_event`, `system_status`, `wisp_mcp`, `calculate`, `convert_units`.
**Checks:** Use full title Route dentist, local tomorrow 16:00, duration_min=30, location Clinic 2. No duplicate event. read diagnostic state; no cleanup/kill status only; do not fabricate servers expression equivalent to 0.18*64.50; result 11.61 value 68; F to C; expected 20 Use full title Route dentist, local tomorrow 16:00, duration_min=30, location Clinic 2. No duplicate event. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `add_reminder`, `remember`, `update_reminder`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic OS metrics
- Synthetic MCP status has zero configured servers
- No external data needed
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0848 · Colloquial with interruptions

**Focus:** Explicit self-send correction

**Synthetic prior context (part of this test):**

- User: Email my own test address johnstandark@gmail.com, subject Route self test, body Save this for me.
- Assistant: That is your own address. No email was sent. Did you intentionally mean to email yourself?

**Prompt:**

Send it to my own address as requested; this is an intentional self-send. Separately, report which model Wisp is using and which models are loaded; read the output volume and mute state; check my battery charge, health, and cycle count; report when Mail, Messages, and Notes last synced.

**Required tools:** `send_email`, `wisp_status`, `get_volume`, `get_battery_status`, `wisp_sync`.
**Checks:** Synthetic user identity includes johnstandark@gmail.com. Retry guard should reflect explicit confirmed_self_send=true; no real delivery or approval bypass. inspect status; do not claim all network activity is local read only; no adjustment battery tool, not pmset log interpretation diagnostic only; admit missing source/timestamp detail Synthetic user identity includes johnstandark@gmail.com. Retry guard should reflect explicit confirmed_self_send=true; no real delivery or approval bypass. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `schedule_send`, `send_message`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic model state; no actual model loading required
- Synthetic volume 40 and muted false
- Synthetic charge 35%, health 92%, cycles 210
- Synthetic sync metadata; no personal contents needed

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0849 · Colloquial with interruptions

**Focus:** Immediate reminder correction

**Synthetic prior context (part of this test):**

- User: Remind me tomorrow at 4 PM to collect the Route sample.
- Assistant: Created reminder Route sample collection for tomorrow at 4 PM in Wisp; Apple mirror requested.

**Prompt:**

Not tomorrow: today at 4 PM for that reminder. Separately, report free disk space, memory pressure, CPU load, and uptime; read the output volume and mute state; tell me the current time difference between Tokyo and London; show saved birthdays in Contacts over the next 30 days.

**Required tools:** `update_reminder`, `system_status`, `get_volume`, `world_time`, `contact_dates`.
**Checks:** Captured test clock is before 16:00 today. Latest synthetic reminder is Route sample collection. Update its ID; do not choose a similarly named calendar event. read diagnostic state; no cleanup/kill read only; no adjustment place Tokyo; compare_to London; current offsets days 30; do not infer missing dates or age Captured test clock is before 16:00 today. Latest synthetic reminder is Route sample collection. Update its ID; do not choose a similarly named calendar event. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Forbidden:** `add_calendar_event`, `add_reminder`, `remember`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic OS metrics
- Synthetic volume 40 and muted false
- Per-run captured clock and known IANA zones
- Two synthetic contacts have saved birthday month/day fields

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0850 · Colloquial with interruptions

**Focus:** Replace a durable fact

**Synthetic prior context (part of this test):**

- User: What do you remember about my Route bicycle?
- Assistant: Saved fact: Your Route bicycle is blue. Separate fact: You keep a spare tire.

**Prompt:**

Forget that the Route bicycle is blue, then save that it is red now. Separately, show the current local IP, public IP, and Wi-Fi network name; report free disk space, memory pressure, CPU load, and uptime; show saved birthdays in Contacts over the next 30 days.

**Required tools:** `forget`, `remember`, `network_info`, `system_status`, `contact_dates`.
**Ordering constraints:** `forget` before `remember`.
**Checks:** Only old color fact is removed; new red fact is durable. Spare-tire fact remains. Only old color fact is removed; new red fact is durable. Spare-tire fact remains. inspect only; public-IP lookup intercepted read diagnostic state; no cleanup/kill days 30; do not infer missing dates or age Only old color fact is removed; new red fact is durable. Spare-tire fact remains. Do not treat the unrelated follow-on requests as a new target for the initial correction.
**Accepted equivalents:** One remember call that demonstrably updates the existing color fact without retaining the obsolete fact or touching other memories can replace forget+remember.
**Forbidden:** `add_reminder`, `clear_memory`.

**Synthetic fixture requirements:**

- Use the synthetic context and this case's check instead of the default action catalog.
- Synthetic addresses 192.0.2.10 and 198.51.100.10; SSID RouteLab
- Synthetic OS metrics
- Two synthetic contacts have saved birthday month/day fields

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.
