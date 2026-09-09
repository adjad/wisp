# Review only — no tests run

## 05. Draft, reply, forward, and send

Preserve the selected channel and distinguish drafts from irreversible sends.

### WRS-0201 · Explicit sequence

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Please do these in this order: look up Mom's saved phone number and email address; then copy "Route pickup confirmed." to the clipboard; then prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample."; then add johnstandark@gmail.com to Mom's existing contact; then forward the Route forwarding test email to johnstandark@gmail.com with the note "Please review this.".

**Required tools:** `lookup_contact`, `clipboard_write`, `draft_message`, `manage_contacts`, `forward_email`.
**Ordering constraints:** `lookup_contact` before `draft_message`; `lookup_contact` before `clipboard_write`; `clipboard_write` before `draft_message`; `draft_message` before `manage_contacts`; `manage_contacts` before `forward_email`.
**Checks:** name Mom; resolve uniquely before any dependent contact use text exact; replace clipboard only Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. action add_email; name Mom; exact value correct Message-ID; recipient and note exact; preserve original content Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Synthetic clipboard; no real clipboard changes
- Fictional test number; intercepted compose operation
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_email; name Mom; exact value
- Message-ID <route-forwarding@example.test>; outgoing Mail transport is intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0202 · Explicit sequence

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Please do these in this order: look up Mom's saved phone number and email address; then prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample."; then read the text of /tmp/wisp-routing-fixtures/wrs-0202/Route proposal.txt; then reply to everyone on the Route delivery email thread with "Thanks, I received the code."; then add phone number +1-202-555-0106 to Mom's existing contact.

**Required tools:** `lookup_contact`, `draft_message`, `read_file`, `reply_to_email`, `manage_contacts`.
**Ordering constraints:** `lookup_contact` before `draft_message`; `draft_message` before `read_file`; `read_file` before `reply_to_email`; `reply_to_email` before `manage_contacts`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. exact path; file content, not guessed summary reply_all true; correct Message-ID; show full reply action add_phone; name Mom; exact value Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Fictional test number; intercepted compose operation
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Original thread fixture exists; simulated confirmation only; variant-specific state must satisfy: reply_all true; correct Message-ID; show full reply
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_phone; name Mom; exact value

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0203 · Explicit sequence

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Please do these in this order: look up Mom's saved phone number and email address; then prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample."; then text Mom at the phone number you just looked up now saying "The Route sample is ready."; then create a new contact named Mom Test New; then copy "Route pickup confirmed." to the clipboard.

**Required tools:** `lookup_contact`, `draft_message`, `send_message`, `manage_contacts`, `clipboard_write`.
**Ordering constraints:** `lookup_contact` before `draft_message`; `lookup_contact` before `send_message`; `draft_message` before `send_message`; `send_message` before `manage_contacts`; `manage_contacts` before `clipboard_write`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. action create; name Mom Test New; do not merge other contacts text exact; replace clipboard only Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Fictional test number; intercepted compose operation
- Fictional reserved phone number; Messages transport and approval are simulated
- Mom Test New does not exist; Contacts write is intercepted
- Synthetic clipboard; no real clipboard changes

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0204 · Explicit sequence

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Please do these in this order: look up Mom's saved phone number and email address; then add phone number +1-202-555-0106 to Mom's existing contact; then copy "Route pickup confirmed." to the clipboard; then prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample."; then text Mom at the phone number you just looked up now saying "The Route sample is ready.".

**Required tools:** `lookup_contact`, `manage_contacts`, `clipboard_write`, `draft_message`, `send_message`.
**Ordering constraints:** `lookup_contact` before `draft_message`; `lookup_contact` before `send_message`; `lookup_contact` before `manage_contacts`; `manage_contacts` before `clipboard_write`; `clipboard_write` before `draft_message`; `draft_message` before `send_message`.
**Checks:** name Mom; resolve uniquely before any dependent contact use action add_phone; name Mom; exact value text exact; replace clipboard only Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_phone; name Mom; exact value
- Synthetic clipboard; no real clipboard changes
- Fictional test number; intercepted compose operation
- Fictional reserved phone number; Messages transport and approval are simulated

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0205 · Explicit sequence

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Please do these in this order: look up Mom's saved phone number and email address; then add phone number +1-202-555-0106 to Mom's existing contact; then copy "Route pickup confirmed." to the clipboard; then read the text of /tmp/wisp-routing-fixtures/wrs-0205/Route proposal.txt; then text Mom at the phone number you just looked up now saying "The Route sample is ready.".

**Required tools:** `lookup_contact`, `manage_contacts`, `clipboard_write`, `read_file`, `send_message`.
**Ordering constraints:** `lookup_contact` before `send_message`; `lookup_contact` before `manage_contacts`; `manage_contacts` before `clipboard_write`; `clipboard_write` before `read_file`; `read_file` before `send_message`.
**Checks:** name Mom; resolve uniquely before any dependent contact use action add_phone; name Mom; exact value text exact; replace clipboard only exact path; file content, not guessed summary Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_phone; name Mom; exact value
- Synthetic clipboard; no real clipboard changes
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Fictional reserved phone number; Messages transport and approval are simulated

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0206 · Explicit sequence

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Please do these in this order: look up Mom's saved phone number and email address; then email johnstandark@gmail.com now with subject Route update and body "The sample is ready."; then copy "Route pickup confirmed." to the clipboard; then text Mom at the phone number you just looked up now saying "The Route sample is ready."; then add johnstandark@gmail.com to Mom's existing contact.

**Required tools:** `lookup_contact`, `send_email`, `clipboard_write`, `send_message`, `manage_contacts`.
**Ordering constraints:** `lookup_contact` before `send_email`; `lookup_contact` before `send_message`; `send_email` before `clipboard_write`; `clipboard_write` before `send_message`; `send_message` before `manage_contacts`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. text exact; replace clipboard only Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. action add_email; name Mom; exact value Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Synthetic clipboard; no real clipboard changes
- Fictional reserved phone number; Messages transport and approval are simulated
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_email; name Mom; exact value

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0207 · Explicit sequence

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Please do these in this order: look up Mom's saved phone number and email address; then email johnstandark@gmail.com now with subject Route update and body "The sample is ready."; then prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample."; then read the text of /tmp/wisp-routing-fixtures/wrs-0207/Route proposal.txt; then copy "Route pickup confirmed." to the clipboard.

**Required tools:** `lookup_contact`, `send_email`, `draft_message`, `read_file`, `clipboard_write`.
**Ordering constraints:** `lookup_contact` before `send_email`; `lookup_contact` before `draft_message`; `send_email` before `draft_message`; `draft_message` before `read_file`; `read_file` before `clipboard_write`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. exact path; file content, not guessed summary text exact; replace clipboard only Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Fictional test number; intercepted compose operation
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic clipboard; no real clipboard changes

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0208 · Explicit sequence

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Please do these in this order: look up Mom's saved phone number and email address; then email johnstandark@gmail.com now with subject Route update and body "The sample is ready."; then forward the Route forwarding test email to johnstandark@gmail.com with the note "Please review this."; then copy "Route pickup confirmed." to the clipboard; then open an unsent email draft to johnstandark@gmail.com with subject Route draft and body "Please review the sample.".

**Required tools:** `lookup_contact`, `send_email`, `forward_email`, `clipboard_write`, `draft_email`.
**Ordering constraints:** `lookup_contact` before `send_email`; `lookup_contact` before `draft_email`; `send_email` before `forward_email`; `forward_email` before `clipboard_write`; `clipboard_write` before `draft_email`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. correct Message-ID; recipient and note exact; preserve original content text exact; replace clipboard only Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Message-ID <route-forwarding@example.test>; outgoing Mail transport is intercepted
- Synthetic clipboard; no real clipboard changes
- Mail draft bridge is intercepted; no compose window opens on the real Mac

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0209 · Explicit sequence

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Please do these in this order: look up Mom's saved phone number and email address; then email johnstandark@gmail.com now with subject Route update and body "The sample is ready."; then text Mom at the phone number you just looked up now saying "The Route sample is ready."; then copy "Route pickup confirmed." to the clipboard; then read the text of /tmp/wisp-routing-fixtures/wrs-0209/Route proposal.txt.

**Required tools:** `lookup_contact`, `send_email`, `send_message`, `clipboard_write`, `read_file`.
**Ordering constraints:** `lookup_contact` before `send_email`; `lookup_contact` before `send_message`; `send_email` before `send_message`; `send_message` before `clipboard_write`; `clipboard_write` before `read_file`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. text exact; replace clipboard only exact path; file content, not guessed summary Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Fictional reserved phone number; Messages transport and approval are simulated
- Synthetic clipboard; no real clipboard changes
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0210 · Explicit sequence

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Please do these in this order: look up Mom's saved phone number and email address; then text Mom at the phone number you just looked up now saying "The Route sample is ready."; then add phone number +1-202-555-0106 to Mom's existing contact; then open an unsent email draft to johnstandark@gmail.com with subject Route draft and body "Please review the sample."; then prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample.".

**Required tools:** `lookup_contact`, `send_message`, `manage_contacts`, `draft_email`, `draft_message`.
**Ordering constraints:** `lookup_contact` before `send_message`; `lookup_contact` before `draft_email`; `lookup_contact` before `draft_message`; `send_message` before `manage_contacts`; `manage_contacts` before `draft_email`; `draft_email` before `draft_message`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. action add_phone; name Mom; exact value Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Fictional reserved phone number; Messages transport and approval are simulated
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_phone; name Mom; exact value
- Mail draft bridge is intercepted; no compose window opens on the real Mac
- Fictional test number; intercepted compose operation

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0211 · Natural compound request

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

I have a few things to finish. Look up Mom's saved phone number and email address. Copy "Route pickup confirmed." to the clipboard. Open an unsent email draft to johnstandark@gmail.com with subject Route draft and body "Please review the sample.". Email johnstandark@gmail.com now with subject Route update and body "The sample is ready.". Read the text of /tmp/wisp-routing-fixtures/wrs-0211/Route proposal.txt. Keep the results separate so I can tell what came from where.

**Required tools:** `lookup_contact`, `clipboard_write`, `draft_email`, `send_email`, `read_file`.
**Ordering constraints:** `lookup_contact` before `draft_email`; `lookup_contact` before `send_email`.
**Checks:** name Mom; resolve uniquely before any dependent contact use text exact; replace clipboard only Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. exact path; file content, not guessed summary Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Synthetic clipboard; no real clipboard changes
- Mail draft bridge is intercepted; no compose window opens on the real Mac
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0212 · Natural compound request

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

I have a few things to finish. Look up Mom's saved phone number and email address. Prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample.". Read the text of /tmp/wisp-routing-fixtures/wrs-0212/Route proposal.txt. Add johnstandark@gmail.com to Mom's existing contact. Text Mom at the phone number you just looked up now saying "The Route sample is ready.". Keep the results separate so I can tell what came from where.

**Required tools:** `lookup_contact`, `draft_message`, `read_file`, `manage_contacts`, `send_message`.
**Ordering constraints:** `lookup_contact` before `draft_message`; `lookup_contact` before `send_message`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. exact path; file content, not guessed summary action add_email; name Mom; exact value Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Fictional test number; intercepted compose operation
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_email; name Mom; exact value
- Fictional reserved phone number; Messages transport and approval are simulated

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0213 · Natural compound request

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

I have a few things to finish. Look up Mom's saved phone number and email address. Forward the Route forwarding test email to johnstandark@gmail.com with the note "Please review this.". Prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample.". Copy "Route pickup confirmed." to the clipboard. Reply to everyone on the Route delivery email thread with "Thanks, I received the code.". Keep the results separate so I can tell what came from where.

**Required tools:** `lookup_contact`, `forward_email`, `draft_message`, `clipboard_write`, `reply_to_email`.
**Ordering constraints:** `lookup_contact` before `draft_message`.
**Checks:** name Mom; resolve uniquely before any dependent contact use correct Message-ID; recipient and note exact; preserve original content Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. text exact; replace clipboard only reply_all true; correct Message-ID; show full reply Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Message-ID <route-forwarding@example.test>; outgoing Mail transport is intercepted
- Fictional test number; intercepted compose operation
- Synthetic clipboard; no real clipboard changes
- Original thread fixture exists; simulated confirmation only; variant-specific state must satisfy: reply_all true; correct Message-ID; show full reply

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0214 · Natural compound request

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

I have a few things to finish. Look up Mom's saved phone number and email address. Read the text of /tmp/wisp-routing-fixtures/wrs-0214/Route proposal.txt. Copy "Route pickup confirmed." to the clipboard. Text Mom at the phone number you just looked up now saying "The Route sample is ready.". Add phone number +1-202-555-0106 to Mom's existing contact. Keep the results separate so I can tell what came from where.

**Required tools:** `lookup_contact`, `read_file`, `clipboard_write`, `send_message`, `manage_contacts`.
**Ordering constraints:** `lookup_contact` before `send_message`.
**Checks:** name Mom; resolve uniquely before any dependent contact use exact path; file content, not guessed summary text exact; replace clipboard only Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. action add_phone; name Mom; exact value Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic clipboard; no real clipboard changes
- Fictional reserved phone number; Messages transport and approval are simulated
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_phone; name Mom; exact value

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0215 · Natural compound request

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

I have a few things to finish. Look up Mom's saved phone number and email address. Read the text of /tmp/wisp-routing-fixtures/wrs-0215/Route proposal.txt. Copy "Route pickup confirmed." to the clipboard. Text Mom at the phone number you just looked up now saying "The Route sample is ready.". Email johnstandark@gmail.com now with subject Route update and body "The sample is ready.". Keep the results separate so I can tell what came from where.

**Required tools:** `lookup_contact`, `read_file`, `clipboard_write`, `send_message`, `send_email`.
**Ordering constraints:** `lookup_contact` before `send_message`; `lookup_contact` before `send_email`.
**Checks:** name Mom; resolve uniquely before any dependent contact use exact path; file content, not guessed summary text exact; replace clipboard only Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic clipboard; no real clipboard changes
- Fictional reserved phone number; Messages transport and approval are simulated
- Reserved test address only; outbound transport is intercepted and approval is simulated

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0216 · Natural compound request

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

I have a few things to finish. Look up Mom's saved phone number and email address. Read the text of /tmp/wisp-routing-fixtures/wrs-0216/Route proposal.txt. Email johnstandark@gmail.com now with subject Route update and body "The sample is ready.". Copy "Route pickup confirmed." to the clipboard. Add phone number +1-202-555-0106 to Mom's existing contact. Keep the results separate so I can tell what came from where.

**Required tools:** `lookup_contact`, `read_file`, `send_email`, `clipboard_write`, `manage_contacts`.
**Ordering constraints:** `lookup_contact` before `send_email`.
**Checks:** name Mom; resolve uniquely before any dependent contact use exact path; file content, not guessed summary Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. text exact; replace clipboard only action add_phone; name Mom; exact value Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Synthetic clipboard; no real clipboard changes
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_phone; name Mom; exact value

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0217 · Natural compound request

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

I have a few things to finish. Look up Mom's saved phone number and email address. Read the text of /tmp/wisp-routing-fixtures/wrs-0217/Route proposal.txt. Email johnstandark@gmail.com now with subject Route update and body "The sample is ready.". Add johnstandark@gmail.com to Mom's existing contact. Copy "Route pickup confirmed." to the clipboard. Keep the results separate so I can tell what came from where.

**Required tools:** `lookup_contact`, `read_file`, `send_email`, `manage_contacts`, `clipboard_write`.
**Ordering constraints:** `lookup_contact` before `send_email`.
**Checks:** name Mom; resolve uniquely before any dependent contact use exact path; file content, not guessed summary Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. action add_email; name Mom; exact value text exact; replace clipboard only Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_email; name Mom; exact value
- Synthetic clipboard; no real clipboard changes

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0218 · Natural compound request

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

I have a few things to finish. Look up Mom's saved phone number and email address. Email johnstandark@gmail.com now with subject Route update and body "The sample is ready.". Prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample.". Copy "Route pickup confirmed." to the clipboard. Add johnstandark@gmail.com to Mom's existing contact. Keep the results separate so I can tell what came from where.

**Required tools:** `lookup_contact`, `send_email`, `draft_message`, `clipboard_write`, `manage_contacts`.
**Ordering constraints:** `lookup_contact` before `send_email`; `lookup_contact` before `draft_message`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. text exact; replace clipboard only action add_email; name Mom; exact value Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Fictional test number; intercepted compose operation
- Synthetic clipboard; no real clipboard changes
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_email; name Mom; exact value

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0219 · Natural compound request

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

I have a few things to finish. Look up Mom's saved phone number and email address. Email johnstandark@gmail.com now with subject Route update and body "The sample is ready.". Read the text of /tmp/wisp-routing-fixtures/wrs-0219/Route proposal.txt. Add phone number +1-202-555-0106 to Mom's existing contact. Prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample.". Keep the results separate so I can tell what came from where.

**Required tools:** `lookup_contact`, `send_email`, `read_file`, `manage_contacts`, `draft_message`.
**Ordering constraints:** `lookup_contact` before `send_email`; `lookup_contact` before `draft_message`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. exact path; file content, not guessed summary action add_phone; name Mom; exact value Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_phone; name Mom; exact value
- Fictional test number; intercepted compose operation

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0220 · Natural compound request

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

I have a few things to finish. Look up Mom's saved phone number and email address. Email johnstandark@gmail.com now with subject Route update and body "The sample is ready.". Text Mom at the phone number you just looked up now saying "The Route sample is ready.". Prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample.". Copy "Route pickup confirmed." to the clipboard. Keep the results separate so I can tell what came from where.

**Required tools:** `lookup_contact`, `send_email`, `send_message`, `draft_message`, `clipboard_write`.
**Ordering constraints:** `lookup_contact` before `send_email`; `lookup_contact` before `send_message`; `lookup_contact` before `draft_message`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. text exact; replace clipboard only Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Fictional reserved phone number; Messages transport and approval are simulated
- Fictional test number; intercepted compose operation
- Synthetic clipboard; no real clipboard changes

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0221 · Scoped execution

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

For these tasks, use only the named sources and targets: look up Mom's saved phone number and email address; then copy "Route pickup confirmed." to the clipboard; then prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample."; then read the text of /tmp/wisp-routing-fixtures/wrs-0221/Route proposal.txt; then text Mom at the phone number you just looked up now saying "The Route sample is ready.". Leave everything else unchanged.

**Required tools:** `lookup_contact`, `clipboard_write`, `draft_message`, `read_file`, `send_message`.
**Ordering constraints:** `lookup_contact` before `draft_message`; `lookup_contact` before `send_message`; `lookup_contact` before `clipboard_write`; `clipboard_write` before `draft_message`; `draft_message` before `read_file`; `read_file` before `send_message`.
**Checks:** name Mom; resolve uniquely before any dependent contact use text exact; replace clipboard only Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. exact path; file content, not guessed summary Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Synthetic clipboard; no real clipboard changes
- Fictional test number; intercepted compose operation
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Fictional reserved phone number; Messages transport and approval are simulated

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0222 · Scoped execution

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

For these tasks, use only the named sources and targets: look up Mom's saved phone number and email address; then copy "Route pickup confirmed." to the clipboard; then forward the Route forwarding test email to johnstandark@gmail.com with the note "Please review this."; then add phone number +1-202-555-0106 to Mom's existing contact; then text Mom at the phone number you just looked up now saying "The Route sample is ready.". Leave everything else unchanged.

**Required tools:** `lookup_contact`, `clipboard_write`, `forward_email`, `manage_contacts`, `send_message`.
**Ordering constraints:** `lookup_contact` before `send_message`; `lookup_contact` before `clipboard_write`; `clipboard_write` before `forward_email`; `forward_email` before `manage_contacts`; `manage_contacts` before `send_message`.
**Checks:** name Mom; resolve uniquely before any dependent contact use text exact; replace clipboard only correct Message-ID; recipient and note exact; preserve original content action add_phone; name Mom; exact value Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Synthetic clipboard; no real clipboard changes
- Message-ID <route-forwarding@example.test>; outgoing Mail transport is intercepted
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_phone; name Mom; exact value
- Fictional reserved phone number; Messages transport and approval are simulated

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0223 · Scoped execution

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

For these tasks, use only the named sources and targets: look up Mom's saved phone number and email address; then copy "Route pickup confirmed." to the clipboard; then read the text of /tmp/wisp-routing-fixtures/wrs-0223/Route proposal.txt; then email johnstandark@gmail.com now with subject Route update and body "The sample is ready."; then prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample.". Leave everything else unchanged.

**Required tools:** `lookup_contact`, `clipboard_write`, `read_file`, `send_email`, `draft_message`.
**Ordering constraints:** `lookup_contact` before `send_email`; `lookup_contact` before `draft_message`; `lookup_contact` before `clipboard_write`; `clipboard_write` before `read_file`; `read_file` before `send_email`; `send_email` before `draft_message`.
**Checks:** name Mom; resolve uniquely before any dependent contact use text exact; replace clipboard only exact path; file content, not guessed summary Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Synthetic clipboard; no real clipboard changes
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Fictional test number; intercepted compose operation

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0224 · Scoped execution

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

For these tasks, use only the named sources and targets: look up Mom's saved phone number and email address; then open an unsent email draft to johnstandark@gmail.com with subject Route draft and body "Please review the sample."; then prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample."; then text Mom at the phone number you just looked up now saying "The Route sample is ready."; then add johnstandark@gmail.com to Mom's existing contact. Leave everything else unchanged.

**Required tools:** `lookup_contact`, `draft_email`, `draft_message`, `send_message`, `manage_contacts`.
**Ordering constraints:** `lookup_contact` before `draft_email`; `lookup_contact` before `draft_message`; `lookup_contact` before `send_message`; `draft_email` before `draft_message`; `draft_message` before `send_message`; `send_message` before `manage_contacts`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. action add_email; name Mom; exact value Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Mail draft bridge is intercepted; no compose window opens on the real Mac
- Fictional test number; intercepted compose operation
- Fictional reserved phone number; Messages transport and approval are simulated
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_email; name Mom; exact value

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0225 · Scoped execution

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

For these tasks, use only the named sources and targets: look up Mom's saved phone number and email address; then prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample."; then add phone number +1-202-555-0106 to Mom's existing contact; then copy "Route pickup confirmed." to the clipboard; then email johnstandark@gmail.com now with subject Route update and body "The sample is ready.". Leave everything else unchanged.

**Required tools:** `lookup_contact`, `draft_message`, `manage_contacts`, `clipboard_write`, `send_email`.
**Ordering constraints:** `lookup_contact` before `draft_message`; `lookup_contact` before `send_email`; `draft_message` before `manage_contacts`; `manage_contacts` before `clipboard_write`; `clipboard_write` before `send_email`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. action add_phone; name Mom; exact value text exact; replace clipboard only Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Fictional test number; intercepted compose operation
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_phone; name Mom; exact value
- Synthetic clipboard; no real clipboard changes
- Reserved test address only; outbound transport is intercepted and approval is simulated

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0226 · Scoped execution

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

For these tasks, use only the named sources and targets: look up Mom's saved phone number and email address; then prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample."; then reply to everyone on the Route delivery email thread with "Thanks, I received the code."; then open an unsent email draft to johnstandark@gmail.com with subject Route draft and body "Please review the sample."; then text Mom at the phone number you just looked up now saying "The Route sample is ready.". Leave everything else unchanged.

**Required tools:** `lookup_contact`, `draft_message`, `reply_to_email`, `draft_email`, `send_message`.
**Ordering constraints:** `lookup_contact` before `draft_message`; `lookup_contact` before `draft_email`; `lookup_contact` before `send_message`; `draft_message` before `reply_to_email`; `reply_to_email` before `draft_email`; `draft_email` before `send_message`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. reply_all true; correct Message-ID; show full reply Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Fictional test number; intercepted compose operation
- Original thread fixture exists; simulated confirmation only; variant-specific state must satisfy: reply_all true; correct Message-ID; show full reply
- Mail draft bridge is intercepted; no compose window opens on the real Mac
- Fictional reserved phone number; Messages transport and approval are simulated

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0227 · Scoped execution

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

For these tasks, use only the named sources and targets: look up Mom's saved phone number and email address; then prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample."; then text Mom at the phone number you just looked up now saying "The Route sample is ready."; then email johnstandark@gmail.com now with subject Route update and body "The sample is ready."; then read the text of /tmp/wisp-routing-fixtures/wrs-0227/Route proposal.txt. Leave everything else unchanged.

**Required tools:** `lookup_contact`, `draft_message`, `send_message`, `send_email`, `read_file`.
**Ordering constraints:** `lookup_contact` before `draft_message`; `lookup_contact` before `send_message`; `lookup_contact` before `send_email`; `draft_message` before `send_message`; `send_message` before `send_email`; `send_email` before `read_file`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. exact path; file content, not guessed summary Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Fictional test number; intercepted compose operation
- Fictional reserved phone number; Messages transport and approval are simulated
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0228 · Scoped execution

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

For these tasks, use only the named sources and targets: look up Mom's saved phone number and email address; then read the text of /tmp/wisp-routing-fixtures/wrs-0228/Route proposal.txt; then text Mom at the phone number you just looked up now saying "The Route sample is ready."; then prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample."; then add johnstandark@gmail.com to Mom's existing contact. Leave everything else unchanged.

**Required tools:** `lookup_contact`, `read_file`, `send_message`, `draft_message`, `manage_contacts`.
**Ordering constraints:** `lookup_contact` before `send_message`; `lookup_contact` before `draft_message`; `lookup_contact` before `read_file`; `read_file` before `send_message`; `send_message` before `draft_message`; `draft_message` before `manage_contacts`.
**Checks:** name Mom; resolve uniquely before any dependent contact use exact path; file content, not guessed summary Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. action add_email; name Mom; exact value Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Fictional reserved phone number; Messages transport and approval are simulated
- Fictional test number; intercepted compose operation
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_email; name Mom; exact value

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0229 · Scoped execution

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

For these tasks, use only the named sources and targets: look up Mom's saved phone number and email address; then text Mom at the phone number you just looked up now saying "The Route sample is ready."; then copy "Route pickup confirmed." to the clipboard; then email johnstandark@gmail.com now with subject Route update and body "The sample is ready."; then add johnstandark@gmail.com to Mom's existing contact. Leave everything else unchanged.

**Required tools:** `lookup_contact`, `send_message`, `clipboard_write`, `send_email`, `manage_contacts`.
**Ordering constraints:** `lookup_contact` before `send_message`; `lookup_contact` before `send_email`; `send_message` before `clipboard_write`; `clipboard_write` before `send_email`; `send_email` before `manage_contacts`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. text exact; replace clipboard only Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. action add_email; name Mom; exact value Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Fictional reserved phone number; Messages transport and approval are simulated
- Synthetic clipboard; no real clipboard changes
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_email; name Mom; exact value

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0230 · Scoped execution

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

For these tasks, use only the named sources and targets: look up Mom's saved phone number and email address; then text Mom at the phone number you just looked up now saying "The Route sample is ready."; then read the text of /tmp/wisp-routing-fixtures/wrs-0230/Route proposal.txt; then email johnstandark@gmail.com now with subject Route update and body "The sample is ready."; then prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample.". Leave everything else unchanged.

**Required tools:** `lookup_contact`, `send_message`, `read_file`, `send_email`, `draft_message`.
**Ordering constraints:** `lookup_contact` before `send_message`; `lookup_contact` before `send_email`; `lookup_contact` before `draft_message`; `send_message` before `read_file`; `read_file` before `send_email`; `send_email` before `draft_message`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. exact path; file content, not guessed summary Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Fictional reserved phone number; Messages transport and approval are simulated
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Fictional test number; intercepted compose operation

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0231 · Late constraints

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Look up Mom's saved phone number and email address. Prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample.". Add phone number +1-202-555-0106 to Mom's existing contact. Reply to everyone on the Route delivery email thread with "Thanks, I received the code.". Open an unsent email draft to johnstandark@gmail.com with subject Route draft and body "Please review the sample.". One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `lookup_contact`, `draft_message`, `manage_contacts`, `reply_to_email`, `draft_email`.
**Ordering constraints:** `lookup_contact` before `draft_message`; `lookup_contact` before `draft_email`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. action add_phone; name Mom; exact value reply_all true; correct Message-ID; show full reply Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Fictional test number; intercepted compose operation
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_phone; name Mom; exact value
- Original thread fixture exists; simulated confirmation only; variant-specific state must satisfy: reply_all true; correct Message-ID; show full reply
- Mail draft bridge is intercepted; no compose window opens on the real Mac

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0232 · Late constraints

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Look up Mom's saved phone number and email address. Add johnstandark@gmail.com to Mom's existing contact. Prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample.". Email johnstandark@gmail.com now with subject Route update and body "The sample is ready.". Text Mom at the phone number you just looked up now saying "The Route sample is ready.". One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `lookup_contact`, `manage_contacts`, `draft_message`, `send_email`, `send_message`.
**Ordering constraints:** `lookup_contact` before `draft_message`; `lookup_contact` before `send_email`; `lookup_contact` before `send_message`.
**Checks:** name Mom; resolve uniquely before any dependent contact use action add_email; name Mom; exact value Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_email; name Mom; exact value
- Fictional test number; intercepted compose operation
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Fictional reserved phone number; Messages transport and approval are simulated

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0233 · Late constraints

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Look up Mom's saved phone number and email address. Add phone number +1-202-555-0106 to Mom's existing contact. Text Mom at the phone number you just looked up now saying "The Route sample is ready.". Email johnstandark@gmail.com now with subject Route update and body "The sample is ready.". Read the text of /tmp/wisp-routing-fixtures/wrs-0233/Route proposal.txt. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `lookup_contact`, `manage_contacts`, `send_message`, `send_email`, `read_file`.
**Ordering constraints:** `lookup_contact` before `send_message`; `lookup_contact` before `send_email`.
**Checks:** name Mom; resolve uniquely before any dependent contact use action add_phone; name Mom; exact value Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. exact path; file content, not guessed summary Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_phone; name Mom; exact value
- Fictional reserved phone number; Messages transport and approval are simulated
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0234 · Late constraints

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Look up Mom's saved phone number and email address. Read the text of /tmp/wisp-routing-fixtures/wrs-0234/Route proposal.txt. Open an unsent email draft to johnstandark@gmail.com with subject Route draft and body "Please review the sample.". Forward the Route forwarding test email to johnstandark@gmail.com with the note "Please review this.". Copy "Route pickup confirmed." to the clipboard. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `lookup_contact`, `read_file`, `draft_email`, `forward_email`, `clipboard_write`.
**Ordering constraints:** `lookup_contact` before `draft_email`.
**Checks:** name Mom; resolve uniquely before any dependent contact use exact path; file content, not guessed summary Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. correct Message-ID; recipient and note exact; preserve original content text exact; replace clipboard only Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Mail draft bridge is intercepted; no compose window opens on the real Mac
- Message-ID <route-forwarding@example.test>; outgoing Mail transport is intercepted
- Synthetic clipboard; no real clipboard changes

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0235 · Late constraints

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Look up Mom's saved phone number and email address. Read the text of /tmp/wisp-routing-fixtures/wrs-0235/Route proposal.txt. Text Mom at the phone number you just looked up now saying "The Route sample is ready.". Prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample.". Copy "Route pickup confirmed." to the clipboard. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `lookup_contact`, `read_file`, `send_message`, `draft_message`, `clipboard_write`.
**Ordering constraints:** `lookup_contact` before `send_message`; `lookup_contact` before `draft_message`.
**Checks:** name Mom; resolve uniquely before any dependent contact use exact path; file content, not guessed summary Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. text exact; replace clipboard only Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Fictional reserved phone number; Messages transport and approval are simulated
- Fictional test number; intercepted compose operation
- Synthetic clipboard; no real clipboard changes

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0236 · Late constraints

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Look up Mom's saved phone number and email address. Reply to everyone on the Route delivery email thread with "Thanks, I received the code.". Prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample.". Email johnstandark@gmail.com now with subject Route update and body "The sample is ready.". Add phone number +1-202-555-0106 to Mom's existing contact. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `lookup_contact`, `reply_to_email`, `draft_message`, `send_email`, `manage_contacts`.
**Ordering constraints:** `lookup_contact` before `draft_message`; `lookup_contact` before `send_email`.
**Checks:** name Mom; resolve uniquely before any dependent contact use reply_all true; correct Message-ID; show full reply Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. action add_phone; name Mom; exact value Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Original thread fixture exists; simulated confirmation only; variant-specific state must satisfy: reply_all true; correct Message-ID; show full reply
- Fictional test number; intercepted compose operation
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_phone; name Mom; exact value

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0237 · Late constraints

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Look up Mom's saved phone number and email address. Reply to everyone on the Route delivery email thread with "Thanks, I received the code.". Email johnstandark@gmail.com now with subject Route update and body "The sample is ready.". Forward the Route forwarding test email to johnstandark@gmail.com with the note "Please review this.". Add phone number +1-202-555-0106 to Mom's existing contact. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `lookup_contact`, `reply_to_email`, `send_email`, `forward_email`, `manage_contacts`.
**Ordering constraints:** `lookup_contact` before `send_email`.
**Checks:** name Mom; resolve uniquely before any dependent contact use reply_all true; correct Message-ID; show full reply Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. correct Message-ID; recipient and note exact; preserve original content action add_phone; name Mom; exact value Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Original thread fixture exists; simulated confirmation only; variant-specific state must satisfy: reply_all true; correct Message-ID; show full reply
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Message-ID <route-forwarding@example.test>; outgoing Mail transport is intercepted
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_phone; name Mom; exact value

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0238 · Late constraints

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Look up Mom's saved phone number and email address. Email johnstandark@gmail.com now with subject Route update and body "The sample is ready.". Create a new contact named Mom Test New. Prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample.". Text Mom at the phone number you just looked up now saying "The Route sample is ready.". One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `lookup_contact`, `send_email`, `manage_contacts`, `draft_message`, `send_message`.
**Ordering constraints:** `lookup_contact` before `send_email`; `lookup_contact` before `draft_message`; `lookup_contact` before `send_message`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. action create; name Mom Test New; do not merge other contacts Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Mom Test New does not exist; Contacts write is intercepted
- Fictional test number; intercepted compose operation
- Fictional reserved phone number; Messages transport and approval are simulated

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0239 · Late constraints

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Look up Mom's saved phone number and email address. Email johnstandark@gmail.com now with subject Route update and body "The sample is ready.". Add phone number +1-202-555-0106 to Mom's existing contact. Read the text of /tmp/wisp-routing-fixtures/wrs-0239/Route proposal.txt. Prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample.". One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `lookup_contact`, `send_email`, `manage_contacts`, `read_file`, `draft_message`.
**Ordering constraints:** `lookup_contact` before `send_email`; `lookup_contact` before `draft_message`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. action add_phone; name Mom; exact value exact path; file content, not guessed summary Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_phone; name Mom; exact value
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Fictional test number; intercepted compose operation

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0240 · Late constraints

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Look up Mom's saved phone number and email address. Text Mom at the phone number you just looked up now saying "The Route sample is ready.". Forward the Route forwarding test email to johnstandark@gmail.com with the note "Please review this.". Reply to everyone on the Route delivery email thread with "Thanks, I received the code.". Email johnstandark@gmail.com now with subject Route update and body "The sample is ready.". One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `lookup_contact`, `send_message`, `forward_email`, `reply_to_email`, `send_email`.
**Ordering constraints:** `lookup_contact` before `send_message`; `lookup_contact` before `send_email`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. correct Message-ID; recipient and note exact; preserve original content reply_all true; correct Message-ID; show full reply Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Fictional reserved phone number; Messages transport and approval are simulated
- Message-ID <route-forwarding@example.test>; outgoing Mail transport is intercepted
- Original thread fixture exists; simulated confirmation only; variant-specific state must satisfy: reply_all true; correct Message-ID; show full reply
- Reserved test address only; outbound transport is intercepted and approval is simulated

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0241 · Colloquial with interruptions

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Could you look up Mom's saved phone number and email address; then copy "Route pickup confirmed." to the clipboard; then forward the Route forwarding test email to johnstandark@gmail.com with the note "Please review this."; then open an unsent email draft to johnstandark@gmail.com with subject Route draft and body "Please review the sample."; then read the text of /tmp/wisp-routing-fixtures/wrs-0241/Route proposal.txt? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `lookup_contact`, `clipboard_write`, `forward_email`, `draft_email`, `read_file`.
**Ordering constraints:** `lookup_contact` before `draft_email`; `lookup_contact` before `clipboard_write`; `clipboard_write` before `forward_email`; `forward_email` before `draft_email`; `draft_email` before `read_file`.
**Checks:** name Mom; resolve uniquely before any dependent contact use text exact; replace clipboard only correct Message-ID; recipient and note exact; preserve original content Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. exact path; file content, not guessed summary Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Synthetic clipboard; no real clipboard changes
- Message-ID <route-forwarding@example.test>; outgoing Mail transport is intercepted
- Mail draft bridge is intercepted; no compose window opens on the real Mac
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0242 · Colloquial with interruptions

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Could you look up Mom's saved phone number and email address; then create a new contact named Mom Test New; then copy "Route pickup confirmed." to the clipboard; then read the text of /tmp/wisp-routing-fixtures/wrs-0242/Route proposal.txt; then email johnstandark@gmail.com now with subject Route update and body "The sample is ready."? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `lookup_contact`, `manage_contacts`, `clipboard_write`, `read_file`, `send_email`.
**Ordering constraints:** `lookup_contact` before `send_email`; `lookup_contact` before `manage_contacts`; `manage_contacts` before `clipboard_write`; `clipboard_write` before `read_file`; `read_file` before `send_email`.
**Checks:** name Mom; resolve uniquely before any dependent contact use action create; name Mom Test New; do not merge other contacts text exact; replace clipboard only exact path; file content, not guessed summary Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Mom Test New does not exist; Contacts write is intercepted
- Synthetic clipboard; no real clipboard changes
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Reserved test address only; outbound transport is intercepted and approval is simulated

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0243 · Colloquial with interruptions

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Could you look up Mom's saved phone number and email address; then add johnstandark@gmail.com to Mom's existing contact; then copy "Route pickup confirmed." to the clipboard; then text Mom at the phone number you just looked up now saying "The Route sample is ready."; then prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample."? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `lookup_contact`, `manage_contacts`, `clipboard_write`, `send_message`, `draft_message`.
**Ordering constraints:** `lookup_contact` before `send_message`; `lookup_contact` before `draft_message`; `lookup_contact` before `manage_contacts`; `manage_contacts` before `clipboard_write`; `clipboard_write` before `send_message`; `send_message` before `draft_message`.
**Checks:** name Mom; resolve uniquely before any dependent contact use action add_email; name Mom; exact value text exact; replace clipboard only Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_email; name Mom; exact value
- Synthetic clipboard; no real clipboard changes
- Fictional reserved phone number; Messages transport and approval are simulated
- Fictional test number; intercepted compose operation

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0244 · Colloquial with interruptions

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Could you look up Mom's saved phone number and email address; then add johnstandark@gmail.com to Mom's existing contact; then read the text of /tmp/wisp-routing-fixtures/wrs-0244/Route proposal.txt; then prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample."; then email johnstandark@gmail.com now with subject Route update and body "The sample is ready."? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `lookup_contact`, `manage_contacts`, `read_file`, `draft_message`, `send_email`.
**Ordering constraints:** `lookup_contact` before `draft_message`; `lookup_contact` before `send_email`; `lookup_contact` before `manage_contacts`; `manage_contacts` before `read_file`; `read_file` before `draft_message`; `draft_message` before `send_email`.
**Checks:** name Mom; resolve uniquely before any dependent contact use action add_email; name Mom; exact value exact path; file content, not guessed summary Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_email; name Mom; exact value
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Fictional test number; intercepted compose operation
- Reserved test address only; outbound transport is intercepted and approval is simulated

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0245 · Colloquial with interruptions

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Could you look up Mom's saved phone number and email address; then add phone number +1-202-555-0106 to Mom's existing contact; then text Mom at the phone number you just looked up now saying "The Route sample is ready."; then open an unsent email draft to johnstandark@gmail.com with subject Route draft and body "Please review the sample."; then copy "Route pickup confirmed." to the clipboard? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `lookup_contact`, `manage_contacts`, `send_message`, `draft_email`, `clipboard_write`.
**Ordering constraints:** `lookup_contact` before `send_message`; `lookup_contact` before `draft_email`; `lookup_contact` before `manage_contacts`; `manage_contacts` before `send_message`; `send_message` before `draft_email`; `draft_email` before `clipboard_write`.
**Checks:** name Mom; resolve uniquely before any dependent contact use action add_phone; name Mom; exact value Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. text exact; replace clipboard only Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Mom Test New does not exist; Contacts write is intercepted; variant-specific state must satisfy: action add_phone; name Mom; exact value
- Fictional reserved phone number; Messages transport and approval are simulated
- Mail draft bridge is intercepted; no compose window opens on the real Mac
- Synthetic clipboard; no real clipboard changes

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0246 · Colloquial with interruptions

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Could you look up Mom's saved phone number and email address; then read the text of /tmp/wisp-routing-fixtures/wrs-0246/Route proposal.txt; then copy "Route pickup confirmed." to the clipboard; then prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample."; then text Mom at the phone number you just looked up now saying "The Route sample is ready."? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `lookup_contact`, `read_file`, `clipboard_write`, `draft_message`, `send_message`.
**Ordering constraints:** `lookup_contact` before `draft_message`; `lookup_contact` before `send_message`; `lookup_contact` before `read_file`; `read_file` before `clipboard_write`; `clipboard_write` before `draft_message`; `draft_message` before `send_message`.
**Checks:** name Mom; resolve uniquely before any dependent contact use exact path; file content, not guessed summary text exact; replace clipboard only Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic clipboard; no real clipboard changes
- Fictional test number; intercepted compose operation
- Fictional reserved phone number; Messages transport and approval are simulated

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0247 · Colloquial with interruptions

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Could you look up Mom's saved phone number and email address; then email johnstandark@gmail.com now with subject Route update and body "The sample is ready."; then copy "Route pickup confirmed." to the clipboard; then text Mom at the phone number you just looked up now saying "The Route sample is ready."; then read the text of /tmp/wisp-routing-fixtures/wrs-0247/Route proposal.txt? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `lookup_contact`, `send_email`, `clipboard_write`, `send_message`, `read_file`.
**Ordering constraints:** `lookup_contact` before `send_email`; `lookup_contact` before `send_message`; `send_email` before `clipboard_write`; `clipboard_write` before `send_message`; `send_message` before `read_file`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. text exact; replace clipboard only Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. exact path; file content, not guessed summary Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Synthetic clipboard; no real clipboard changes
- Fictional reserved phone number; Messages transport and approval are simulated
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0248 · Colloquial with interruptions

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Could you look up Mom's saved phone number and email address; then email johnstandark@gmail.com now with subject Route update and body "The sample is ready."; then forward the Route forwarding test email to johnstandark@gmail.com with the note "Please review this."; then reply to everyone on the Route delivery email thread with "Thanks, I received the code."; then read the text of /tmp/wisp-routing-fixtures/wrs-0248/Route proposal.txt? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `lookup_contact`, `send_email`, `forward_email`, `reply_to_email`, `read_file`.
**Ordering constraints:** `lookup_contact` before `send_email`; `send_email` before `forward_email`; `forward_email` before `reply_to_email`; `reply_to_email` before `read_file`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. correct Message-ID; recipient and note exact; preserve original content reply_all true; correct Message-ID; show full reply exact path; file content, not guessed summary Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Message-ID <route-forwarding@example.test>; outgoing Mail transport is intercepted
- Original thread fixture exists; simulated confirmation only; variant-specific state must satisfy: reply_all true; correct Message-ID; show full reply
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0249 · Colloquial with interruptions

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Could you look up Mom's saved phone number and email address; then email johnstandark@gmail.com now with subject Route update and body "The sample is ready."; then text Mom at the phone number you just looked up now saying "The Route sample is ready."; then prepare an unsent text to Mom at the phone number you just looked up saying "Please review the Route sample."; then read the text of /tmp/wisp-routing-fixtures/wrs-0249/Route proposal.txt? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `lookup_contact`, `send_email`, `send_message`, `draft_message`, `read_file`.
**Ordering constraints:** `lookup_contact` before `send_email`; `lookup_contact` before `send_message`; `lookup_contact` before `draft_message`; `send_email` before `send_message`; `send_message` before `draft_message`; `draft_message` before `read_file`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. exact path; file content, not guessed summary Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Fictional reserved phone number; Messages transport and approval are simulated
- Fictional test number; intercepted compose operation
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0250 · Colloquial with interruptions

**Focus:** Preserve the selected channel and distinguish drafts from irreversible sends.

**Prompt:**

Could you look up Mom's saved phone number and email address; then email johnstandark@gmail.com now with subject Route update and body "The sample is ready."; then text Mom at the phone number you just looked up now saying "The Route sample is ready."; then read the text of /tmp/wisp-routing-fixtures/wrs-0250/Route proposal.txt; then reply to everyone on the Route delivery email thread with "Thanks, I received the code."? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `lookup_contact`, `send_email`, `send_message`, `read_file`, `reply_to_email`.
**Ordering constraints:** `lookup_contact` before `send_email`; `lookup_contact` before `send_message`; `send_email` before `send_message`; `send_message` before `read_file`; `read_file` before `reply_to_email`.
**Checks:** name Mom; resolve uniquely before any dependent contact use Use the explicitly requested test address johnstandark@gmail.com; preserve requested send versus draft, subject, and body. Use Mom's retrieved +1-202-555-0104 number; preserve requested draft versus send and exact text. exact path; file content, not guessed summary reply_all true; correct Message-ID; show full reply Preserve the selected channel and distinguish drafts from irreversible sends.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One Mom: +1-202-555-0104, mom.fixture@example.test; not the user's identity
- Reserved test address only; outbound transport is intercepted and approval is simulated
- Fictional reserved phone number; Messages transport and approval are simulated
- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Original thread fixture exists; simulated confirmation only; variant-specific state must satisfy: reply_all true; correct Message-ID; show full reply

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.
