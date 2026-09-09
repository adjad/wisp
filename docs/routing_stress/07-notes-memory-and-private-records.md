# Review only — no tests run

## 07. Notes, memory, and private records

Choose the correct persistence destination and preserve existing content.

### WRS-0301 · Explicit sequence

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Please do these in this order: append "Bring a spare cable." to my existing Route packing note; then summarize my manually logged health entries from the last seven days; then show a preview, then delete only saved facts containing Route old job; I authorize that exact scoped cleanup; then remember that I prefer Route project meetings in the morning; then forget the single saved fact that my old Route locker is number 12.

**Required tools:** `append_note`, `health_summary`, `clear_memory`, `remember`, `forget`.
**Ordering constraints:** `append_note` before `health_summary`; `health_summary` before `clear_memory`; `clear_memory` before `remember`; `remember` before `forget`.
**Checks:** title Route packing; append exact line; preserve body days 7; only logged facts, not medical inference query Route old job; preview then confirm true; preserve other facts durable preference; not a timed reminder precise single fact; preserve other memories Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Unique existing Route packing note; backend cache may not refresh immediately
- Synthetic health-tagged journal entries only
- Two synthetic memories match; no real memory store; variant-specific state must satisfy: query Route old job; preview then confirm true; preserve other facts
- Synthetic memory store; no real personal facts
- One exact old locker fact plus unrelated facts

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0302 · Explicit sequence

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Please do these in this order: append "Bring a spare cable." to my existing Route packing note; then open Notes' document scanner so I can scan the Route receipt; then forget the single saved fact that my old Route locker is number 12; then set my locally tracked daily steps goal to 8000; then log "Walked 3 miles." in my private workout journal.

**Required tools:** `append_note`, `scan_to_note`, `forget`, `set_fitness_goal`, `log_entry`.
**Ordering constraints:** `append_note` before `scan_to_note`; `scan_to_note` before `forget`; `forget` before `set_fitness_goal`; `set_fitness_goal` before `log_entry`.
**Checks:** title Route packing; append exact line; preserve body manual scanner handoff, not OCR or completed scan precise single fact; preserve other memories name daily steps; target 8000; not wearable configuration category workout; text exact; not Apple Health Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Unique existing Route packing note; backend cache may not refresh immediately
- Synthetic native bridge represents paired device and accessibility; no real UI action
- One exact old locker fact plus unrelated facts
- Synthetic goal store
- Synthetic local journal database

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0303 · Explicit sequence

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Please do these in this order: show a preview, then delete only saved facts containing Route old job; I authorize that exact scoped cleanup; then log "Walked 3 miles." in my private workout journal; then show the private workout entries I logged over the last seven days; then summarize my manually logged health entries from the last seven days; then open Notes' document scanner so I can scan the Route receipt.

**Required tools:** `clear_memory`, `log_entry`, `read_log`, `health_summary`, `scan_to_note`.
**Ordering constraints:** `clear_memory` before `log_entry`; `log_entry` before `read_log`; `read_log` before `health_summary`; `health_summary` before `scan_to_note`.
**Checks:** query Route old job; preview then confirm true; preserve other facts category workout; text exact; not Apple Health category workout; days 7; preserve timestamps days 7; only logged facts, not medical inference manual scanner handoff, not OCR or completed scan Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Two synthetic memories match; no real memory store; variant-specific state must satisfy: query Route old job; preview then confirm true; preserve other facts
- Synthetic local journal database
- Three synthetic workout entries; one older entry excluded
- Synthetic health-tagged journal entries only
- Synthetic native bridge represents paired device and accessibility; no real UI action

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0304 · Explicit sequence

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Please do these in this order: create a Notes note called Route groceries with Milk and Bread as separate checklist lines; then set my locally tracked daily steps goal to 8000; then open Notes' document scanner so I can scan the Route receipt; then append "Bring a spare cable." to my existing Route packing note; then log "Walked 3 miles." in my private workout journal.

**Required tools:** `create_note`, `set_fitness_goal`, `scan_to_note`, `append_note`, `log_entry`.
**Ordering constraints:** `create_note` before `set_fitness_goal`; `set_fitness_goal` before `scan_to_note`; `scan_to_note` before `append_note`; `append_note` before `log_entry`.
**Checks:** title Route groceries; checklist true; disclose bullet-list limitation if relevant name daily steps; target 8000; not wearable configuration manual scanner handoff, not OCR or completed scan title Route packing; append exact line; preserve body category workout; text exact; not Apple Health Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- No existing Route ideas note; native Notes operation is intercepted; variant-specific state must satisfy: title Route groceries; checklist true; disclose bullet-list limitation if relevant
- Synthetic goal store
- Synthetic native bridge represents paired device and accessibility; no real UI action
- Unique existing Route packing note; backend cache may not refresh immediately
- Synthetic local journal database

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0305 · Explicit sequence

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Please do these in this order: forget the single saved fact that my old Route locker is number 12; then show the private workout entries I logged over the last seven days; then create a Notes note called Route groceries with Milk and Bread as separate checklist lines; then log "Walked 3 miles." in my private workout journal; then append "Bring a spare cable." to my existing Route packing note.

**Required tools:** `forget`, `read_log`, `create_note`, `log_entry`, `append_note`.
**Ordering constraints:** `forget` before `read_log`; `read_log` before `create_note`; `create_note` before `log_entry`; `log_entry` before `append_note`.
**Checks:** precise single fact; preserve other memories category workout; days 7; preserve timestamps title Route groceries; checklist true; disclose bullet-list limitation if relevant category workout; text exact; not Apple Health title Route packing; append exact line; preserve body Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One exact old locker fact plus unrelated facts
- Three synthetic workout entries; one older entry excluded
- No existing Route ideas note; native Notes operation is intercepted; variant-specific state must satisfy: title Route groceries; checklist true; disclose bullet-list limitation if relevant
- Synthetic local journal database
- Unique existing Route packing note; backend cache may not refresh immediately

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0306 · Explicit sequence

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Please do these in this order: summarize my manually logged health entries from the last seven days; then open Notes' document scanner so I can scan the Route receipt; then set my locally tracked daily steps goal to 8000; then show a preview, then delete only saved facts containing Route old job; I authorize that exact scoped cleanup; then create a Notes note called Route groceries with Milk and Bread as separate checklist lines.

**Required tools:** `health_summary`, `scan_to_note`, `set_fitness_goal`, `clear_memory`, `create_note`.
**Ordering constraints:** `health_summary` before `scan_to_note`; `scan_to_note` before `set_fitness_goal`; `set_fitness_goal` before `clear_memory`; `clear_memory` before `create_note`.
**Checks:** days 7; only logged facts, not medical inference manual scanner handoff, not OCR or completed scan name daily steps; target 8000; not wearable configuration query Route old job; preview then confirm true; preserve other facts title Route groceries; checklist true; disclose bullet-list limitation if relevant Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic health-tagged journal entries only
- Synthetic native bridge represents paired device and accessibility; no real UI action
- Synthetic goal store
- Two synthetic memories match; no real memory store; variant-specific state must satisfy: query Route old job; preview then confirm true; preserve other facts
- No existing Route ideas note; native Notes operation is intercepted; variant-specific state must satisfy: title Route groceries; checklist true; disclose bullet-list limitation if relevant

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0307 · Explicit sequence

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Please do these in this order: log "Walked 3 miles." in my private workout journal; then append "Bring a spare cable." to my existing Route packing note; then summarize my manually logged health entries from the last seven days; then forget the single saved fact that my old Route locker is number 12; then open Notes' document scanner so I can scan the Route receipt.

**Required tools:** `log_entry`, `append_note`, `health_summary`, `forget`, `scan_to_note`.
**Ordering constraints:** `log_entry` before `append_note`; `append_note` before `health_summary`; `health_summary` before `forget`; `forget` before `scan_to_note`.
**Checks:** category workout; text exact; not Apple Health title Route packing; append exact line; preserve body days 7; only logged facts, not medical inference precise single fact; preserve other memories manual scanner handoff, not OCR or completed scan Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic local journal database
- Unique existing Route packing note; backend cache may not refresh immediately
- Synthetic health-tagged journal entries only
- One exact old locker fact plus unrelated facts
- Synthetic native bridge represents paired device and accessibility; no real UI action

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0308 · Explicit sequence

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Please do these in this order: open Notes' document scanner so I can scan the Route receipt; then show the private workout entries I logged over the last seven days; then append "Bring a spare cable." to my existing Route packing note; then preview the saved memories mentioning Route old job, without deleting them; then summarize my manually logged health entries from the last seven days.

**Required tools:** `scan_to_note`, `read_log`, `append_note`, `clear_memory`, `health_summary`.
**Ordering constraints:** `scan_to_note` before `read_log`; `read_log` before `append_note`; `append_note` before `clear_memory`; `clear_memory` before `health_summary`.
**Checks:** manual scanner handoff, not OCR or completed scan category workout; days 7; preserve timestamps title Route packing; append exact line; preserve body query Route old job; confirm false; preview only days 7; only logged facts, not medical inference Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic native bridge represents paired device and accessibility; no real UI action
- Three synthetic workout entries; one older entry excluded
- Unique existing Route packing note; backend cache may not refresh immediately
- Two synthetic memories match; no real memory store
- Synthetic health-tagged journal entries only

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0309 · Explicit sequence

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Please do these in this order: set my locally tracked daily steps goal to 8000; then show a preview, then delete only saved facts containing Route old job; I authorize that exact scoped cleanup; then remember that I prefer Route project meetings in the morning; then open Notes' document scanner so I can scan the Route receipt; then summarize my manually logged health entries from the last seven days.

**Required tools:** `set_fitness_goal`, `clear_memory`, `remember`, `scan_to_note`, `health_summary`.
**Ordering constraints:** `set_fitness_goal` before `clear_memory`; `clear_memory` before `remember`; `remember` before `scan_to_note`; `scan_to_note` before `health_summary`.
**Checks:** name daily steps; target 8000; not wearable configuration query Route old job; preview then confirm true; preserve other facts durable preference; not a timed reminder manual scanner handoff, not OCR or completed scan days 7; only logged facts, not medical inference Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic goal store
- Two synthetic memories match; no real memory store; variant-specific state must satisfy: query Route old job; preview then confirm true; preserve other facts
- Synthetic memory store; no real personal facts
- Synthetic native bridge represents paired device and accessibility; no real UI action
- Synthetic health-tagged journal entries only

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0310 · Explicit sequence

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Please do these in this order: set my locally tracked daily steps goal to 8000; then remember that I prefer Route project meetings in the morning; then create a Notes note called Route groceries with Milk and Bread as separate checklist lines; then show a preview, then delete only saved facts containing Route old job; I authorize that exact scoped cleanup; then summarize my manually logged health entries from the last seven days.

**Required tools:** `set_fitness_goal`, `remember`, `create_note`, `clear_memory`, `health_summary`.
**Ordering constraints:** `set_fitness_goal` before `remember`; `remember` before `create_note`; `create_note` before `clear_memory`; `clear_memory` before `health_summary`.
**Checks:** name daily steps; target 8000; not wearable configuration durable preference; not a timed reminder title Route groceries; checklist true; disclose bullet-list limitation if relevant query Route old job; preview then confirm true; preserve other facts days 7; only logged facts, not medical inference Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic goal store
- Synthetic memory store; no real personal facts
- No existing Route ideas note; native Notes operation is intercepted; variant-specific state must satisfy: title Route groceries; checklist true; disclose bullet-list limitation if relevant
- Two synthetic memories match; no real memory store; variant-specific state must satisfy: query Route old job; preview then confirm true; preserve other facts
- Synthetic health-tagged journal entries only

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0311 · Natural compound request

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

I have a few things to finish. Append "Bring a spare cable." to my existing Route packing note. Open Notes' document scanner so I can scan the Route receipt. Show the private workout entries I logged over the last seven days. Create a Notes note called Route groceries with Milk and Bread as separate checklist lines. Remember that I prefer Route project meetings in the morning. Keep the results separate so I can tell what came from where.

**Required tools:** `append_note`, `scan_to_note`, `read_log`, `create_note`, `remember`.
**Checks:** title Route packing; append exact line; preserve body manual scanner handoff, not OCR or completed scan category workout; days 7; preserve timestamps title Route groceries; checklist true; disclose bullet-list limitation if relevant durable preference; not a timed reminder Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Unique existing Route packing note; backend cache may not refresh immediately
- Synthetic native bridge represents paired device and accessibility; no real UI action
- Three synthetic workout entries; one older entry excluded
- No existing Route ideas note; native Notes operation is intercepted; variant-specific state must satisfy: title Route groceries; checklist true; disclose bullet-list limitation if relevant
- Synthetic memory store; no real personal facts

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0312 · Natural compound request

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

I have a few things to finish. Append "Bring a spare cable." to my existing Route packing note. Set my locally tracked daily steps goal to 8000. Create a Notes note called Route groceries with Milk and Bread as separate checklist lines. Show the private workout entries I logged over the last seven days. Show a preview, then delete only saved facts containing Route old job; I authorize that exact scoped cleanup. Keep the results separate so I can tell what came from where.

**Required tools:** `append_note`, `set_fitness_goal`, `create_note`, `read_log`, `clear_memory`.
**Checks:** title Route packing; append exact line; preserve body name daily steps; target 8000; not wearable configuration title Route groceries; checklist true; disclose bullet-list limitation if relevant category workout; days 7; preserve timestamps query Route old job; preview then confirm true; preserve other facts Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Unique existing Route packing note; backend cache may not refresh immediately
- Synthetic goal store
- No existing Route ideas note; native Notes operation is intercepted; variant-specific state must satisfy: title Route groceries; checklist true; disclose bullet-list limitation if relevant
- Three synthetic workout entries; one older entry excluded
- Two synthetic memories match; no real memory store; variant-specific state must satisfy: query Route old job; preview then confirm true; preserve other facts

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0313 · Natural compound request

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

I have a few things to finish. Forget the single saved fact that my old Route locker is number 12. Log "Walked 3 miles." in my private workout journal. Set my locally tracked daily steps goal to 8000. Create a Notes note called Route groceries with Milk and Bread as separate checklist lines. Show the private workout entries I logged over the last seven days. Keep the results separate so I can tell what came from where.

**Required tools:** `forget`, `log_entry`, `set_fitness_goal`, `create_note`, `read_log`.
**Checks:** precise single fact; preserve other memories category workout; text exact; not Apple Health name daily steps; target 8000; not wearable configuration title Route groceries; checklist true; disclose bullet-list limitation if relevant category workout; days 7; preserve timestamps Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One exact old locker fact plus unrelated facts
- Synthetic local journal database
- Synthetic goal store
- No existing Route ideas note; native Notes operation is intercepted; variant-specific state must satisfy: title Route groceries; checklist true; disclose bullet-list limitation if relevant
- Three synthetic workout entries; one older entry excluded

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0314 · Natural compound request

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

I have a few things to finish. Show the private workout entries I logged over the last seven days. Append "Bring a spare cable." to my existing Route packing note. Log "Walked 3 miles." in my private workout journal. Set my locally tracked daily steps goal to 8000. Open Notes' document scanner so I can scan the Route receipt. Keep the results separate so I can tell what came from where.

**Required tools:** `read_log`, `append_note`, `log_entry`, `set_fitness_goal`, `scan_to_note`.
**Checks:** category workout; days 7; preserve timestamps title Route packing; append exact line; preserve body category workout; text exact; not Apple Health name daily steps; target 8000; not wearable configuration manual scanner handoff, not OCR or completed scan Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Three synthetic workout entries; one older entry excluded
- Unique existing Route packing note; backend cache may not refresh immediately
- Synthetic local journal database
- Synthetic goal store
- Synthetic native bridge represents paired device and accessibility; no real UI action

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0315 · Natural compound request

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

I have a few things to finish. Remember that I prefer Route project meetings in the morning. Log "Walked 3 miles." in my private workout journal. Show a preview, then delete only saved facts containing Route old job; I authorize that exact scoped cleanup. Summarize my manually logged health entries from the last seven days. Create a Notes note called Route groceries with Milk and Bread as separate checklist lines. Keep the results separate so I can tell what came from where.

**Required tools:** `remember`, `log_entry`, `clear_memory`, `health_summary`, `create_note`.
**Checks:** durable preference; not a timed reminder category workout; text exact; not Apple Health query Route old job; preview then confirm true; preserve other facts days 7; only logged facts, not medical inference title Route groceries; checklist true; disclose bullet-list limitation if relevant Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic memory store; no real personal facts
- Synthetic local journal database
- Two synthetic memories match; no real memory store; variant-specific state must satisfy: query Route old job; preview then confirm true; preserve other facts
- Synthetic health-tagged journal entries only
- No existing Route ideas note; native Notes operation is intercepted; variant-specific state must satisfy: title Route groceries; checklist true; disclose bullet-list limitation if relevant

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0316 · Natural compound request

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

I have a few things to finish. Remember that I prefer Route project meetings in the morning. Show the private workout entries I logged over the last seven days. Summarize my manually logged health entries from the last seven days. Tell me what you remember about my Route bicycle. Find what we previously said in Wisp chats about Route renovation. Keep the results separate so I can tell what came from where.

**Required tools:** `remember`, `read_log`, `health_summary`, `recall`, `search_conversations`.
**Checks:** durable preference; not a timed reminder category workout; days 7; preserve timestamps days 7; only logged facts, not medical inference query Route bicycle; saved facts, not Messages query Route renovation; transcripts, not Contacts or messages Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic memory store; no real personal facts
- Three synthetic workout entries; one older entry excluded
- Synthetic health-tagged journal entries only
- Saved fact: Route bicycle is blue; transcript contains a conflicting old statement
- Two synthetic past Wisp sessions mention Route renovation

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0317 · Natural compound request

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

I have a few things to finish. Open Notes' document scanner so I can scan the Route receipt. Show the private workout entries I logged over the last seven days. Show a preview, then delete only saved facts containing Route old job; I authorize that exact scoped cleanup. Forget the single saved fact that my old Route locker is number 12. Remember that I prefer Route project meetings in the morning. Keep the results separate so I can tell what came from where.

**Required tools:** `scan_to_note`, `read_log`, `clear_memory`, `forget`, `remember`.
**Checks:** manual scanner handoff, not OCR or completed scan category workout; days 7; preserve timestamps query Route old job; preview then confirm true; preserve other facts precise single fact; preserve other memories durable preference; not a timed reminder Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic native bridge represents paired device and accessibility; no real UI action
- Three synthetic workout entries; one older entry excluded
- Two synthetic memories match; no real memory store; variant-specific state must satisfy: query Route old job; preview then confirm true; preserve other facts
- One exact old locker fact plus unrelated facts
- Synthetic memory store; no real personal facts

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0318 · Natural compound request

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

I have a few things to finish. Open Notes' document scanner so I can scan the Route receipt. Set my locally tracked daily steps goal to 8000. Forget the single saved fact that my old Route locker is number 12. Create a Notes note titled Route ideas containing "Test the pickup workflow.". Log "Walked 3 miles." in my private workout journal. Keep the results separate so I can tell what came from where.

**Required tools:** `scan_to_note`, `set_fitness_goal`, `forget`, `create_note`, `log_entry`.
**Checks:** manual scanner handoff, not OCR or completed scan name daily steps; target 8000; not wearable configuration precise single fact; preserve other memories title/body exact; actual Notes destination category workout; text exact; not Apple Health Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic native bridge represents paired device and accessibility; no real UI action
- Synthetic goal store
- One exact old locker fact plus unrelated facts
- No existing Route ideas note; native Notes operation is intercepted
- Synthetic local journal database

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0319 · Natural compound request

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

I have a few things to finish. Open Notes' document scanner so I can scan the Route receipt. Set my locally tracked daily steps goal to 8000. Forget the single saved fact that my old Route locker is number 12. Show the private workout entries I logged over the last seven days. Log "Walked 3 miles." in my private workout journal. Keep the results separate so I can tell what came from where.

**Required tools:** `scan_to_note`, `set_fitness_goal`, `forget`, `read_log`, `log_entry`.
**Checks:** manual scanner handoff, not OCR or completed scan name daily steps; target 8000; not wearable configuration precise single fact; preserve other memories category workout; days 7; preserve timestamps category workout; text exact; not Apple Health Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic native bridge represents paired device and accessibility; no real UI action
- Synthetic goal store
- One exact old locker fact plus unrelated facts
- Three synthetic workout entries; one older entry excluded
- Synthetic local journal database

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0320 · Natural compound request

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

I have a few things to finish. Set my locally tracked daily steps goal to 8000. Create a Notes note called Route groceries with Milk and Bread as separate checklist lines. Show the private workout entries I logged over the last seven days. Forget the single saved fact that my old Route locker is number 12. Remember that I prefer Route project meetings in the morning. Keep the results separate so I can tell what came from where.

**Required tools:** `set_fitness_goal`, `create_note`, `read_log`, `forget`, `remember`.
**Checks:** name daily steps; target 8000; not wearable configuration title Route groceries; checklist true; disclose bullet-list limitation if relevant category workout; days 7; preserve timestamps precise single fact; preserve other memories durable preference; not a timed reminder Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic goal store
- No existing Route ideas note; native Notes operation is intercepted; variant-specific state must satisfy: title Route groceries; checklist true; disclose bullet-list limitation if relevant
- Three synthetic workout entries; one older entry excluded
- One exact old locker fact plus unrelated facts
- Synthetic memory store; no real personal facts

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0321 · Scoped execution

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

For these tasks, use only the named sources and targets: show a preview, then delete only saved facts containing Route old job; I authorize that exact scoped cleanup; then remember that I prefer Route project meetings in the morning; then forget the single saved fact that my old Route locker is number 12; then summarize my manually logged health entries from the last seven days; then show the private workout entries I logged over the last seven days. Leave everything else unchanged.

**Required tools:** `clear_memory`, `remember`, `forget`, `health_summary`, `read_log`.
**Ordering constraints:** `clear_memory` before `remember`; `remember` before `forget`; `forget` before `health_summary`; `health_summary` before `read_log`.
**Checks:** query Route old job; preview then confirm true; preserve other facts durable preference; not a timed reminder precise single fact; preserve other memories days 7; only logged facts, not medical inference category workout; days 7; preserve timestamps Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Two synthetic memories match; no real memory store; variant-specific state must satisfy: query Route old job; preview then confirm true; preserve other facts
- Synthetic memory store; no real personal facts
- One exact old locker fact plus unrelated facts
- Synthetic health-tagged journal entries only
- Three synthetic workout entries; one older entry excluded

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0322 · Scoped execution

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

For these tasks, use only the named sources and targets: forget the single saved fact that my old Route locker is number 12; then summarize my manually logged health entries from the last seven days; then log "Walked 3 miles." in my private workout journal; then remember that I prefer Route project meetings in the morning; then append "Bring a spare cable." to my existing Route packing note. Leave everything else unchanged.

**Required tools:** `forget`, `health_summary`, `log_entry`, `remember`, `append_note`.
**Ordering constraints:** `forget` before `health_summary`; `health_summary` before `log_entry`; `log_entry` before `remember`; `remember` before `append_note`.
**Checks:** precise single fact; preserve other memories days 7; only logged facts, not medical inference category workout; text exact; not Apple Health durable preference; not a timed reminder title Route packing; append exact line; preserve body Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One exact old locker fact plus unrelated facts
- Synthetic health-tagged journal entries only
- Synthetic local journal database
- Synthetic memory store; no real personal facts
- Unique existing Route packing note; backend cache may not refresh immediately

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0323 · Scoped execution

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

For these tasks, use only the named sources and targets: forget the single saved fact that my old Route locker is number 12; then remember that I prefer Route project meetings in the morning; then log "Walked 3 miles." in my private workout journal; then create a Notes note called Route groceries with Milk and Bread as separate checklist lines; then open Notes' document scanner so I can scan the Route receipt. Leave everything else unchanged.

**Required tools:** `forget`, `remember`, `log_entry`, `create_note`, `scan_to_note`.
**Ordering constraints:** `forget` before `remember`; `remember` before `log_entry`; `log_entry` before `create_note`; `create_note` before `scan_to_note`.
**Checks:** precise single fact; preserve other memories durable preference; not a timed reminder category workout; text exact; not Apple Health title Route groceries; checklist true; disclose bullet-list limitation if relevant manual scanner handoff, not OCR or completed scan Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One exact old locker fact plus unrelated facts
- Synthetic memory store; no real personal facts
- Synthetic local journal database
- No existing Route ideas note; native Notes operation is intercepted; variant-specific state must satisfy: title Route groceries; checklist true; disclose bullet-list limitation if relevant
- Synthetic native bridge represents paired device and accessibility; no real UI action

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0324 · Scoped execution

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

For these tasks, use only the named sources and targets: log "Walked 3 miles." in my private workout journal; then create a Notes note called Route groceries with Milk and Bread as separate checklist lines; then show a preview, then delete only saved facts containing Route old job; I authorize that exact scoped cleanup; then open Notes' document scanner so I can scan the Route receipt; then forget the single saved fact that my old Route locker is number 12. Leave everything else unchanged.

**Required tools:** `log_entry`, `create_note`, `clear_memory`, `scan_to_note`, `forget`.
**Ordering constraints:** `log_entry` before `create_note`; `create_note` before `clear_memory`; `clear_memory` before `scan_to_note`; `scan_to_note` before `forget`.
**Checks:** category workout; text exact; not Apple Health title Route groceries; checklist true; disclose bullet-list limitation if relevant query Route old job; preview then confirm true; preserve other facts manual scanner handoff, not OCR or completed scan precise single fact; preserve other memories Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic local journal database
- No existing Route ideas note; native Notes operation is intercepted; variant-specific state must satisfy: title Route groceries; checklist true; disclose bullet-list limitation if relevant
- Two synthetic memories match; no real memory store; variant-specific state must satisfy: query Route old job; preview then confirm true; preserve other facts
- Synthetic native bridge represents paired device and accessibility; no real UI action
- One exact old locker fact plus unrelated facts

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0325 · Scoped execution

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

For these tasks, use only the named sources and targets: log "Walked 3 miles." in my private workout journal; then set my locally tracked daily steps goal to 8000; then create a Notes note called Route groceries with Milk and Bread as separate checklist lines; then summarize my manually logged health entries from the last seven days; then append "Bring a spare cable." to my existing Route packing note. Leave everything else unchanged.

**Required tools:** `log_entry`, `set_fitness_goal`, `create_note`, `health_summary`, `append_note`.
**Ordering constraints:** `log_entry` before `set_fitness_goal`; `set_fitness_goal` before `create_note`; `create_note` before `health_summary`; `health_summary` before `append_note`.
**Checks:** category workout; text exact; not Apple Health name daily steps; target 8000; not wearable configuration title Route groceries; checklist true; disclose bullet-list limitation if relevant days 7; only logged facts, not medical inference title Route packing; append exact line; preserve body Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic local journal database
- Synthetic goal store
- No existing Route ideas note; native Notes operation is intercepted; variant-specific state must satisfy: title Route groceries; checklist true; disclose bullet-list limitation if relevant
- Synthetic health-tagged journal entries only
- Unique existing Route packing note; backend cache may not refresh immediately

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0326 · Scoped execution

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

For these tasks, use only the named sources and targets: remember that I prefer Route project meetings in the morning; then summarize my manually logged health entries from the last seven days; then show a preview, then delete only saved facts containing Route old job; I authorize that exact scoped cleanup; then set my locally tracked daily steps goal to 8000; then create a Notes note called Route groceries with Milk and Bread as separate checklist lines. Leave everything else unchanged.

**Required tools:** `remember`, `health_summary`, `clear_memory`, `set_fitness_goal`, `create_note`.
**Ordering constraints:** `remember` before `health_summary`; `health_summary` before `clear_memory`; `clear_memory` before `set_fitness_goal`; `set_fitness_goal` before `create_note`.
**Checks:** durable preference; not a timed reminder days 7; only logged facts, not medical inference query Route old job; preview then confirm true; preserve other facts name daily steps; target 8000; not wearable configuration title Route groceries; checklist true; disclose bullet-list limitation if relevant Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic memory store; no real personal facts
- Synthetic health-tagged journal entries only
- Two synthetic memories match; no real memory store; variant-specific state must satisfy: query Route old job; preview then confirm true; preserve other facts
- Synthetic goal store
- No existing Route ideas note; native Notes operation is intercepted; variant-specific state must satisfy: title Route groceries; checklist true; disclose bullet-list limitation if relevant

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0327 · Scoped execution

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

For these tasks, use only the named sources and targets: open Notes' document scanner so I can scan the Route receipt; then log "Walked 3 miles." in my private workout journal; then append "Bring a spare cable." to my existing Route packing note; then forget the single saved fact that my old Route locker is number 12; then summarize my manually logged health entries from the last seven days. Leave everything else unchanged.

**Required tools:** `scan_to_note`, `log_entry`, `append_note`, `forget`, `health_summary`.
**Ordering constraints:** `scan_to_note` before `log_entry`; `log_entry` before `append_note`; `append_note` before `forget`; `forget` before `health_summary`.
**Checks:** manual scanner handoff, not OCR or completed scan category workout; text exact; not Apple Health title Route packing; append exact line; preserve body precise single fact; preserve other memories days 7; only logged facts, not medical inference Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic native bridge represents paired device and accessibility; no real UI action
- Synthetic local journal database
- Unique existing Route packing note; backend cache may not refresh immediately
- One exact old locker fact plus unrelated facts
- Synthetic health-tagged journal entries only

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0328 · Scoped execution

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

For these tasks, use only the named sources and targets: set my locally tracked daily steps goal to 8000; then remember that I prefer Route project meetings in the morning; then create a Notes note called Route groceries with Milk and Bread as separate checklist lines; then forget the single saved fact that my old Route locker is number 12; then open Notes' document scanner so I can scan the Route receipt. Leave everything else unchanged.

**Required tools:** `set_fitness_goal`, `remember`, `create_note`, `forget`, `scan_to_note`.
**Ordering constraints:** `set_fitness_goal` before `remember`; `remember` before `create_note`; `create_note` before `forget`; `forget` before `scan_to_note`.
**Checks:** name daily steps; target 8000; not wearable configuration durable preference; not a timed reminder title Route groceries; checklist true; disclose bullet-list limitation if relevant precise single fact; preserve other memories manual scanner handoff, not OCR or completed scan Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic goal store
- Synthetic memory store; no real personal facts
- No existing Route ideas note; native Notes operation is intercepted; variant-specific state must satisfy: title Route groceries; checklist true; disclose bullet-list limitation if relevant
- One exact old locker fact plus unrelated facts
- Synthetic native bridge represents paired device and accessibility; no real UI action

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0329 · Scoped execution

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

For these tasks, use only the named sources and targets: set my locally tracked daily steps goal to 8000; then remember that I prefer Route project meetings in the morning; then summarize my manually logged health entries from the last seven days; then log "Walked 3 miles." in my private workout journal; then create a Notes note titled Route ideas containing "Test the pickup workflow.". Leave everything else unchanged.

**Required tools:** `set_fitness_goal`, `remember`, `health_summary`, `log_entry`, `create_note`.
**Ordering constraints:** `set_fitness_goal` before `remember`; `remember` before `health_summary`; `health_summary` before `log_entry`; `log_entry` before `create_note`.
**Checks:** name daily steps; target 8000; not wearable configuration durable preference; not a timed reminder days 7; only logged facts, not medical inference category workout; text exact; not Apple Health title/body exact; actual Notes destination Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic goal store
- Synthetic memory store; no real personal facts
- Synthetic health-tagged journal entries only
- Synthetic local journal database
- No existing Route ideas note; native Notes operation is intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0330 · Scoped execution

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

For these tasks, use only the named sources and targets: set my locally tracked daily steps goal to 8000; then open Notes' document scanner so I can scan the Route receipt; then find what we previously said in Wisp chats about Route renovation; then append "Bring a spare cable." to my existing Route packing note; then forget the single saved fact that my old Route locker is number 12. Leave everything else unchanged.

**Required tools:** `set_fitness_goal`, `scan_to_note`, `search_conversations`, `append_note`, `forget`.
**Ordering constraints:** `set_fitness_goal` before `scan_to_note`; `scan_to_note` before `search_conversations`; `search_conversations` before `append_note`; `append_note` before `forget`.
**Checks:** name daily steps; target 8000; not wearable configuration manual scanner handoff, not OCR or completed scan query Route renovation; transcripts, not Contacts or messages title Route packing; append exact line; preserve body precise single fact; preserve other memories Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic goal store
- Synthetic native bridge represents paired device and accessibility; no real UI action
- Two synthetic past Wisp sessions mention Route renovation
- Unique existing Route packing note; backend cache may not refresh immediately
- One exact old locker fact plus unrelated facts

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0331 · Late constraints

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Append "Bring a spare cable." to my existing Route packing note. Show a preview, then delete only saved facts containing Route old job; I authorize that exact scoped cleanup. Remember that I prefer Route project meetings in the morning. Show the private workout entries I logged over the last seven days. Set my locally tracked daily steps goal to 8000. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `append_note`, `clear_memory`, `remember`, `read_log`, `set_fitness_goal`.
**Checks:** title Route packing; append exact line; preserve body query Route old job; preview then confirm true; preserve other facts durable preference; not a timed reminder category workout; days 7; preserve timestamps name daily steps; target 8000; not wearable configuration Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Unique existing Route packing note; backend cache may not refresh immediately
- Two synthetic memories match; no real memory store; variant-specific state must satisfy: query Route old job; preview then confirm true; preserve other facts
- Synthetic memory store; no real personal facts
- Three synthetic workout entries; one older entry excluded
- Synthetic goal store

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0332 · Late constraints

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Show a preview, then delete only saved facts containing Route old job; I authorize that exact scoped cleanup. Create a Notes note called Route groceries with Milk and Bread as separate checklist lines. Append "Bring a spare cable." to my existing Route packing note. Tell me what you remember about my Route bicycle. Find what we previously said in Wisp chats about Route renovation. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `clear_memory`, `create_note`, `append_note`, `recall`, `search_conversations`.
**Checks:** query Route old job; preview then confirm true; preserve other facts title Route groceries; checklist true; disclose bullet-list limitation if relevant title Route packing; append exact line; preserve body query Route bicycle; saved facts, not Messages query Route renovation; transcripts, not Contacts or messages Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Two synthetic memories match; no real memory store; variant-specific state must satisfy: query Route old job; preview then confirm true; preserve other facts
- No existing Route ideas note; native Notes operation is intercepted; variant-specific state must satisfy: title Route groceries; checklist true; disclose bullet-list limitation if relevant
- Unique existing Route packing note; backend cache may not refresh immediately
- Saved fact: Route bicycle is blue; transcript contains a conflicting old statement
- Two synthetic past Wisp sessions mention Route renovation

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0333 · Late constraints

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Show a preview, then delete only saved facts containing Route old job; I authorize that exact scoped cleanup. Set my locally tracked daily steps goal to 8000. Append "Bring a spare cable." to my existing Route packing note. Summarize my manually logged health entries from the last seven days. Forget the single saved fact that my old Route locker is number 12. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `clear_memory`, `set_fitness_goal`, `append_note`, `health_summary`, `forget`.
**Checks:** query Route old job; preview then confirm true; preserve other facts name daily steps; target 8000; not wearable configuration title Route packing; append exact line; preserve body days 7; only logged facts, not medical inference precise single fact; preserve other memories Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Two synthetic memories match; no real memory store; variant-specific state must satisfy: query Route old job; preview then confirm true; preserve other facts
- Synthetic goal store
- Unique existing Route packing note; backend cache may not refresh immediately
- Synthetic health-tagged journal entries only
- One exact old locker fact plus unrelated facts

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0334 · Late constraints

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Forget the single saved fact that my old Route locker is number 12. Show the private workout entries I logged over the last seven days. Open Notes' document scanner so I can scan the Route receipt. Append "Bring a spare cable." to my existing Route packing note. Log "Walked 3 miles." in my private workout journal. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `forget`, `read_log`, `scan_to_note`, `append_note`, `log_entry`.
**Checks:** precise single fact; preserve other memories category workout; days 7; preserve timestamps manual scanner handoff, not OCR or completed scan title Route packing; append exact line; preserve body category workout; text exact; not Apple Health Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One exact old locker fact plus unrelated facts
- Three synthetic workout entries; one older entry excluded
- Synthetic native bridge represents paired device and accessibility; no real UI action
- Unique existing Route packing note; backend cache may not refresh immediately
- Synthetic local journal database

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0335 · Late constraints

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Summarize my manually logged health entries from the last seven days. Log "Walked 3 miles." in my private workout journal. Show the private workout entries I logged over the last seven days. Remember that I prefer Route project meetings in the morning. Create a Notes note called Route groceries with Milk and Bread as separate checklist lines. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `health_summary`, `log_entry`, `read_log`, `remember`, `create_note`.
**Checks:** days 7; only logged facts, not medical inference category workout; text exact; not Apple Health category workout; days 7; preserve timestamps durable preference; not a timed reminder title Route groceries; checklist true; disclose bullet-list limitation if relevant Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic health-tagged journal entries only
- Synthetic local journal database
- Three synthetic workout entries; one older entry excluded
- Synthetic memory store; no real personal facts
- No existing Route ideas note; native Notes operation is intercepted; variant-specific state must satisfy: title Route groceries; checklist true; disclose bullet-list limitation if relevant

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0336 · Late constraints

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Summarize my manually logged health entries from the last seven days. Show the private workout entries I logged over the last seven days. Forget the single saved fact that my old Route locker is number 12. Preview the saved memories mentioning Route old job, without deleting them. Append "Bring a spare cable." to my existing Route packing note. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `health_summary`, `read_log`, `forget`, `clear_memory`, `append_note`.
**Checks:** days 7; only logged facts, not medical inference category workout; days 7; preserve timestamps precise single fact; preserve other memories query Route old job; confirm false; preview only title Route packing; append exact line; preserve body Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic health-tagged journal entries only
- Three synthetic workout entries; one older entry excluded
- One exact old locker fact plus unrelated facts
- Two synthetic memories match; no real memory store
- Unique existing Route packing note; backend cache may not refresh immediately

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0337 · Late constraints

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Log "Walked 3 miles." in my private workout journal. Show the private workout entries I logged over the last seven days. Show a preview, then delete only saved facts containing Route old job; I authorize that exact scoped cleanup. Create a Notes note called Route groceries with Milk and Bread as separate checklist lines. Open Notes' document scanner so I can scan the Route receipt. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `log_entry`, `read_log`, `clear_memory`, `create_note`, `scan_to_note`.
**Checks:** category workout; text exact; not Apple Health category workout; days 7; preserve timestamps query Route old job; preview then confirm true; preserve other facts title Route groceries; checklist true; disclose bullet-list limitation if relevant manual scanner handoff, not OCR or completed scan Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic local journal database
- Three synthetic workout entries; one older entry excluded
- Two synthetic memories match; no real memory store; variant-specific state must satisfy: query Route old job; preview then confirm true; preserve other facts
- No existing Route ideas note; native Notes operation is intercepted; variant-specific state must satisfy: title Route groceries; checklist true; disclose bullet-list limitation if relevant
- Synthetic native bridge represents paired device and accessibility; no real UI action

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0338 · Late constraints

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Log "Walked 3 miles." in my private workout journal. Show the private workout entries I logged over the last seven days. Show a preview, then delete only saved facts containing Route old job; I authorize that exact scoped cleanup. Summarize my manually logged health entries from the last seven days. Append "Bring a spare cable." to my existing Route packing note. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `log_entry`, `read_log`, `clear_memory`, `health_summary`, `append_note`.
**Checks:** category workout; text exact; not Apple Health category workout; days 7; preserve timestamps query Route old job; preview then confirm true; preserve other facts days 7; only logged facts, not medical inference title Route packing; append exact line; preserve body Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic local journal database
- Three synthetic workout entries; one older entry excluded
- Two synthetic memories match; no real memory store; variant-specific state must satisfy: query Route old job; preview then confirm true; preserve other facts
- Synthetic health-tagged journal entries only
- Unique existing Route packing note; backend cache may not refresh immediately

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0339 · Late constraints

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Set my locally tracked daily steps goal to 8000. Append "Bring a spare cable." to my existing Route packing note. Remember that I prefer Route project meetings in the morning. Show the private workout entries I logged over the last seven days. Show a preview, then delete only saved facts containing Route old job; I authorize that exact scoped cleanup. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `set_fitness_goal`, `append_note`, `remember`, `read_log`, `clear_memory`.
**Checks:** name daily steps; target 8000; not wearable configuration title Route packing; append exact line; preserve body durable preference; not a timed reminder category workout; days 7; preserve timestamps query Route old job; preview then confirm true; preserve other facts Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic goal store
- Unique existing Route packing note; backend cache may not refresh immediately
- Synthetic memory store; no real personal facts
- Three synthetic workout entries; one older entry excluded
- Two synthetic memories match; no real memory store; variant-specific state must satisfy: query Route old job; preview then confirm true; preserve other facts

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0340 · Late constraints

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Set my locally tracked daily steps goal to 8000. Show a preview, then delete only saved facts containing Route old job; I authorize that exact scoped cleanup. Create a Notes note called Route groceries with Milk and Bread as separate checklist lines. Append "Bring a spare cable." to my existing Route packing note. Log "Walked 3 miles." in my private workout journal. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `set_fitness_goal`, `clear_memory`, `create_note`, `append_note`, `log_entry`.
**Checks:** name daily steps; target 8000; not wearable configuration query Route old job; preview then confirm true; preserve other facts title Route groceries; checklist true; disclose bullet-list limitation if relevant title Route packing; append exact line; preserve body category workout; text exact; not Apple Health Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic goal store
- Two synthetic memories match; no real memory store; variant-specific state must satisfy: query Route old job; preview then confirm true; preserve other facts
- No existing Route ideas note; native Notes operation is intercepted; variant-specific state must satisfy: title Route groceries; checklist true; disclose bullet-list limitation if relevant
- Unique existing Route packing note; backend cache may not refresh immediately
- Synthetic local journal database

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0341 · Colloquial with interruptions

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Could you append "Bring a spare cable." to my existing Route packing note; then remember that I prefer Route project meetings in the morning; then show the private workout entries I logged over the last seven days; then set my locally tracked daily steps goal to 8000; then summarize my manually logged health entries from the last seven days? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `append_note`, `remember`, `read_log`, `set_fitness_goal`, `health_summary`.
**Ordering constraints:** `append_note` before `remember`; `remember` before `read_log`; `read_log` before `set_fitness_goal`; `set_fitness_goal` before `health_summary`.
**Checks:** title Route packing; append exact line; preserve body durable preference; not a timed reminder category workout; days 7; preserve timestamps name daily steps; target 8000; not wearable configuration days 7; only logged facts, not medical inference Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Unique existing Route packing note; backend cache may not refresh immediately
- Synthetic memory store; no real personal facts
- Three synthetic workout entries; one older entry excluded
- Synthetic goal store
- Synthetic health-tagged journal entries only

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0342 · Colloquial with interruptions

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Could you show a preview, then delete only saved facts containing Route old job; I authorize that exact scoped cleanup; then create a Notes note called Route groceries with Milk and Bread as separate checklist lines; then summarize my manually logged health entries from the last seven days; then append "Bring a spare cable." to my existing Route packing note; then show the private workout entries I logged over the last seven days? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `clear_memory`, `create_note`, `health_summary`, `append_note`, `read_log`.
**Ordering constraints:** `clear_memory` before `create_note`; `create_note` before `health_summary`; `health_summary` before `append_note`; `append_note` before `read_log`.
**Checks:** query Route old job; preview then confirm true; preserve other facts title Route groceries; checklist true; disclose bullet-list limitation if relevant days 7; only logged facts, not medical inference title Route packing; append exact line; preserve body category workout; days 7; preserve timestamps Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Two synthetic memories match; no real memory store; variant-specific state must satisfy: query Route old job; preview then confirm true; preserve other facts
- No existing Route ideas note; native Notes operation is intercepted; variant-specific state must satisfy: title Route groceries; checklist true; disclose bullet-list limitation if relevant
- Synthetic health-tagged journal entries only
- Unique existing Route packing note; backend cache may not refresh immediately
- Three synthetic workout entries; one older entry excluded

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0343 · Colloquial with interruptions

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Could you show a preview, then delete only saved facts containing Route old job; I authorize that exact scoped cleanup; then tell me what you remember about my Route bicycle; then create a Notes note called Route groceries with Milk and Bread as separate checklist lines; then remember that I prefer Route project meetings in the morning; then show the private workout entries I logged over the last seven days? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `clear_memory`, `recall`, `create_note`, `remember`, `read_log`.
**Ordering constraints:** `clear_memory` before `recall`; `recall` before `create_note`; `create_note` before `remember`; `remember` before `read_log`.
**Checks:** query Route old job; preview then confirm true; preserve other facts query Route bicycle; saved facts, not Messages title Route groceries; checklist true; disclose bullet-list limitation if relevant durable preference; not a timed reminder category workout; days 7; preserve timestamps Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Two synthetic memories match; no real memory store; variant-specific state must satisfy: query Route old job; preview then confirm true; preserve other facts
- Saved fact: Route bicycle is blue; transcript contains a conflicting old statement
- No existing Route ideas note; native Notes operation is intercepted; variant-specific state must satisfy: title Route groceries; checklist true; disclose bullet-list limitation if relevant
- Synthetic memory store; no real personal facts
- Three synthetic workout entries; one older entry excluded

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0344 · Colloquial with interruptions

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Could you show a preview, then delete only saved facts containing Route old job; I authorize that exact scoped cleanup; then open Notes' document scanner so I can scan the Route receipt; then remember that I prefer Route project meetings in the morning; then append "Bring a spare cable." to my existing Route packing note; then set my locally tracked daily steps goal to 8000? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `clear_memory`, `scan_to_note`, `remember`, `append_note`, `set_fitness_goal`.
**Ordering constraints:** `clear_memory` before `scan_to_note`; `scan_to_note` before `remember`; `remember` before `append_note`; `append_note` before `set_fitness_goal`.
**Checks:** query Route old job; preview then confirm true; preserve other facts manual scanner handoff, not OCR or completed scan durable preference; not a timed reminder title Route packing; append exact line; preserve body name daily steps; target 8000; not wearable configuration Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Two synthetic memories match; no real memory store; variant-specific state must satisfy: query Route old job; preview then confirm true; preserve other facts
- Synthetic native bridge represents paired device and accessibility; no real UI action
- Synthetic memory store; no real personal facts
- Unique existing Route packing note; backend cache may not refresh immediately
- Synthetic goal store

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0345 · Colloquial with interruptions

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Could you forget the single saved fact that my old Route locker is number 12; then remember that I prefer Route project meetings in the morning; then open Notes' document scanner so I can scan the Route receipt; then create a Notes note called Route groceries with Milk and Bread as separate checklist lines; then append "Bring a spare cable." to my existing Route packing note? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `forget`, `remember`, `scan_to_note`, `create_note`, `append_note`.
**Ordering constraints:** `forget` before `remember`; `remember` before `scan_to_note`; `scan_to_note` before `create_note`; `create_note` before `append_note`.
**Checks:** precise single fact; preserve other memories durable preference; not a timed reminder manual scanner handoff, not OCR or completed scan title Route groceries; checklist true; disclose bullet-list limitation if relevant title Route packing; append exact line; preserve body Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- One exact old locker fact plus unrelated facts
- Synthetic memory store; no real personal facts
- Synthetic native bridge represents paired device and accessibility; no real UI action
- No existing Route ideas note; native Notes operation is intercepted; variant-specific state must satisfy: title Route groceries; checklist true; disclose bullet-list limitation if relevant
- Unique existing Route packing note; backend cache may not refresh immediately

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0346 · Colloquial with interruptions

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Could you show the private workout entries I logged over the last seven days; then summarize my manually logged health entries from the last seven days; then show a preview, then delete only saved facts containing Route old job; I authorize that exact scoped cleanup; then forget the single saved fact that my old Route locker is number 12; then set my locally tracked daily steps goal to 8000? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `read_log`, `health_summary`, `clear_memory`, `forget`, `set_fitness_goal`.
**Ordering constraints:** `read_log` before `health_summary`; `health_summary` before `clear_memory`; `clear_memory` before `forget`; `forget` before `set_fitness_goal`.
**Checks:** category workout; days 7; preserve timestamps days 7; only logged facts, not medical inference query Route old job; preview then confirm true; preserve other facts precise single fact; preserve other memories name daily steps; target 8000; not wearable configuration Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Three synthetic workout entries; one older entry excluded
- Synthetic health-tagged journal entries only
- Two synthetic memories match; no real memory store; variant-specific state must satisfy: query Route old job; preview then confirm true; preserve other facts
- One exact old locker fact plus unrelated facts
- Synthetic goal store

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0347 · Colloquial with interruptions

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Could you remember that I prefer Route project meetings in the morning; then open Notes' document scanner so I can scan the Route receipt; then summarize my manually logged health entries from the last seven days; then forget the single saved fact that my old Route locker is number 12; then log "Walked 3 miles." in my private workout journal? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `remember`, `scan_to_note`, `health_summary`, `forget`, `log_entry`.
**Ordering constraints:** `remember` before `scan_to_note`; `scan_to_note` before `health_summary`; `health_summary` before `forget`; `forget` before `log_entry`.
**Checks:** durable preference; not a timed reminder manual scanner handoff, not OCR or completed scan days 7; only logged facts, not medical inference precise single fact; preserve other memories category workout; text exact; not Apple Health Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic memory store; no real personal facts
- Synthetic native bridge represents paired device and accessibility; no real UI action
- Synthetic health-tagged journal entries only
- One exact old locker fact plus unrelated facts
- Synthetic local journal database

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0348 · Colloquial with interruptions

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Could you open Notes' document scanner so I can scan the Route receipt; then log "Walked 3 miles." in my private workout journal; then forget the single saved fact that my old Route locker is number 12; then remember that I prefer Route project meetings in the morning; then summarize my manually logged health entries from the last seven days? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `scan_to_note`, `log_entry`, `forget`, `remember`, `health_summary`.
**Ordering constraints:** `scan_to_note` before `log_entry`; `log_entry` before `forget`; `forget` before `remember`; `remember` before `health_summary`.
**Checks:** manual scanner handoff, not OCR or completed scan category workout; text exact; not Apple Health precise single fact; preserve other memories durable preference; not a timed reminder days 7; only logged facts, not medical inference Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic native bridge represents paired device and accessibility; no real UI action
- Synthetic local journal database
- One exact old locker fact plus unrelated facts
- Synthetic memory store; no real personal facts
- Synthetic health-tagged journal entries only

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0349 · Colloquial with interruptions

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Could you open Notes' document scanner so I can scan the Route receipt; then remember that I prefer Route project meetings in the morning; then log "Walked 3 miles." in my private workout journal; then create a Notes note titled Route ideas containing "Test the pickup workflow."; then set my locally tracked daily steps goal to 8000? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `scan_to_note`, `remember`, `log_entry`, `create_note`, `set_fitness_goal`.
**Ordering constraints:** `scan_to_note` before `remember`; `remember` before `log_entry`; `log_entry` before `create_note`; `create_note` before `set_fitness_goal`.
**Checks:** manual scanner handoff, not OCR or completed scan durable preference; not a timed reminder category workout; text exact; not Apple Health title/body exact; actual Notes destination name daily steps; target 8000; not wearable configuration Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic native bridge represents paired device and accessibility; no real UI action
- Synthetic memory store; no real personal facts
- Synthetic local journal database
- No existing Route ideas note; native Notes operation is intercepted
- Synthetic goal store

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0350 · Colloquial with interruptions

**Focus:** Choose the correct persistence destination and preserve existing content.

**Prompt:**

Could you set my locally tracked daily steps goal to 8000; then open Notes' document scanner so I can scan the Route receipt; then forget the single saved fact that my old Route locker is number 12; then remember that I prefer Route project meetings in the morning; then show the private workout entries I logged over the last seven days? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `set_fitness_goal`, `scan_to_note`, `forget`, `remember`, `read_log`.
**Ordering constraints:** `set_fitness_goal` before `scan_to_note`; `scan_to_note` before `forget`; `forget` before `remember`; `remember` before `read_log`.
**Checks:** name daily steps; target 8000; not wearable configuration manual scanner handoff, not OCR or completed scan precise single fact; preserve other memories durable preference; not a timed reminder category workout; days 7; preserve timestamps Choose the correct persistence destination and preserve existing content.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic goal store
- Synthetic native bridge represents paired device and accessibility; no real UI action
- One exact old locker fact plus unrelated facts
- Synthetic memory store; no real personal facts
- Three synthetic workout entries; one older entry excluded

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.
