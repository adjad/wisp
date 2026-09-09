# Review only — no tests run

## 03. Calendar and reminder boundaries

Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

### WRS-0101 · Explicit sequence

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Please do these in this order: check my Personal calendar for the next seven days; then remove the overdue reminders and past Calendar events with Route expired in their titles; then set a one-time alarm for 6:45 PM labeled Route evening; then open the stored call link for Route standup; then bring the text Route review checklist back to my attention in 25 minutes.

**Required tools:** `get_upcoming`, `clear_past_reminders`, `set_alarm`, `join_video_call`, `schedule_task`.
**Ordering constraints:** `get_upcoming` before `clear_past_reminders`; `clear_past_reminders` before `set_alarm`; `set_alarm` before `join_video_call`; `join_video_call` before `schedule_task`.
**Checks:** account Personal; days 7 query Route expired; past-due only; preserve future items time 18:45; repeat_daily false match title Route standup; use stored URL, never invent one text Route review checklist; minutes_from_now 25; notify only Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Past reminder/event and future event share Route expired prefix
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- Synthetic notification scheduler; no arbitrary future tool execution

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0102 · Explicit sequence

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Please do these in this order: check my Personal calendar for the next seven days; then delete all active Route cleanup reminders and keep reminders with other titles; then cancel only the Calendar event titled Route obsolete lunch; then remove the overdue reminders and past Calendar events with Route expired in their titles; then add Route planning to my Calendar tomorrow at 3 PM for 45 minutes in Room 4.

**Required tools:** `get_upcoming`, `clear_reminders`, `cancel_event`, `clear_past_reminders`, `add_calendar_event`.
**Ordering constraints:** `get_upcoming` before `clear_reminders`; `clear_reminders` before `cancel_event`; `cancel_event` before `clear_past_reminders`; `clear_past_reminders` before `add_calendar_event`.
**Checks:** account Personal; days 7 scope all; query Route cleanup; preserve other reminders exact named event; do not complete or clear other items query Route expired; past-due only; preserve future items real calendar event; tomorrow 15:00; duration_min 45; location Room 4 Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Two Route cleanup reminders today, one tomorrow, and one similarly named Calendar event; variant-specific state must satisfy: scope all; query Route cleanup; preserve other reminders
- One active Calendar event Route obsolete lunch and an unrelated reminder exist
- Past reminder/event and future event share Route expired prefix
- No existing event named Route planning; native event write is intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0103 · Explicit sequence

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Please do these in this order: check my Personal calendar for the next seven days; then find a 45-minute free slot tomorrow between 10 AM and 5 PM; then open the stored call link for Route standup; then reschedule Route dentist to tomorrow at 4 PM, keeping its full title, 30-minute duration, and Clinic 2 location; then remove the overdue reminders and past Calendar events with Route expired in their titles.

**Required tools:** `get_upcoming`, `find_free_time`, `join_video_call`, `update_event`, `clear_past_reminders`.
**Ordering constraints:** `get_upcoming` before `find_free_time`; `find_free_time` before `join_video_call`; `join_video_call` before `update_event`; `update_event` before `clear_past_reminders`.
**Checks:** account Personal; days 7 minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions match title Route standup; use stored URL, never invent one match/full title Route dentist; tomorrow 16:00; duration_min 30; location Clinic 2 query Route expired; past-due only; preserve future items Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- One Calendar event Route dentist; 30 minutes; Clinic 2; no attendees/recurrence
- Past reminder/event and future event share Route expired prefix

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0104 · Explicit sequence

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Please do these in this order: check my Personal calendar for the next seven days; then open the stored call link for Route standup; then delete all active Route cleanup reminders and keep reminders with other titles; then set a one-time alarm for 6:45 PM labeled Route evening; then add Route planning to my Calendar tomorrow at 3 PM for 45 minutes in Room 4.

**Required tools:** `get_upcoming`, `join_video_call`, `clear_reminders`, `set_alarm`, `add_calendar_event`.
**Ordering constraints:** `get_upcoming` before `join_video_call`; `join_video_call` before `clear_reminders`; `clear_reminders` before `set_alarm`; `set_alarm` before `add_calendar_event`.
**Checks:** account Personal; days 7 match title Route standup; use stored URL, never invent one scope all; query Route cleanup; preserve other reminders time 18:45; repeat_daily false real calendar event; tomorrow 15:00; duration_min 45; location Room 4 Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- Two Route cleanup reminders today, one tomorrow, and one similarly named Calendar event; variant-specific state must satisfy: scope all; query Route cleanup; preserve other reminders
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false
- No existing event named Route planning; native event write is intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0105 · Explicit sequence

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Please do these in this order: check my Personal calendar for the next seven days; then set a one-time alarm for 6:45 PM labeled Route evening; then cancel only the Calendar event titled Route obsolete lunch; then bring the text Route review checklist back to my attention in 25 minutes; then reschedule Route dentist to tomorrow at 4 PM, keeping its full title, 30-minute duration, and Clinic 2 location.

**Required tools:** `get_upcoming`, `set_alarm`, `cancel_event`, `schedule_task`, `update_event`.
**Ordering constraints:** `get_upcoming` before `set_alarm`; `set_alarm` before `cancel_event`; `cancel_event` before `schedule_task`; `schedule_task` before `update_event`.
**Checks:** account Personal; days 7 time 18:45; repeat_daily false exact named event; do not complete or clear other items text Route review checklist; minutes_from_now 25; notify only match/full title Route dentist; tomorrow 16:00; duration_min 30; location Clinic 2 Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false
- One active Calendar event Route obsolete lunch and an unrelated reminder exist
- Synthetic notification scheduler; no arbitrary future tool execution
- One Calendar event Route dentist; 30 minutes; Clinic 2; no attendees/recurrence

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0106 · Explicit sequence

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Please do these in this order: check my Personal calendar for the next seven days; then set a one-time alarm for 6:45 PM labeled Route evening; then reschedule Route dentist to tomorrow at 4 PM, keeping its full title, 30-minute duration, and Clinic 2 location; then remind me tomorrow at 9 AM to return the Route sample; then start a 90-second countdown named Route tea.

**Required tools:** `get_upcoming`, `set_alarm`, `update_event`, `add_reminder`, `set_timer`.
**Ordering constraints:** `get_upcoming` before `set_alarm`; `set_alarm` before `update_event`; `update_event` before `add_reminder`; `add_reminder` before `set_timer`.
**Checks:** account Personal; days 7 time 18:45; repeat_daily false match/full title Route dentist; tomorrow 16:00; duration_min 30; location Clinic 2 local tomorrow 09:00; title Return the Route sample; no email or calendar event duration 90s; label Route tea Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false
- One Calendar event Route dentist; 30 minutes; Clinic 2; no attendees/recurrence
- Tomorrow 09:00 is future; native mirror request is intercepted
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0107 · Explicit sequence

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Please do these in this order: check my Personal calendar for the next seven days; then start a 90-second countdown named Route tea; then mark the Route dentist booking task done, without deleting it; then rename my Route rent reminder to Route rent payment and keep its known tomorrow 10 AM due time; then bring the text Route review checklist back to my attention in 25 minutes.

**Required tools:** `get_upcoming`, `set_timer`, `complete_reminder`, `update_reminder`, `schedule_task`.
**Ordering constraints:** `get_upcoming` before `set_timer`; `set_timer` before `complete_reminder`; `complete_reminder` before `update_reminder`; `update_reminder` before `schedule_task`.
**Checks:** account Personal; days 7 duration 90s; label Route tea title Route dentist booking; done status; no cancel_event title Route rent; new_title Route rent payment; preserve timestamp text Route review checklist; minutes_from_now 25; notify only Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea
- One active future reminder Route dentist booking; preserve historical record
- One active Route rent reminder exists; a similarly named Calendar event must remain unchanged; variant-specific state must satisfy: title Route rent; new_title Route rent payment; preserve timestamp
- Synthetic notification scheduler; no arbitrary future tool execution

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0108 · Explicit sequence

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Please do these in this order: check my Work calendar for tomorrow and list the reminders separately; then start a ten-minute timer labeled Route pasta; then move my existing Route rent reminder to tomorrow at 10 AM without creating another one; then remove the overdue reminders and past Calendar events with Route expired in their titles; then delete only today's reminders with Route cleanup in the title, leaving every Calendar event alone.

**Required tools:** `get_upcoming`, `set_timer`, `update_reminder`, `clear_past_reminders`, `clear_reminders`.
**Ordering constraints:** `get_upcoming` before `set_timer`; `set_timer` before `update_reminder`; `update_reminder` before `clear_past_reminders`; `clear_past_reminders` before `clear_reminders`.
**Checks:** account Work; include tomorrow; distinguish events from reminders duration 10 minutes; label Route pasta; not add_reminder match Route rent; update existing ID; preserve title query Route expired; past-due only; preserve future items scope today; query Route cleanup; exclude calendar Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder
- Synthetic timer daemon with notifications intercepted
- One active Route rent reminder exists; a similarly named Calendar event must remain unchanged
- Past reminder/event and future event share Route expired prefix
- Two Route cleanup reminders today, one tomorrow, and one similarly named Calendar event

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0109 · Explicit sequence

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Please do these in this order: check my Personal calendar for the next seven days; then reschedule Route dentist to tomorrow at 4 PM, keeping its full title, 30-minute duration, and Clinic 2 location; then remind me tomorrow at 9 AM to return the Route sample; then open the stored call link for Route standup; then add Route planning to my Calendar tomorrow at 3 PM for 45 minutes in Room 4.

**Required tools:** `get_upcoming`, `update_event`, `add_reminder`, `join_video_call`, `add_calendar_event`.
**Ordering constraints:** `get_upcoming` before `update_event`; `update_event` before `add_reminder`; `add_reminder` before `join_video_call`; `join_video_call` before `add_calendar_event`.
**Checks:** account Personal; days 7 match/full title Route dentist; tomorrow 16:00; duration_min 30; location Clinic 2 local tomorrow 09:00; title Return the Route sample; no email or calendar event match title Route standup; use stored URL, never invent one real calendar event; tomorrow 15:00; duration_min 45; location Room 4 Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- One Calendar event Route dentist; 30 minutes; Clinic 2; no attendees/recurrence
- Tomorrow 09:00 is future; native mirror request is intercepted
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- No existing event named Route planning; native event write is intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0110 · Explicit sequence

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Please do these in this order: check my Personal calendar for the next seven days; then reschedule Route dentist to tomorrow at 4 PM, keeping its full title, 30-minute duration, and Clinic 2 location; then open the stored call link for Route standup; then remove the overdue reminders and past Calendar events with Route expired in their titles; then set a one-time alarm for 6:45 PM labeled Route evening.

**Required tools:** `get_upcoming`, `update_event`, `join_video_call`, `clear_past_reminders`, `set_alarm`.
**Ordering constraints:** `get_upcoming` before `update_event`; `update_event` before `join_video_call`; `join_video_call` before `clear_past_reminders`; `clear_past_reminders` before `set_alarm`.
**Checks:** account Personal; days 7 match/full title Route dentist; tomorrow 16:00; duration_min 30; location Clinic 2 match title Route standup; use stored URL, never invent one query Route expired; past-due only; preserve future items time 18:45; repeat_daily false Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- One Calendar event Route dentist; 30 minutes; Clinic 2; no attendees/recurrence
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- Past reminder/event and future event share Route expired prefix
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0111 · Natural compound request

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

I have a few things to finish. Check my Personal calendar for the next seven days. Remind me tomorrow at 9 AM to return the Route sample. Bring the text Route review checklist back to my attention in 25 minutes. Set a one-time alarm for 6:45 PM labeled Route evening. Find a 45-minute free slot tomorrow between 10 AM and 5 PM. Keep the results separate so I can tell what came from where.

**Required tools:** `get_upcoming`, `add_reminder`, `schedule_task`, `set_alarm`, `find_free_time`.
**Checks:** account Personal; days 7 local tomorrow 09:00; title Return the Route sample; no email or calendar event text Route review checklist; minutes_from_now 25; notify only time 18:45; repeat_daily false minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Tomorrow 09:00 is future; native mirror request is intercepted
- Synthetic notification scheduler; no arbitrary future tool execution
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0112 · Natural compound request

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

I have a few things to finish. Check my Personal calendar for the next seven days. Remove the overdue reminders and past Calendar events with Route expired in their titles. Bring the text Route review checklist back to my attention in 25 minutes. Delete only the overdue Route cleanup reminders, never Calendar events. Mark the Route dentist booking task done, without deleting it. Keep the results separate so I can tell what came from where.

**Required tools:** `get_upcoming`, `clear_past_reminders`, `schedule_task`, `clear_reminders`, `complete_reminder`.
**Checks:** account Personal; days 7 query Route expired; past-due only; preserve future items text Route review checklist; minutes_from_now 25; notify only scope past_due; query Route cleanup title Route dentist booking; done status; no cancel_event Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Past reminder/event and future event share Route expired prefix
- Synthetic notification scheduler; no arbitrary future tool execution
- Two Route cleanup reminders today, one tomorrow, and one similarly named Calendar event; variant-specific state must satisfy: scope past_due; query Route cleanup
- One active future reminder Route dentist booking; preserve historical record

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0113 · Natural compound request

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

I have a few things to finish. Check my Personal calendar for the next seven days. Find a 45-minute free slot tomorrow between 10 AM and 5 PM. Delete all active Route cleanup reminders and keep reminders with other titles. Mark the Route dentist booking task done, without deleting it. Bring the text Route review checklist back to my attention in 25 minutes. Keep the results separate so I can tell what came from where.

**Required tools:** `get_upcoming`, `find_free_time`, `clear_reminders`, `complete_reminder`, `schedule_task`.
**Checks:** account Personal; days 7 minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions scope all; query Route cleanup; preserve other reminders title Route dentist booking; done status; no cancel_event text Route review checklist; minutes_from_now 25; notify only Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow
- Two Route cleanup reminders today, one tomorrow, and one similarly named Calendar event; variant-specific state must satisfy: scope all; query Route cleanup; preserve other reminders
- One active future reminder Route dentist booking; preserve historical record
- Synthetic notification scheduler; no arbitrary future tool execution

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0114 · Natural compound request

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

I have a few things to finish. Check my Personal calendar for the next seven days. Find a 45-minute free slot tomorrow between 10 AM and 5 PM. Mark the Route dentist booking task done, without deleting it. Remind me tomorrow at 9 AM to return the Route sample. Start a 90-second countdown named Route tea. Keep the results separate so I can tell what came from where.

**Required tools:** `get_upcoming`, `find_free_time`, `complete_reminder`, `add_reminder`, `set_timer`.
**Checks:** account Personal; days 7 minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions title Route dentist booking; done status; no cancel_event local tomorrow 09:00; title Return the Route sample; no email or calendar event duration 90s; label Route tea Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow
- One active future reminder Route dentist booking; preserve historical record
- Tomorrow 09:00 is future; native mirror request is intercepted
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0115 · Natural compound request

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

I have a few things to finish. Check my Personal calendar for the next seven days. Find a 45-minute free slot tomorrow between 10 AM and 5 PM. Bring the text Route review checklist back to my attention in 25 minutes. Open the stored call link for Route standup. Reschedule Route dentist to tomorrow at 4 PM, keeping its full title, 30-minute duration, and Clinic 2 location. Keep the results separate so I can tell what came from where.

**Required tools:** `get_upcoming`, `find_free_time`, `schedule_task`, `join_video_call`, `update_event`.
**Checks:** account Personal; days 7 minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions text Route review checklist; minutes_from_now 25; notify only match title Route standup; use stored URL, never invent one match/full title Route dentist; tomorrow 16:00; duration_min 30; location Clinic 2 Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow
- Synthetic notification scheduler; no arbitrary future tool execution
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- One Calendar event Route dentist; 30 minutes; Clinic 2; no attendees/recurrence

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0116 · Natural compound request

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

I have a few things to finish. Check my Personal calendar for the next seven days. Find a 45-minute free slot tomorrow between 10 AM and 5 PM. Start a 90-second countdown named Route tea. Cancel only the Calendar event titled Route obsolete lunch. Mark the Route dentist booking task done, without deleting it. Keep the results separate so I can tell what came from where.

**Required tools:** `get_upcoming`, `find_free_time`, `set_timer`, `cancel_event`, `complete_reminder`.
**Checks:** account Personal; days 7 minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions duration 90s; label Route tea exact named event; do not complete or clear other items title Route dentist booking; done status; no cancel_event Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea
- One active Calendar event Route obsolete lunch and an unrelated reminder exist
- One active future reminder Route dentist booking; preserve historical record

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0117 · Natural compound request

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

I have a few things to finish. Check my Work calendar for tomorrow and list the reminders separately. Set a daily alarm for 7:30 AM labeled Route wake. Remind me tomorrow at 9 AM to return the Route sample. Bring the text Route review checklist back to my attention in 25 minutes. Reschedule Route dentist to tomorrow at 4 PM, keeping its full title, 30-minute duration, and Clinic 2 location. Keep the results separate so I can tell what came from where.

**Required tools:** `get_upcoming`, `set_alarm`, `add_reminder`, `schedule_task`, `update_event`.
**Checks:** account Work; include tomorrow; distinguish events from reminders time 7:30am; repeat_daily true; not a Calendar event local tomorrow 09:00; title Return the Route sample; no email or calendar event text Route review checklist; minutes_from_now 25; notify only match/full title Route dentist; tomorrow 16:00; duration_min 30; location Clinic 2 Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder
- Synthetic timer clock; nearest future occurrence; no real alarm armed
- Tomorrow 09:00 is future; native mirror request is intercepted
- Synthetic notification scheduler; no arbitrary future tool execution
- One Calendar event Route dentist; 30 minutes; Clinic 2; no attendees/recurrence

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0118 · Natural compound request

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

I have a few things to finish. Check my Personal calendar for the next seven days. Start a 90-second countdown named Route tea. Add Route planning to my Calendar tomorrow at 3 PM for 45 minutes in Room 4. Cancel only the Calendar event titled Route obsolete lunch. Remind me tomorrow at 9 AM to return the Route sample. Keep the results separate so I can tell what came from where.

**Required tools:** `get_upcoming`, `set_timer`, `add_calendar_event`, `cancel_event`, `add_reminder`.
**Checks:** account Personal; days 7 duration 90s; label Route tea real calendar event; tomorrow 15:00; duration_min 45; location Room 4 exact named event; do not complete or clear other items local tomorrow 09:00; title Return the Route sample; no email or calendar event Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea
- No existing event named Route planning; native event write is intercepted
- One active Calendar event Route obsolete lunch and an unrelated reminder exist
- Tomorrow 09:00 is future; native mirror request is intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0119 · Natural compound request

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

I have a few things to finish. Check my Personal calendar for the next seven days. Reschedule Route dentist to tomorrow at 4 PM, keeping its full title, 30-minute duration, and Clinic 2 location. Open the stored call link for Route standup. Remind me tomorrow at 9 AM to return the Route sample. Add Route planning to my Calendar tomorrow at 3 PM for 45 minutes in Room 4. Keep the results separate so I can tell what came from where.

**Required tools:** `get_upcoming`, `update_event`, `join_video_call`, `add_reminder`, `add_calendar_event`.
**Checks:** account Personal; days 7 match/full title Route dentist; tomorrow 16:00; duration_min 30; location Clinic 2 match title Route standup; use stored URL, never invent one local tomorrow 09:00; title Return the Route sample; no email or calendar event real calendar event; tomorrow 15:00; duration_min 45; location Room 4 Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- One Calendar event Route dentist; 30 minutes; Clinic 2; no attendees/recurrence
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- Tomorrow 09:00 is future; native mirror request is intercepted
- No existing event named Route planning; native event write is intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0120 · Natural compound request

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

I have a few things to finish. Check my Personal calendar for the next seven days. Reschedule Route dentist to tomorrow at 4 PM, keeping its full title, 30-minute duration, and Clinic 2 location. Bring the text Route review checklist back to my attention in 25 minutes. Mark the Route dentist booking task done, without deleting it. Delete only the overdue Route cleanup reminders, never Calendar events. Keep the results separate so I can tell what came from where.

**Required tools:** `get_upcoming`, `update_event`, `schedule_task`, `complete_reminder`, `clear_reminders`.
**Checks:** account Personal; days 7 match/full title Route dentist; tomorrow 16:00; duration_min 30; location Clinic 2 text Route review checklist; minutes_from_now 25; notify only title Route dentist booking; done status; no cancel_event scope past_due; query Route cleanup Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- One Calendar event Route dentist; 30 minutes; Clinic 2; no attendees/recurrence
- Synthetic notification scheduler; no arbitrary future tool execution
- One active future reminder Route dentist booking; preserve historical record
- Two Route cleanup reminders today, one tomorrow, and one similarly named Calendar event; variant-specific state must satisfy: scope past_due; query Route cleanup

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0121 · Scoped execution

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

For these tasks, use only the named sources and targets: check my Personal calendar for the next seven days; then add Route planning to my Calendar tomorrow at 3 PM for 45 minutes in Room 4; then find a 45-minute free slot tomorrow between 10 AM and 5 PM; then rename my Route rent reminder to Route rent payment and keep its known tomorrow 10 AM due time; then cancel only the Calendar event titled Route obsolete lunch. Leave everything else unchanged.

**Required tools:** `get_upcoming`, `add_calendar_event`, `find_free_time`, `update_reminder`, `cancel_event`.
**Ordering constraints:** `get_upcoming` before `add_calendar_event`; `add_calendar_event` before `find_free_time`; `find_free_time` before `update_reminder`; `update_reminder` before `cancel_event`.
**Checks:** account Personal; days 7 real calendar event; tomorrow 15:00; duration_min 45; location Room 4 minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions title Route rent; new_title Route rent payment; preserve timestamp exact named event; do not complete or clear other items Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- No existing event named Route planning; native event write is intercepted
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow
- One active Route rent reminder exists; a similarly named Calendar event must remain unchanged; variant-specific state must satisfy: title Route rent; new_title Route rent payment; preserve timestamp
- One active Calendar event Route obsolete lunch and an unrelated reminder exist

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0122 · Scoped execution

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

For these tasks, use only the named sources and targets: check my Personal calendar for the next seven days; then cancel only the Calendar event titled Route obsolete lunch; then start a 90-second countdown named Route tea; then remove the overdue reminders and past Calendar events with Route expired in their titles; then remind me tomorrow at 9 AM to return the Route sample. Leave everything else unchanged.

**Required tools:** `get_upcoming`, `cancel_event`, `set_timer`, `clear_past_reminders`, `add_reminder`.
**Ordering constraints:** `get_upcoming` before `cancel_event`; `cancel_event` before `set_timer`; `set_timer` before `clear_past_reminders`; `clear_past_reminders` before `add_reminder`.
**Checks:** account Personal; days 7 exact named event; do not complete or clear other items duration 90s; label Route tea query Route expired; past-due only; preserve future items local tomorrow 09:00; title Return the Route sample; no email or calendar event Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- One active Calendar event Route obsolete lunch and an unrelated reminder exist
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea
- Past reminder/event and future event share Route expired prefix
- Tomorrow 09:00 is future; native mirror request is intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0123 · Scoped execution

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

For these tasks, use only the named sources and targets: check my Personal calendar for the next seven days; then cancel only the Calendar event titled Route obsolete lunch; then start a 90-second countdown named Route tea; then remove the overdue reminders and past Calendar events with Route expired in their titles; then find a 45-minute free slot tomorrow between 10 AM and 5 PM. Leave everything else unchanged.

**Required tools:** `get_upcoming`, `cancel_event`, `set_timer`, `clear_past_reminders`, `find_free_time`.
**Ordering constraints:** `get_upcoming` before `cancel_event`; `cancel_event` before `set_timer`; `set_timer` before `clear_past_reminders`; `clear_past_reminders` before `find_free_time`.
**Checks:** account Personal; days 7 exact named event; do not complete or clear other items duration 90s; label Route tea query Route expired; past-due only; preserve future items minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- One active Calendar event Route obsolete lunch and an unrelated reminder exist
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea
- Past reminder/event and future event share Route expired prefix
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0124 · Scoped execution

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

For these tasks, use only the named sources and targets: check my Personal calendar for the next seven days; then remove the overdue reminders and past Calendar events with Route expired in their titles; then add Route planning to my Calendar tomorrow at 3 PM for 45 minutes in Room 4; then reschedule Route dentist to tomorrow at 4 PM, keeping its full title, 30-minute duration, and Clinic 2 location; then open the stored call link for Route standup. Leave everything else unchanged.

**Required tools:** `get_upcoming`, `clear_past_reminders`, `add_calendar_event`, `update_event`, `join_video_call`.
**Ordering constraints:** `get_upcoming` before `clear_past_reminders`; `clear_past_reminders` before `add_calendar_event`; `add_calendar_event` before `update_event`; `update_event` before `join_video_call`.
**Checks:** account Personal; days 7 query Route expired; past-due only; preserve future items real calendar event; tomorrow 15:00; duration_min 45; location Room 4 match/full title Route dentist; tomorrow 16:00; duration_min 30; location Clinic 2 match title Route standup; use stored URL, never invent one Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Past reminder/event and future event share Route expired prefix
- No existing event named Route planning; native event write is intercepted
- One Calendar event Route dentist; 30 minutes; Clinic 2; no attendees/recurrence
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0125 · Scoped execution

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

For these tasks, use only the named sources and targets: check my Personal calendar for the next seven days; then remove the overdue reminders and past Calendar events with Route expired in their titles; then add Route planning to my Calendar tomorrow at 3 PM for 45 minutes in Room 4; then rename my Route rent reminder to Route rent payment and keep its known tomorrow 10 AM due time; then remind me tomorrow at 9 AM to return the Route sample. Leave everything else unchanged.

**Required tools:** `get_upcoming`, `clear_past_reminders`, `add_calendar_event`, `update_reminder`, `add_reminder`.
**Ordering constraints:** `get_upcoming` before `clear_past_reminders`; `clear_past_reminders` before `add_calendar_event`; `add_calendar_event` before `update_reminder`; `update_reminder` before `add_reminder`.
**Checks:** account Personal; days 7 query Route expired; past-due only; preserve future items real calendar event; tomorrow 15:00; duration_min 45; location Room 4 title Route rent; new_title Route rent payment; preserve timestamp local tomorrow 09:00; title Return the Route sample; no email or calendar event Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Past reminder/event and future event share Route expired prefix
- No existing event named Route planning; native event write is intercepted
- One active Route rent reminder exists; a similarly named Calendar event must remain unchanged; variant-specific state must satisfy: title Route rent; new_title Route rent payment; preserve timestamp
- Tomorrow 09:00 is future; native mirror request is intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0126 · Scoped execution

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

For these tasks, use only the named sources and targets: check my Personal calendar for the next seven days; then delete all active Route cleanup reminders and keep reminders with other titles; then remove the overdue reminders and past Calendar events with Route expired in their titles; then open the stored call link for Route standup; then find a 45-minute free slot tomorrow between 10 AM and 5 PM. Leave everything else unchanged.

**Required tools:** `get_upcoming`, `clear_reminders`, `clear_past_reminders`, `join_video_call`, `find_free_time`.
**Ordering constraints:** `get_upcoming` before `clear_reminders`; `clear_reminders` before `clear_past_reminders`; `clear_past_reminders` before `join_video_call`; `join_video_call` before `find_free_time`.
**Checks:** account Personal; days 7 scope all; query Route cleanup; preserve other reminders query Route expired; past-due only; preserve future items match title Route standup; use stored URL, never invent one minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Two Route cleanup reminders today, one tomorrow, and one similarly named Calendar event; variant-specific state must satisfy: scope all; query Route cleanup; preserve other reminders
- Past reminder/event and future event share Route expired prefix
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0127 · Scoped execution

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

For these tasks, use only the named sources and targets: check my Personal calendar for the next seven days; then delete only the overdue Route cleanup reminders, never Calendar events; then rename my Route rent reminder to Route rent payment and keep its known tomorrow 10 AM due time; then mark the Route dentist booking task done, without deleting it; then find a 45-minute free slot tomorrow between 10 AM and 5 PM. Leave everything else unchanged.

**Required tools:** `get_upcoming`, `clear_reminders`, `update_reminder`, `complete_reminder`, `find_free_time`.
**Ordering constraints:** `get_upcoming` before `clear_reminders`; `clear_reminders` before `update_reminder`; `update_reminder` before `complete_reminder`; `complete_reminder` before `find_free_time`.
**Checks:** account Personal; days 7 scope past_due; query Route cleanup title Route rent; new_title Route rent payment; preserve timestamp title Route dentist booking; done status; no cancel_event minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Two Route cleanup reminders today, one tomorrow, and one similarly named Calendar event; variant-specific state must satisfy: scope past_due; query Route cleanup
- One active Route rent reminder exists; a similarly named Calendar event must remain unchanged; variant-specific state must satisfy: title Route rent; new_title Route rent payment; preserve timestamp
- One active future reminder Route dentist booking; preserve historical record
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0128 · Scoped execution

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

For these tasks, use only the named sources and targets: check my Work calendar for tomorrow and list the reminders separately; then open the stored call link for Route standup; then mark the Route dentist booking task done, without deleting it; then find a 45-minute free slot tomorrow between 10 AM and 5 PM; then add Route planning to my Calendar tomorrow at 3 PM for 45 minutes in Room 4. Leave everything else unchanged.

**Required tools:** `get_upcoming`, `join_video_call`, `complete_reminder`, `find_free_time`, `add_calendar_event`.
**Ordering constraints:** `get_upcoming` before `join_video_call`; `join_video_call` before `complete_reminder`; `complete_reminder` before `find_free_time`; `find_free_time` before `add_calendar_event`.
**Checks:** account Work; include tomorrow; distinguish events from reminders match title Route standup; use stored URL, never invent one title Route dentist booking; done status; no cancel_event minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions real calendar event; tomorrow 15:00; duration_min 45; location Room 4 Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- One active future reminder Route dentist booking; preserve historical record
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow
- No existing event named Route planning; native event write is intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0129 · Scoped execution

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

For these tasks, use only the named sources and targets: check my Personal calendar for the next seven days; then bring the text Route review checklist back to my attention in 25 minutes; then rename my Route rent reminder to Route rent payment and keep its known tomorrow 10 AM due time; then remind me tomorrow at 9 AM to return the Route sample; then reschedule Route dentist to tomorrow at 4 PM, keeping its full title, 30-minute duration, and Clinic 2 location. Leave everything else unchanged.

**Required tools:** `get_upcoming`, `schedule_task`, `update_reminder`, `add_reminder`, `update_event`.
**Ordering constraints:** `get_upcoming` before `schedule_task`; `schedule_task` before `update_reminder`; `update_reminder` before `add_reminder`; `add_reminder` before `update_event`.
**Checks:** account Personal; days 7 text Route review checklist; minutes_from_now 25; notify only title Route rent; new_title Route rent payment; preserve timestamp local tomorrow 09:00; title Return the Route sample; no email or calendar event match/full title Route dentist; tomorrow 16:00; duration_min 30; location Clinic 2 Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Synthetic notification scheduler; no arbitrary future tool execution
- One active Route rent reminder exists; a similarly named Calendar event must remain unchanged; variant-specific state must satisfy: title Route rent; new_title Route rent payment; preserve timestamp
- Tomorrow 09:00 is future; native mirror request is intercepted
- One Calendar event Route dentist; 30 minutes; Clinic 2; no attendees/recurrence

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0130 · Scoped execution

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

For these tasks, use only the named sources and targets: check my Personal calendar for the next seven days; then start a 90-second countdown named Route tea; then add Route planning to my Calendar tomorrow at 3 PM for 45 minutes in Room 4; then open the stored call link for Route standup; then cancel only the Calendar event titled Route obsolete lunch. Leave everything else unchanged.

**Required tools:** `get_upcoming`, `set_timer`, `add_calendar_event`, `join_video_call`, `cancel_event`.
**Ordering constraints:** `get_upcoming` before `set_timer`; `set_timer` before `add_calendar_event`; `add_calendar_event` before `join_video_call`; `join_video_call` before `cancel_event`.
**Checks:** account Personal; days 7 duration 90s; label Route tea real calendar event; tomorrow 15:00; duration_min 45; location Room 4 match title Route standup; use stored URL, never invent one exact named event; do not complete or clear other items Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea
- No existing event named Route planning; native event write is intercepted
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- One active Calendar event Route obsolete lunch and an unrelated reminder exist

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0131 · Late constraints

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Check my Personal calendar for the next seven days. Add Route planning to my Calendar tomorrow at 3 PM for 45 minutes in Room 4. Mark the Route dentist booking task done, without deleting it. Cancel only the Calendar event titled Route obsolete lunch. Bring the text Route review checklist back to my attention in 25 minutes. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `get_upcoming`, `add_calendar_event`, `complete_reminder`, `cancel_event`, `schedule_task`.
**Checks:** account Personal; days 7 real calendar event; tomorrow 15:00; duration_min 45; location Room 4 title Route dentist booking; done status; no cancel_event exact named event; do not complete or clear other items text Route review checklist; minutes_from_now 25; notify only Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- No existing event named Route planning; native event write is intercepted
- One active future reminder Route dentist booking; preserve historical record
- One active Calendar event Route obsolete lunch and an unrelated reminder exist
- Synthetic notification scheduler; no arbitrary future tool execution

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0132 · Late constraints

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Check my Personal calendar for the next seven days. Add Route planning to my Calendar tomorrow at 3 PM for 45 minutes in Room 4. Find a 45-minute free slot tomorrow between 10 AM and 5 PM. Remind me tomorrow at 9 AM to return the Route sample. Set a one-time alarm for 6:45 PM labeled Route evening. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `get_upcoming`, `add_calendar_event`, `find_free_time`, `add_reminder`, `set_alarm`.
**Checks:** account Personal; days 7 real calendar event; tomorrow 15:00; duration_min 45; location Room 4 minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions local tomorrow 09:00; title Return the Route sample; no email or calendar event time 18:45; repeat_daily false Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- No existing event named Route planning; native event write is intercepted
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow
- Tomorrow 09:00 is future; native mirror request is intercepted
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0133 · Late constraints

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Check my Personal calendar for the next seven days. Add Route planning to my Calendar tomorrow at 3 PM for 45 minutes in Room 4. Rename my Route rent reminder to Route rent payment and keep its known tomorrow 10 AM due time. Remind me tomorrow at 9 AM to return the Route sample. Open the stored call link for Route standup. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `get_upcoming`, `add_calendar_event`, `update_reminder`, `add_reminder`, `join_video_call`.
**Checks:** account Personal; days 7 real calendar event; tomorrow 15:00; duration_min 45; location Room 4 title Route rent; new_title Route rent payment; preserve timestamp local tomorrow 09:00; title Return the Route sample; no email or calendar event match title Route standup; use stored URL, never invent one Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- No existing event named Route planning; native event write is intercepted
- One active Route rent reminder exists; a similarly named Calendar event must remain unchanged; variant-specific state must satisfy: title Route rent; new_title Route rent payment; preserve timestamp
- Tomorrow 09:00 is future; native mirror request is intercepted
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0134 · Late constraints

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Check my Personal calendar for the next seven days. Cancel only the Calendar event titled Route obsolete lunch. Delete only the overdue Route cleanup reminders, never Calendar events. Rename my Route rent reminder to Route rent payment and keep its known tomorrow 10 AM due time. Set a one-time alarm for 6:45 PM labeled Route evening. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `get_upcoming`, `cancel_event`, `clear_reminders`, `update_reminder`, `set_alarm`.
**Checks:** account Personal; days 7 exact named event; do not complete or clear other items scope past_due; query Route cleanup title Route rent; new_title Route rent payment; preserve timestamp time 18:45; repeat_daily false Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- One active Calendar event Route obsolete lunch and an unrelated reminder exist
- Two Route cleanup reminders today, one tomorrow, and one similarly named Calendar event; variant-specific state must satisfy: scope past_due; query Route cleanup
- One active Route rent reminder exists; a similarly named Calendar event must remain unchanged; variant-specific state must satisfy: title Route rent; new_title Route rent payment; preserve timestamp
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0135 · Late constraints

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Check my Personal calendar for the next seven days. Remove the overdue reminders and past Calendar events with Route expired in their titles. Delete only the overdue Route cleanup reminders, never Calendar events. Set a one-time alarm for 6:45 PM labeled Route evening. Rename my Route rent reminder to Route rent payment and keep its known tomorrow 10 AM due time. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `get_upcoming`, `clear_past_reminders`, `clear_reminders`, `set_alarm`, `update_reminder`.
**Checks:** account Personal; days 7 query Route expired; past-due only; preserve future items scope past_due; query Route cleanup time 18:45; repeat_daily false title Route rent; new_title Route rent payment; preserve timestamp Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Past reminder/event and future event share Route expired prefix
- Two Route cleanup reminders today, one tomorrow, and one similarly named Calendar event; variant-specific state must satisfy: scope past_due; query Route cleanup
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false
- One active Route rent reminder exists; a similarly named Calendar event must remain unchanged; variant-specific state must satisfy: title Route rent; new_title Route rent payment; preserve timestamp

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0136 · Late constraints

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Check my Personal calendar for the next seven days. Remove the overdue reminders and past Calendar events with Route expired in their titles. Mark the Route dentist booking task done, without deleting it. Start a 90-second countdown named Route tea. Bring the text Route review checklist back to my attention in 25 minutes. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `get_upcoming`, `clear_past_reminders`, `complete_reminder`, `set_timer`, `schedule_task`.
**Checks:** account Personal; days 7 query Route expired; past-due only; preserve future items title Route dentist booking; done status; no cancel_event duration 90s; label Route tea text Route review checklist; minutes_from_now 25; notify only Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Past reminder/event and future event share Route expired prefix
- One active future reminder Route dentist booking; preserve historical record
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea
- Synthetic notification scheduler; no arbitrary future tool execution

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0137 · Late constraints

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Check my Personal calendar for the next seven days. Open the stored call link for Route standup. Set a one-time alarm for 6:45 PM labeled Route evening. Mark the Route dentist booking task done, without deleting it. Cancel only the Calendar event titled Route obsolete lunch. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `get_upcoming`, `join_video_call`, `set_alarm`, `complete_reminder`, `cancel_event`.
**Checks:** account Personal; days 7 match title Route standup; use stored URL, never invent one time 18:45; repeat_daily false title Route dentist booking; done status; no cancel_event exact named event; do not complete or clear other items Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false
- One active future reminder Route dentist booking; preserve historical record
- One active Calendar event Route obsolete lunch and an unrelated reminder exist

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0138 · Late constraints

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Check my Personal calendar for the next seven days. Set a one-time alarm for 6:45 PM labeled Route evening. Cancel only the Calendar event titled Route obsolete lunch. Mark the Route dentist booking task done, without deleting it. Start a 90-second countdown named Route tea. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `get_upcoming`, `set_alarm`, `cancel_event`, `complete_reminder`, `set_timer`.
**Checks:** account Personal; days 7 time 18:45; repeat_daily false exact named event; do not complete or clear other items title Route dentist booking; done status; no cancel_event duration 90s; label Route tea Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false
- One active Calendar event Route obsolete lunch and an unrelated reminder exist
- One active future reminder Route dentist booking; preserve historical record
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0139 · Late constraints

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Check my Personal calendar for the next seven days. Set a one-time alarm for 6:45 PM labeled Route evening. Delete all active Route cleanup reminders and keep reminders with other titles. Start a 90-second countdown named Route tea. Add Route planning to my Calendar tomorrow at 3 PM for 45 minutes in Room 4. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `get_upcoming`, `set_alarm`, `clear_reminders`, `set_timer`, `add_calendar_event`.
**Checks:** account Personal; days 7 time 18:45; repeat_daily false scope all; query Route cleanup; preserve other reminders duration 90s; label Route tea real calendar event; tomorrow 15:00; duration_min 45; location Room 4 Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false
- Two Route cleanup reminders today, one tomorrow, and one similarly named Calendar event; variant-specific state must satisfy: scope all; query Route cleanup; preserve other reminders
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea
- No existing event named Route planning; native event write is intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0140 · Late constraints

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Check my Work calendar for tomorrow and list the reminders separately. Set a daily alarm for 7:30 AM labeled Route wake. Find a 45-minute free slot tomorrow between 10 AM and 5 PM. Cancel only the Calendar event titled Route obsolete lunch. Delete only today's reminders with Route cleanup in the title, leaving every Calendar event alone. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `get_upcoming`, `set_alarm`, `find_free_time`, `cancel_event`, `clear_reminders`.
**Checks:** account Work; include tomorrow; distinguish events from reminders time 7:30am; repeat_daily true; not a Calendar event minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions exact named event; do not complete or clear other items scope today; query Route cleanup; exclude calendar Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder
- Synthetic timer clock; nearest future occurrence; no real alarm armed
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow
- One active Calendar event Route obsolete lunch and an unrelated reminder exist
- Two Route cleanup reminders today, one tomorrow, and one similarly named Calendar event

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0141 · Colloquial with interruptions

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Could you check my Personal calendar for the next seven days; then add Route planning to my Calendar tomorrow at 3 PM for 45 minutes in Room 4; then remind me tomorrow at 9 AM to return the Route sample; then rename my Route rent reminder to Route rent payment and keep its known tomorrow 10 AM due time; then delete all active Route cleanup reminders and keep reminders with other titles? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `get_upcoming`, `add_calendar_event`, `add_reminder`, `update_reminder`, `clear_reminders`.
**Ordering constraints:** `get_upcoming` before `add_calendar_event`; `add_calendar_event` before `add_reminder`; `add_reminder` before `update_reminder`; `update_reminder` before `clear_reminders`.
**Checks:** account Personal; days 7 real calendar event; tomorrow 15:00; duration_min 45; location Room 4 local tomorrow 09:00; title Return the Route sample; no email or calendar event title Route rent; new_title Route rent payment; preserve timestamp scope all; query Route cleanup; preserve other reminders Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- No existing event named Route planning; native event write is intercepted
- Tomorrow 09:00 is future; native mirror request is intercepted
- One active Route rent reminder exists; a similarly named Calendar event must remain unchanged; variant-specific state must satisfy: title Route rent; new_title Route rent payment; preserve timestamp
- Two Route cleanup reminders today, one tomorrow, and one similarly named Calendar event; variant-specific state must satisfy: scope all; query Route cleanup; preserve other reminders

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0142 · Colloquial with interruptions

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Could you check my Personal calendar for the next seven days; then remind me tomorrow at 9 AM to return the Route sample; then rename my Route rent reminder to Route rent payment and keep its known tomorrow 10 AM due time; then bring the text Route review checklist back to my attention in 25 minutes; then mark the Route dentist booking task done, without deleting it? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `get_upcoming`, `add_reminder`, `update_reminder`, `schedule_task`, `complete_reminder`.
**Ordering constraints:** `get_upcoming` before `add_reminder`; `add_reminder` before `update_reminder`; `update_reminder` before `schedule_task`; `schedule_task` before `complete_reminder`.
**Checks:** account Personal; days 7 local tomorrow 09:00; title Return the Route sample; no email or calendar event title Route rent; new_title Route rent payment; preserve timestamp text Route review checklist; minutes_from_now 25; notify only title Route dentist booking; done status; no cancel_event Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Tomorrow 09:00 is future; native mirror request is intercepted
- One active Route rent reminder exists; a similarly named Calendar event must remain unchanged; variant-specific state must satisfy: title Route rent; new_title Route rent payment; preserve timestamp
- Synthetic notification scheduler; no arbitrary future tool execution
- One active future reminder Route dentist booking; preserve historical record

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0143 · Colloquial with interruptions

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Could you check my Work calendar for tomorrow and list the reminders separately; then cancel only the Calendar event titled Route obsolete lunch; then bring the text Route review checklist back to my attention in 25 minutes; then start a ten-minute timer labeled Route pasta; then move my existing Route rent reminder to tomorrow at 10 AM without creating another one? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `get_upcoming`, `cancel_event`, `schedule_task`, `set_timer`, `update_reminder`.
**Ordering constraints:** `get_upcoming` before `cancel_event`; `cancel_event` before `schedule_task`; `schedule_task` before `set_timer`; `set_timer` before `update_reminder`.
**Checks:** account Work; include tomorrow; distinguish events from reminders exact named event; do not complete or clear other items text Route review checklist; minutes_from_now 25; notify only duration 10 minutes; label Route pasta; not add_reminder match Route rent; update existing ID; preserve title Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder
- One active Calendar event Route obsolete lunch and an unrelated reminder exist
- Synthetic notification scheduler; no arbitrary future tool execution
- Synthetic timer daemon with notifications intercepted
- One active Route rent reminder exists; a similarly named Calendar event must remain unchanged

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0144 · Colloquial with interruptions

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Could you check my Personal calendar for the next seven days; then delete all active Route cleanup reminders and keep reminders with other titles; then reschedule Route dentist to tomorrow at 4 PM, keeping its full title, 30-minute duration, and Clinic 2 location; then start a 90-second countdown named Route tea; then open the stored call link for Route standup? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `get_upcoming`, `clear_reminders`, `update_event`, `set_timer`, `join_video_call`.
**Ordering constraints:** `get_upcoming` before `clear_reminders`; `clear_reminders` before `update_event`; `update_event` before `set_timer`; `set_timer` before `join_video_call`.
**Checks:** account Personal; days 7 scope all; query Route cleanup; preserve other reminders match/full title Route dentist; tomorrow 16:00; duration_min 30; location Clinic 2 duration 90s; label Route tea match title Route standup; use stored URL, never invent one Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- Two Route cleanup reminders today, one tomorrow, and one similarly named Calendar event; variant-specific state must satisfy: scope all; query Route cleanup; preserve other reminders
- One Calendar event Route dentist; 30 minutes; Clinic 2; no attendees/recurrence
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0145 · Colloquial with interruptions

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Could you check my Personal calendar for the next seven days; then mark the Route dentist booking task done, without deleting it; then cancel only the Calendar event titled Route obsolete lunch; then remove the overdue reminders and past Calendar events with Route expired in their titles; then open the stored call link for Route standup? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `get_upcoming`, `complete_reminder`, `cancel_event`, `clear_past_reminders`, `join_video_call`.
**Ordering constraints:** `get_upcoming` before `complete_reminder`; `complete_reminder` before `cancel_event`; `cancel_event` before `clear_past_reminders`; `clear_past_reminders` before `join_video_call`.
**Checks:** account Personal; days 7 title Route dentist booking; done status; no cancel_event exact named event; do not complete or clear other items query Route expired; past-due only; preserve future items match title Route standup; use stored URL, never invent one Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- One active future reminder Route dentist booking; preserve historical record
- One active Calendar event Route obsolete lunch and an unrelated reminder exist
- Past reminder/event and future event share Route expired prefix
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0146 · Colloquial with interruptions

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Could you check my Personal calendar for the next seven days; then reschedule Route dentist to tomorrow at 4 PM, keeping its full title, 30-minute duration, and Clinic 2 location; then remove the overdue reminders and past Calendar events with Route expired in their titles; then find a 45-minute free slot tomorrow between 10 AM and 5 PM; then start a 90-second countdown named Route tea? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `get_upcoming`, `update_event`, `clear_past_reminders`, `find_free_time`, `set_timer`.
**Ordering constraints:** `get_upcoming` before `update_event`; `update_event` before `clear_past_reminders`; `clear_past_reminders` before `find_free_time`; `find_free_time` before `set_timer`.
**Checks:** account Personal; days 7 match/full title Route dentist; tomorrow 16:00; duration_min 30; location Clinic 2 query Route expired; past-due only; preserve future items minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions duration 90s; label Route tea Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- One Calendar event Route dentist; 30 minutes; Clinic 2; no attendees/recurrence
- Past reminder/event and future event share Route expired prefix
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0147 · Colloquial with interruptions

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Could you check my Personal calendar for the next seven days; then reschedule Route dentist to tomorrow at 4 PM, keeping its full title, 30-minute duration, and Clinic 2 location; then find a 45-minute free slot tomorrow between 10 AM and 5 PM; then mark the Route dentist booking task done, without deleting it; then rename my Route rent reminder to Route rent payment and keep its known tomorrow 10 AM due time? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `get_upcoming`, `update_event`, `find_free_time`, `complete_reminder`, `update_reminder`.
**Ordering constraints:** `get_upcoming` before `update_event`; `update_event` before `find_free_time`; `find_free_time` before `complete_reminder`; `complete_reminder` before `update_reminder`.
**Checks:** account Personal; days 7 match/full title Route dentist; tomorrow 16:00; duration_min 30; location Clinic 2 minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions title Route dentist booking; done status; no cancel_event title Route rent; new_title Route rent payment; preserve timestamp Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- One Calendar event Route dentist; 30 minutes; Clinic 2; no attendees/recurrence
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow
- One active future reminder Route dentist booking; preserve historical record
- One active Route rent reminder exists; a similarly named Calendar event must remain unchanged; variant-specific state must satisfy: title Route rent; new_title Route rent payment; preserve timestamp

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0148 · Colloquial with interruptions

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Could you check my Personal calendar for the next seven days; then reschedule Route dentist to tomorrow at 4 PM, keeping its full title, 30-minute duration, and Clinic 2 location; then bring the text Route review checklist back to my attention in 25 minutes; then set a one-time alarm for 6:45 PM labeled Route evening; then delete only the overdue Route cleanup reminders, never Calendar events? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `get_upcoming`, `update_event`, `schedule_task`, `set_alarm`, `clear_reminders`.
**Ordering constraints:** `get_upcoming` before `update_event`; `update_event` before `schedule_task`; `schedule_task` before `set_alarm`; `set_alarm` before `clear_reminders`.
**Checks:** account Personal; days 7 match/full title Route dentist; tomorrow 16:00; duration_min 30; location Clinic 2 text Route review checklist; minutes_from_now 25; notify only time 18:45; repeat_daily false scope past_due; query Route cleanup Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- One Calendar event Route dentist; 30 minutes; Clinic 2; no attendees/recurrence
- Synthetic notification scheduler; no arbitrary future tool execution
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false
- Two Route cleanup reminders today, one tomorrow, and one similarly named Calendar event; variant-specific state must satisfy: scope past_due; query Route cleanup

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0149 · Colloquial with interruptions

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Could you check my Personal calendar for the next seven days; then rename my Route rent reminder to Route rent payment and keep its known tomorrow 10 AM due time; then remind me tomorrow at 9 AM to return the Route sample; then delete all active Route cleanup reminders and keep reminders with other titles; then set a one-time alarm for 6:45 PM labeled Route evening? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `get_upcoming`, `update_reminder`, `add_reminder`, `clear_reminders`, `set_alarm`.
**Ordering constraints:** `get_upcoming` before `update_reminder`; `update_reminder` before `add_reminder`; `add_reminder` before `clear_reminders`; `clear_reminders` before `set_alarm`.
**Checks:** account Personal; days 7 title Route rent; new_title Route rent payment; preserve timestamp local tomorrow 09:00; title Return the Route sample; no email or calendar event scope all; query Route cleanup; preserve other reminders time 18:45; repeat_daily false Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- One active Route rent reminder exists; a similarly named Calendar event must remain unchanged; variant-specific state must satisfy: title Route rent; new_title Route rent payment; preserve timestamp
- Tomorrow 09:00 is future; native mirror request is intercepted
- Two Route cleanup reminders today, one tomorrow, and one similarly named Calendar event; variant-specific state must satisfy: scope all; query Route cleanup; preserve other reminders
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0150 · Colloquial with interruptions

**Focus:** Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.

**Prompt:**

Could you check my Personal calendar for the next seven days; then rename my Route rent reminder to Route rent payment and keep its known tomorrow 10 AM due time; then bring the text Route review checklist back to my attention in 25 minutes; then reschedule Route dentist to tomorrow at 4 PM, keeping its full title, 30-minute duration, and Clinic 2 location; then mark the Route dentist booking task done, without deleting it? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `get_upcoming`, `update_reminder`, `schedule_task`, `update_event`, `complete_reminder`.
**Ordering constraints:** `get_upcoming` before `update_reminder`; `update_reminder` before `schedule_task`; `schedule_task` before `update_event`; `update_event` before `complete_reminder`.
**Checks:** account Personal; days 7 title Route rent; new_title Route rent payment; preserve timestamp text Route review checklist; minutes_from_now 25; notify only match/full title Route dentist; tomorrow 16:00; duration_min 30; location Clinic 2 title Route dentist booking; done status; no cancel_event Keep appointments, reminders, completed tasks, timers, and bulk scopes distinct.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder; variant-specific state must satisfy: account Personal; days 7
- One active Route rent reminder exists; a similarly named Calendar event must remain unchanged; variant-specific state must satisfy: title Route rent; new_title Route rent payment; preserve timestamp
- Synthetic notification scheduler; no arbitrary future tool execution
- One Calendar event Route dentist; 30 minutes; Clinic 2; no attendees/recurrence
- One active future reminder Route dentist booking; preserve historical record

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.
