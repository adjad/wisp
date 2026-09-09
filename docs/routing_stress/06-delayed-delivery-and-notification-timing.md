# Review only — no tests run

## 06. Delayed delivery and notification timing

Keep scheduled sends separate from timers, clock alarms, and reminders.

### WRS-0251 · Explicit sequence

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Please do these in this order: cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries; then start a 90-second countdown named Route tea; then bring the text Route review checklist back to my attention in 25 minutes; then show how much time remains on my active timers and alarms; then remind me tomorrow at 9 AM to return the Route sample.

**Required tools:** `cancel_scheduled_send`, `set_timer`, `schedule_task`, `manage_timers`, `add_reminder`.
**Ordering constraints:** `cancel_scheduled_send` before `set_timer`; `set_timer` before `schedule_task`; `schedule_task` before `manage_timers`; `manage_timers` before `add_reminder`.
**Checks:** id route-old-ping; only pending outbound item duration 90s; label Route tea text Route review checklist; minutes_from_now 25; notify only action list; no cancellation local tomorrow 09:00; title Return the Route sample; no email or calendar event Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Known synthetic scheduled-send ID route-old-ping predates this case
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea
- Synthetic notification scheduler; no arbitrary future tool execution
- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action list; no cancellation
- Tomorrow 09:00 is future; native mirror request is intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0252 · Explicit sequence

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Please do these in this order: show all emails and texts currently queued for later delivery; then cancel the running timer with ID route-old-timer; then start a stopwatch for this work session; then schedule a text to Mom for tomorrow at 6 PM saying "Route pickup is ready."; then pause Music and Spotify in 30 minutes.

**Required tools:** `list_scheduled_sends`, `manage_timers`, `stopwatch`, `schedule_send`, `set_sleep_timer`.
**Ordering constraints:** `list_scheduled_sends` before `manage_timers`; `manage_timers` before `stopwatch`; `stopwatch` before `schedule_send`; `schedule_send` before `set_sleep_timer`.
**Checks:** read pending outbound queue; not timers action cancel; label route-old-timer; no cancel_all action start; counts up channel message; when tomorrow 18:00 local; no immediate send duration 30 minutes; sleep timer, not Mac shutdown Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic queue includes old reminder text ID route-old-ping
- Synthetic running timer route-old-timer and another timer exist
- Synthetic in-memory stopwatch is stopped
- In-memory synthetic outbound queue; no real scheduled delivery
- Synthetic players and persisted timer state

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0253 · Explicit sequence

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Please do these in this order: cancel all timers and alarms in this test session; then show all emails and texts currently queued for later delivery; then cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries; then schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready."; then read the elapsed time on my already running stopwatch.

**Required tools:** `manage_timers`, `list_scheduled_sends`, `cancel_scheduled_send`, `schedule_send`, `stopwatch`.
**Ordering constraints:** `manage_timers` before `list_scheduled_sends`; `list_scheduled_sends` before `cancel_scheduled_send`; `cancel_scheduled_send` before `schedule_send`; `schedule_send` before `stopwatch`.
**Checks:** action cancel_all; synthetic session timers only read pending outbound queue; not timers id route-old-ping; only pending outbound item channel email; when tomorrow 08:00; correct recipient/subject/body action read Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action cancel_all; synthetic session timers only
- Synthetic queue includes old reminder text ID route-old-ping
- Known synthetic scheduled-send ID route-old-ping predates this case
- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action read

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0254 · Explicit sequence

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Please do these in this order: schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready."; then cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries; then show all emails and texts currently queued for later delivery; then show how much time remains on my active timers and alarms; then stop my running stopwatch and report the elapsed time.

**Required tools:** `schedule_send`, `cancel_scheduled_send`, `list_scheduled_sends`, `manage_timers`, `stopwatch`.
**Ordering constraints:** `schedule_send` before `cancel_scheduled_send`; `cancel_scheduled_send` before `list_scheduled_sends`; `list_scheduled_sends` before `manage_timers`; `manage_timers` before `stopwatch`.
**Checks:** channel email; when tomorrow 08:00; correct recipient/subject/body id route-old-ping; only pending outbound item read pending outbound queue; not timers action list; no cancellation action stop Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Known synthetic scheduled-send ID route-old-ping predates this case
- Synthetic queue includes old reminder text ID route-old-ping
- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action list; no cancellation
- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action stop

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0255 · Explicit sequence

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Please do these in this order: schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready."; then record a lap on my running stopwatch; then set a one-time alarm for 6:45 PM labeled Route evening; then pause Music and Spotify in 30 minutes; then remind me tomorrow at 9 AM to return the Route sample.

**Required tools:** `schedule_send`, `stopwatch`, `set_alarm`, `set_sleep_timer`, `add_reminder`.
**Ordering constraints:** `schedule_send` before `stopwatch`; `stopwatch` before `set_alarm`; `set_alarm` before `set_sleep_timer`; `set_sleep_timer` before `add_reminder`.
**Checks:** channel email; when tomorrow 08:00; correct recipient/subject/body action lap time 18:45; repeat_daily false duration 30 minutes; sleep timer, not Mac shutdown local tomorrow 09:00; title Return the Route sample; no email or calendar event Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action lap
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false
- Synthetic players and persisted timer state
- Tomorrow 09:00 is future; native mirror request is intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0256 · Explicit sequence

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Please do these in this order: bring the text Route review checklist back to my attention in 25 minutes; then pause Music and Spotify in 30 minutes; then read the elapsed time on my already running stopwatch; then show how much time remains on my active timers and alarms; then remind me tomorrow at 9 AM to return the Route sample.

**Required tools:** `schedule_task`, `set_sleep_timer`, `stopwatch`, `manage_timers`, `add_reminder`.
**Ordering constraints:** `schedule_task` before `set_sleep_timer`; `set_sleep_timer` before `stopwatch`; `stopwatch` before `manage_timers`; `manage_timers` before `add_reminder`.
**Checks:** text Route review checklist; minutes_from_now 25; notify only duration 30 minutes; sleep timer, not Mac shutdown action read action list; no cancellation local tomorrow 09:00; title Return the Route sample; no email or calendar event Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic notification scheduler; no arbitrary future tool execution
- Synthetic players and persisted timer state
- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action read
- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action list; no cancellation
- Tomorrow 09:00 is future; native mirror request is intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0257 · Explicit sequence

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Please do these in this order: pause Music and Spotify in 30 minutes; then show all emails and texts currently queued for later delivery; then stop my running stopwatch and report the elapsed time; then schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready."; then show how much time remains on my active timers and alarms.

**Required tools:** `set_sleep_timer`, `list_scheduled_sends`, `stopwatch`, `schedule_send`, `manage_timers`.
**Ordering constraints:** `set_sleep_timer` before `list_scheduled_sends`; `list_scheduled_sends` before `stopwatch`; `stopwatch` before `schedule_send`; `schedule_send` before `manage_timers`.
**Checks:** duration 30 minutes; sleep timer, not Mac shutdown read pending outbound queue; not timers action stop channel email; when tomorrow 08:00; correct recipient/subject/body action list; no cancellation Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic players and persisted timer state
- Synthetic queue includes old reminder text ID route-old-ping
- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action stop
- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action list; no cancellation

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0258 · Explicit sequence

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Please do these in this order: pause Music and Spotify in 30 minutes; then schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready."; then cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries; then show how much time remains on my active timers and alarms; then read the elapsed time on my already running stopwatch.

**Required tools:** `set_sleep_timer`, `schedule_send`, `cancel_scheduled_send`, `manage_timers`, `stopwatch`.
**Ordering constraints:** `set_sleep_timer` before `schedule_send`; `schedule_send` before `cancel_scheduled_send`; `cancel_scheduled_send` before `manage_timers`; `manage_timers` before `stopwatch`.
**Checks:** duration 30 minutes; sleep timer, not Mac shutdown channel email; when tomorrow 08:00; correct recipient/subject/body id route-old-ping; only pending outbound item action list; no cancellation action read Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic players and persisted timer state
- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Known synthetic scheduled-send ID route-old-ping predates this case
- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action list; no cancellation
- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action read

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0259 · Explicit sequence

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Please do these in this order: pause Music and Spotify in 30 minutes; then schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready."; then show all emails and texts currently queued for later delivery; then record a lap on my running stopwatch; then cancel all timers and alarms in this test session.

**Required tools:** `set_sleep_timer`, `schedule_send`, `list_scheduled_sends`, `stopwatch`, `manage_timers`.
**Ordering constraints:** `set_sleep_timer` before `schedule_send`; `schedule_send` before `list_scheduled_sends`; `list_scheduled_sends` before `stopwatch`; `stopwatch` before `manage_timers`.
**Checks:** duration 30 minutes; sleep timer, not Mac shutdown channel email; when tomorrow 08:00; correct recipient/subject/body read pending outbound queue; not timers action lap action cancel_all; synthetic session timers only Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic players and persisted timer state
- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Synthetic queue includes old reminder text ID route-old-ping
- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action lap
- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action cancel_all; synthetic session timers only

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0260 · Explicit sequence

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Please do these in this order: pause Music and Spotify in 30 minutes; then set a one-time alarm for 6:45 PM labeled Route evening; then schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready."; then stop my running stopwatch and report the elapsed time; then remind me tomorrow at 9 AM to return the Route sample.

**Required tools:** `set_sleep_timer`, `set_alarm`, `schedule_send`, `stopwatch`, `add_reminder`.
**Ordering constraints:** `set_sleep_timer` before `set_alarm`; `set_alarm` before `schedule_send`; `schedule_send` before `stopwatch`; `stopwatch` before `add_reminder`.
**Checks:** duration 30 minutes; sleep timer, not Mac shutdown time 18:45; repeat_daily false channel email; when tomorrow 08:00; correct recipient/subject/body action stop local tomorrow 09:00; title Return the Route sample; no email or calendar event Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic players and persisted timer state
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false
- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action stop
- Tomorrow 09:00 is future; native mirror request is intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0261 · Natural compound request

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

I have a few things to finish. Remind me tomorrow at 9 AM to return the Route sample. Bring the text Route review checklist back to my attention in 25 minutes. Pause Music and Spotify in 30 minutes. Show all emails and texts currently queued for later delivery. Start a 90-second countdown named Route tea. Keep the results separate so I can tell what came from where.

**Required tools:** `add_reminder`, `schedule_task`, `set_sleep_timer`, `list_scheduled_sends`, `set_timer`.
**Checks:** local tomorrow 09:00; title Return the Route sample; no email or calendar event text Route review checklist; minutes_from_now 25; notify only duration 30 minutes; sleep timer, not Mac shutdown read pending outbound queue; not timers duration 90s; label Route tea Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Tomorrow 09:00 is future; native mirror request is intercepted
- Synthetic notification scheduler; no arbitrary future tool execution
- Synthetic players and persisted timer state
- Synthetic queue includes old reminder text ID route-old-ping
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0262 · Natural compound request

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

I have a few things to finish. Show all emails and texts currently queued for later delivery. Pause Music and Spotify in 30 minutes. Remind me tomorrow at 9 AM to return the Route sample. Cancel all timers and alarms in this test session. Read the elapsed time on my already running stopwatch. Keep the results separate so I can tell what came from where.

**Required tools:** `list_scheduled_sends`, `set_sleep_timer`, `add_reminder`, `manage_timers`, `stopwatch`.
**Checks:** read pending outbound queue; not timers duration 30 minutes; sleep timer, not Mac shutdown local tomorrow 09:00; title Return the Route sample; no email or calendar event action cancel_all; synthetic session timers only action read Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic queue includes old reminder text ID route-old-ping
- Synthetic players and persisted timer state
- Tomorrow 09:00 is future; native mirror request is intercepted
- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action cancel_all; synthetic session timers only
- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action read

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0263 · Natural compound request

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

I have a few things to finish. Show how much time remains on my active timers and alarms. Start a 90-second countdown named Route tea. Remind me tomorrow at 9 AM to return the Route sample. Bring the text Route review checklist back to my attention in 25 minutes. Pause Music and Spotify in 30 minutes. Keep the results separate so I can tell what came from where.

**Required tools:** `manage_timers`, `set_timer`, `add_reminder`, `schedule_task`, `set_sleep_timer`.
**Checks:** action list; no cancellation duration 90s; label Route tea local tomorrow 09:00; title Return the Route sample; no email or calendar event text Route review checklist; minutes_from_now 25; notify only duration 30 minutes; sleep timer, not Mac shutdown Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action list; no cancellation
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea
- Tomorrow 09:00 is future; native mirror request is intercepted
- Synthetic notification scheduler; no arbitrary future tool execution
- Synthetic players and persisted timer state

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0264 · Natural compound request

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

I have a few things to finish. Schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready.". Show how much time remains on my active timers and alarms. Show all emails and texts currently queued for later delivery. Cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries. Pause Music and Spotify in 30 minutes. Keep the results separate so I can tell what came from where.

**Required tools:** `schedule_send`, `manage_timers`, `list_scheduled_sends`, `cancel_scheduled_send`, `set_sleep_timer`.
**Checks:** channel email; when tomorrow 08:00; correct recipient/subject/body action list; no cancellation read pending outbound queue; not timers id route-old-ping; only pending outbound item duration 30 minutes; sleep timer, not Mac shutdown Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action list; no cancellation
- Synthetic queue includes old reminder text ID route-old-ping
- Known synthetic scheduled-send ID route-old-ping predates this case
- Synthetic players and persisted timer state

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0265 · Natural compound request

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

I have a few things to finish. Schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready.". Cancel all timers and alarms in this test session. Pause Music and Spotify in 30 minutes. Bring the text Route review checklist back to my attention in 25 minutes. Cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries. Keep the results separate so I can tell what came from where.

**Required tools:** `schedule_send`, `manage_timers`, `set_sleep_timer`, `schedule_task`, `cancel_scheduled_send`.
**Checks:** channel email; when tomorrow 08:00; correct recipient/subject/body action cancel_all; synthetic session timers only duration 30 minutes; sleep timer, not Mac shutdown text Route review checklist; minutes_from_now 25; notify only id route-old-ping; only pending outbound item Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action cancel_all; synthetic session timers only
- Synthetic players and persisted timer state
- Synthetic notification scheduler; no arbitrary future tool execution
- Known synthetic scheduled-send ID route-old-ping predates this case

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0266 · Natural compound request

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

I have a few things to finish. Schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready.". Pause Music and Spotify in 30 minutes. Cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries. Show all emails and texts currently queued for later delivery. Remind me tomorrow at 9 AM to return the Route sample. Keep the results separate so I can tell what came from where.

**Required tools:** `schedule_send`, `set_sleep_timer`, `cancel_scheduled_send`, `list_scheduled_sends`, `add_reminder`.
**Checks:** channel email; when tomorrow 08:00; correct recipient/subject/body duration 30 minutes; sleep timer, not Mac shutdown id route-old-ping; only pending outbound item read pending outbound queue; not timers local tomorrow 09:00; title Return the Route sample; no email or calendar event Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Synthetic players and persisted timer state
- Known synthetic scheduled-send ID route-old-ping predates this case
- Synthetic queue includes old reminder text ID route-old-ping
- Tomorrow 09:00 is future; native mirror request is intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0267 · Natural compound request

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

I have a few things to finish. Schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready.". Start a 90-second countdown named Route tea. Show all emails and texts currently queued for later delivery. Set a one-time alarm for 6:45 PM labeled Route evening. Show how much time remains on my active timers and alarms. Keep the results separate so I can tell what came from where.

**Required tools:** `schedule_send`, `set_timer`, `list_scheduled_sends`, `set_alarm`, `manage_timers`.
**Checks:** channel email; when tomorrow 08:00; correct recipient/subject/body duration 90s; label Route tea read pending outbound queue; not timers time 18:45; repeat_daily false action list; no cancellation Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea
- Synthetic queue includes old reminder text ID route-old-ping
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false
- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action list; no cancellation

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0268 · Natural compound request

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

I have a few things to finish. Pause Music and Spotify in 30 minutes. Set a one-time alarm for 6:45 PM labeled Route evening. Show how much time remains on my active timers and alarms. Schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready.". Show all emails and texts currently queued for later delivery. Keep the results separate so I can tell what came from where.

**Required tools:** `set_sleep_timer`, `set_alarm`, `manage_timers`, `schedule_send`, `list_scheduled_sends`.
**Checks:** duration 30 minutes; sleep timer, not Mac shutdown time 18:45; repeat_daily false action list; no cancellation channel email; when tomorrow 08:00; correct recipient/subject/body read pending outbound queue; not timers Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic players and persisted timer state
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false
- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action list; no cancellation
- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Synthetic queue includes old reminder text ID route-old-ping

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0269 · Natural compound request

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

I have a few things to finish. Start a ten-minute timer labeled Route pasta. Pause Music and Spotify in 30 minutes. Show all emails and texts currently queued for later delivery. Start a stopwatch for this work session. Cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries. Keep the results separate so I can tell what came from where.

**Required tools:** `set_timer`, `set_sleep_timer`, `list_scheduled_sends`, `stopwatch`, `cancel_scheduled_send`.
**Checks:** duration 10 minutes; label Route pasta; not add_reminder duration 30 minutes; sleep timer, not Mac shutdown read pending outbound queue; not timers action start; counts up id route-old-ping; only pending outbound item Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic timer daemon with notifications intercepted
- Synthetic players and persisted timer state
- Synthetic queue includes old reminder text ID route-old-ping
- Synthetic in-memory stopwatch is stopped
- Known synthetic scheduled-send ID route-old-ping predates this case

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0270 · Natural compound request

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

I have a few things to finish. Read the elapsed time on my already running stopwatch. Show all emails and texts currently queued for later delivery. Show how much time remains on my active timers and alarms. Schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready.". Cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries. Keep the results separate so I can tell what came from where.

**Required tools:** `stopwatch`, `list_scheduled_sends`, `manage_timers`, `schedule_send`, `cancel_scheduled_send`.
**Checks:** action read read pending outbound queue; not timers action list; no cancellation channel email; when tomorrow 08:00; correct recipient/subject/body id route-old-ping; only pending outbound item Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action read
- Synthetic queue includes old reminder text ID route-old-ping
- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action list; no cancellation
- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Known synthetic scheduled-send ID route-old-ping predates this case

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0271 · Scoped execution

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

For these tasks, use only the named sources and targets: remind me tomorrow at 9 AM to return the Route sample; then schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready."; then cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries; then set a one-time alarm for 6:45 PM labeled Route evening; then pause Music and Spotify in 30 minutes. Leave everything else unchanged.

**Required tools:** `add_reminder`, `schedule_send`, `cancel_scheduled_send`, `set_alarm`, `set_sleep_timer`.
**Ordering constraints:** `add_reminder` before `schedule_send`; `schedule_send` before `cancel_scheduled_send`; `cancel_scheduled_send` before `set_alarm`; `set_alarm` before `set_sleep_timer`.
**Checks:** local tomorrow 09:00; title Return the Route sample; no email or calendar event channel email; when tomorrow 08:00; correct recipient/subject/body id route-old-ping; only pending outbound item time 18:45; repeat_daily false duration 30 minutes; sleep timer, not Mac shutdown Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Tomorrow 09:00 is future; native mirror request is intercepted
- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Known synthetic scheduled-send ID route-old-ping predates this case
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false
- Synthetic players and persisted timer state

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0272 · Scoped execution

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

For these tasks, use only the named sources and targets: show all emails and texts currently queued for later delivery; then cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries; then schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready."; then set a one-time alarm for 6:45 PM labeled Route evening; then bring the text Route review checklist back to my attention in 25 minutes. Leave everything else unchanged.

**Required tools:** `list_scheduled_sends`, `cancel_scheduled_send`, `schedule_send`, `set_alarm`, `schedule_task`.
**Ordering constraints:** `list_scheduled_sends` before `cancel_scheduled_send`; `cancel_scheduled_send` before `schedule_send`; `schedule_send` before `set_alarm`; `set_alarm` before `schedule_task`.
**Checks:** read pending outbound queue; not timers id route-old-ping; only pending outbound item channel email; when tomorrow 08:00; correct recipient/subject/body time 18:45; repeat_daily false text Route review checklist; minutes_from_now 25; notify only Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic queue includes old reminder text ID route-old-ping
- Known synthetic scheduled-send ID route-old-ping predates this case
- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false
- Synthetic notification scheduler; no arbitrary future tool execution

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0273 · Scoped execution

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

For these tasks, use only the named sources and targets: show all emails and texts currently queued for later delivery; then show how much time remains on my active timers and alarms; then stop my running stopwatch and report the elapsed time; then pause Music and Spotify in 30 minutes; then cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries. Leave everything else unchanged.

**Required tools:** `list_scheduled_sends`, `manage_timers`, `stopwatch`, `set_sleep_timer`, `cancel_scheduled_send`.
**Ordering constraints:** `list_scheduled_sends` before `manage_timers`; `manage_timers` before `stopwatch`; `stopwatch` before `set_sleep_timer`; `set_sleep_timer` before `cancel_scheduled_send`.
**Checks:** read pending outbound queue; not timers action list; no cancellation action stop duration 30 minutes; sleep timer, not Mac shutdown id route-old-ping; only pending outbound item Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic queue includes old reminder text ID route-old-ping
- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action list; no cancellation
- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action stop
- Synthetic players and persisted timer state
- Known synthetic scheduled-send ID route-old-ping predates this case

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0274 · Scoped execution

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

For these tasks, use only the named sources and targets: show all emails and texts currently queued for later delivery; then schedule a text to Mom for tomorrow at 6 PM saying "Route pickup is ready."; then start a stopwatch for this work session; then cancel the running timer with ID route-old-timer; then cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries. Leave everything else unchanged.

**Required tools:** `list_scheduled_sends`, `schedule_send`, `stopwatch`, `manage_timers`, `cancel_scheduled_send`.
**Ordering constraints:** `list_scheduled_sends` before `schedule_send`; `schedule_send` before `stopwatch`; `stopwatch` before `manage_timers`; `manage_timers` before `cancel_scheduled_send`.
**Checks:** read pending outbound queue; not timers channel message; when tomorrow 18:00 local; no immediate send action start; counts up action cancel; label route-old-timer; no cancel_all id route-old-ping; only pending outbound item Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic queue includes old reminder text ID route-old-ping
- In-memory synthetic outbound queue; no real scheduled delivery
- Synthetic in-memory stopwatch is stopped
- Synthetic running timer route-old-timer and another timer exist
- Known synthetic scheduled-send ID route-old-ping predates this case

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0275 · Scoped execution

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

For these tasks, use only the named sources and targets: show all emails and texts currently queued for later delivery; then pause Music and Spotify in 30 minutes; then schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready."; then show how much time remains on my active timers and alarms; then cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries. Leave everything else unchanged.

**Required tools:** `list_scheduled_sends`, `set_sleep_timer`, `schedule_send`, `manage_timers`, `cancel_scheduled_send`.
**Ordering constraints:** `list_scheduled_sends` before `set_sleep_timer`; `set_sleep_timer` before `schedule_send`; `schedule_send` before `manage_timers`; `manage_timers` before `cancel_scheduled_send`.
**Checks:** read pending outbound queue; not timers duration 30 minutes; sleep timer, not Mac shutdown channel email; when tomorrow 08:00; correct recipient/subject/body action list; no cancellation id route-old-ping; only pending outbound item Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic queue includes old reminder text ID route-old-ping
- Synthetic players and persisted timer state
- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action list; no cancellation
- Known synthetic scheduled-send ID route-old-ping predates this case

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0276 · Scoped execution

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

For these tasks, use only the named sources and targets: cancel all timers and alarms in this test session; then stop my running stopwatch and report the elapsed time; then set a one-time alarm for 6:45 PM labeled Route evening; then cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries; then remind me tomorrow at 9 AM to return the Route sample. Leave everything else unchanged.

**Required tools:** `manage_timers`, `stopwatch`, `set_alarm`, `cancel_scheduled_send`, `add_reminder`.
**Ordering constraints:** `manage_timers` before `stopwatch`; `stopwatch` before `set_alarm`; `set_alarm` before `cancel_scheduled_send`; `cancel_scheduled_send` before `add_reminder`.
**Checks:** action cancel_all; synthetic session timers only action stop time 18:45; repeat_daily false id route-old-ping; only pending outbound item local tomorrow 09:00; title Return the Route sample; no email or calendar event Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action cancel_all; synthetic session timers only
- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action stop
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false
- Known synthetic scheduled-send ID route-old-ping predates this case
- Tomorrow 09:00 is future; native mirror request is intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0277 · Scoped execution

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

For these tasks, use only the named sources and targets: set a one-time alarm for 6:45 PM labeled Route evening; then schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready."; then record a lap on my running stopwatch; then start a 90-second countdown named Route tea; then cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries. Leave everything else unchanged.

**Required tools:** `set_alarm`, `schedule_send`, `stopwatch`, `set_timer`, `cancel_scheduled_send`.
**Ordering constraints:** `set_alarm` before `schedule_send`; `schedule_send` before `stopwatch`; `stopwatch` before `set_timer`; `set_timer` before `cancel_scheduled_send`.
**Checks:** time 18:45; repeat_daily false channel email; when tomorrow 08:00; correct recipient/subject/body action lap duration 90s; label Route tea id route-old-ping; only pending outbound item Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false
- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action lap
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea
- Known synthetic scheduled-send ID route-old-ping predates this case

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0278 · Scoped execution

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

For these tasks, use only the named sources and targets: start a 90-second countdown named Route tea; then cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries; then record a lap on my running stopwatch; then schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready."; then set a one-time alarm for 6:45 PM labeled Route evening. Leave everything else unchanged.

**Required tools:** `set_timer`, `cancel_scheduled_send`, `stopwatch`, `schedule_send`, `set_alarm`.
**Ordering constraints:** `set_timer` before `cancel_scheduled_send`; `cancel_scheduled_send` before `stopwatch`; `stopwatch` before `schedule_send`; `schedule_send` before `set_alarm`.
**Checks:** duration 90s; label Route tea id route-old-ping; only pending outbound item action lap channel email; when tomorrow 08:00; correct recipient/subject/body time 18:45; repeat_daily false Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea
- Known synthetic scheduled-send ID route-old-ping predates this case
- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action lap
- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0279 · Scoped execution

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

For these tasks, use only the named sources and targets: record a lap on my running stopwatch; then schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready."; then cancel all timers and alarms in this test session; then cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries; then pause Music and Spotify in 30 minutes. Leave everything else unchanged.

**Required tools:** `stopwatch`, `schedule_send`, `manage_timers`, `cancel_scheduled_send`, `set_sleep_timer`.
**Ordering constraints:** `stopwatch` before `schedule_send`; `schedule_send` before `manage_timers`; `manage_timers` before `cancel_scheduled_send`; `cancel_scheduled_send` before `set_sleep_timer`.
**Checks:** action lap channel email; when tomorrow 08:00; correct recipient/subject/body action cancel_all; synthetic session timers only id route-old-ping; only pending outbound item duration 30 minutes; sleep timer, not Mac shutdown Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action lap
- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action cancel_all; synthetic session timers only
- Known synthetic scheduled-send ID route-old-ping predates this case
- Synthetic players and persisted timer state

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0280 · Scoped execution

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

For these tasks, use only the named sources and targets: read the elapsed time on my already running stopwatch; then schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready."; then cancel all timers and alarms in this test session; then pause Music and Spotify in 30 minutes; then set a one-time alarm for 6:45 PM labeled Route evening. Leave everything else unchanged.

**Required tools:** `stopwatch`, `schedule_send`, `manage_timers`, `set_sleep_timer`, `set_alarm`.
**Ordering constraints:** `stopwatch` before `schedule_send`; `schedule_send` before `manage_timers`; `manage_timers` before `set_sleep_timer`; `set_sleep_timer` before `set_alarm`.
**Checks:** action read channel email; when tomorrow 08:00; correct recipient/subject/body action cancel_all; synthetic session timers only duration 30 minutes; sleep timer, not Mac shutdown time 18:45; repeat_daily false Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action read
- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action cancel_all; synthetic session timers only
- Synthetic players and persisted timer state
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0281 · Late constraints

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries. Show all emails and texts currently queued for later delivery. Remind me tomorrow at 9 AM to return the Route sample. Show how much time remains on my active timers and alarms. Start a 90-second countdown named Route tea. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `cancel_scheduled_send`, `list_scheduled_sends`, `add_reminder`, `manage_timers`, `set_timer`.
**Checks:** id route-old-ping; only pending outbound item read pending outbound queue; not timers local tomorrow 09:00; title Return the Route sample; no email or calendar event action list; no cancellation duration 90s; label Route tea Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Known synthetic scheduled-send ID route-old-ping predates this case
- Synthetic queue includes old reminder text ID route-old-ping
- Tomorrow 09:00 is future; native mirror request is intercepted
- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action list; no cancellation
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0282 · Late constraints

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Show all emails and texts currently queued for later delivery. Set a one-time alarm for 6:45 PM labeled Route evening. Stop my running stopwatch and report the elapsed time. Cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries. Schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready.". One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `list_scheduled_sends`, `set_alarm`, `stopwatch`, `cancel_scheduled_send`, `schedule_send`.
**Checks:** read pending outbound queue; not timers time 18:45; repeat_daily false action stop id route-old-ping; only pending outbound item channel email; when tomorrow 08:00; correct recipient/subject/body Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic queue includes old reminder text ID route-old-ping
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false
- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action stop
- Known synthetic scheduled-send ID route-old-ping predates this case
- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0283 · Late constraints

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Show all emails and texts currently queued for later delivery. Read the elapsed time on my already running stopwatch. Cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries. Bring the text Route review checklist back to my attention in 25 minutes. Pause Music and Spotify in 30 minutes. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `list_scheduled_sends`, `stopwatch`, `cancel_scheduled_send`, `schedule_task`, `set_sleep_timer`.
**Checks:** read pending outbound queue; not timers action read id route-old-ping; only pending outbound item text Route review checklist; minutes_from_now 25; notify only duration 30 minutes; sleep timer, not Mac shutdown Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic queue includes old reminder text ID route-old-ping
- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action read
- Known synthetic scheduled-send ID route-old-ping predates this case
- Synthetic notification scheduler; no arbitrary future tool execution
- Synthetic players and persisted timer state

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0284 · Late constraints

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Cancel all timers and alarms in this test session. Remind me tomorrow at 9 AM to return the Route sample. Cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries. Show all emails and texts currently queued for later delivery. Record a lap on my running stopwatch. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `manage_timers`, `add_reminder`, `cancel_scheduled_send`, `list_scheduled_sends`, `stopwatch`.
**Checks:** action cancel_all; synthetic session timers only local tomorrow 09:00; title Return the Route sample; no email or calendar event id route-old-ping; only pending outbound item read pending outbound queue; not timers action lap Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action cancel_all; synthetic session timers only
- Tomorrow 09:00 is future; native mirror request is intercepted
- Known synthetic scheduled-send ID route-old-ping predates this case
- Synthetic queue includes old reminder text ID route-old-ping
- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action lap

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0285 · Late constraints

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Cancel the running timer with ID route-old-timer. Cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries. Pause Music and Spotify in 30 minutes. Start a stopwatch for this work session. Schedule a text to Mom for tomorrow at 6 PM saying "Route pickup is ready.". One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `manage_timers`, `cancel_scheduled_send`, `set_sleep_timer`, `stopwatch`, `schedule_send`.
**Checks:** action cancel; label route-old-timer; no cancel_all id route-old-ping; only pending outbound item duration 30 minutes; sleep timer, not Mac shutdown action start; counts up channel message; when tomorrow 18:00 local; no immediate send Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic running timer route-old-timer and another timer exist
- Known synthetic scheduled-send ID route-old-ping predates this case
- Synthetic players and persisted timer state
- Synthetic in-memory stopwatch is stopped
- In-memory synthetic outbound queue; no real scheduled delivery

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0286 · Late constraints

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Show how much time remains on my active timers and alarms. Show all emails and texts currently queued for later delivery. Pause Music and Spotify in 30 minutes. Remind me tomorrow at 9 AM to return the Route sample. Start a 90-second countdown named Route tea. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `manage_timers`, `list_scheduled_sends`, `set_sleep_timer`, `add_reminder`, `set_timer`.
**Checks:** action list; no cancellation read pending outbound queue; not timers duration 30 minutes; sleep timer, not Mac shutdown local tomorrow 09:00; title Return the Route sample; no email or calendar event duration 90s; label Route tea Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action list; no cancellation
- Synthetic queue includes old reminder text ID route-old-ping
- Synthetic players and persisted timer state
- Tomorrow 09:00 is future; native mirror request is intercepted
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0287 · Late constraints

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Cancel all timers and alarms in this test session. Bring the text Route review checklist back to my attention in 25 minutes. Pause Music and Spotify in 30 minutes. Schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready.". Show all emails and texts currently queued for later delivery. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `manage_timers`, `schedule_task`, `set_sleep_timer`, `schedule_send`, `list_scheduled_sends`.
**Checks:** action cancel_all; synthetic session timers only text Route review checklist; minutes_from_now 25; notify only duration 30 minutes; sleep timer, not Mac shutdown channel email; when tomorrow 08:00; correct recipient/subject/body read pending outbound queue; not timers Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action cancel_all; synthetic session timers only
- Synthetic notification scheduler; no arbitrary future tool execution
- Synthetic players and persisted timer state
- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Synthetic queue includes old reminder text ID route-old-ping

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0288 · Late constraints

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Set a one-time alarm for 6:45 PM labeled Route evening. Bring the text Route review checklist back to my attention in 25 minutes. Show all emails and texts currently queued for later delivery. Start a 90-second countdown named Route tea. Stop my running stopwatch and report the elapsed time. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `set_alarm`, `schedule_task`, `list_scheduled_sends`, `set_timer`, `stopwatch`.
**Checks:** time 18:45; repeat_daily false text Route review checklist; minutes_from_now 25; notify only read pending outbound queue; not timers duration 90s; label Route tea action stop Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false
- Synthetic notification scheduler; no arbitrary future tool execution
- Synthetic queue includes old reminder text ID route-old-ping
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea
- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action stop

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0289 · Late constraints

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Pause Music and Spotify in 30 minutes. Show all emails and texts currently queued for later delivery. Start a 90-second countdown named Route tea. Cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries. Bring the text Route review checklist back to my attention in 25 minutes. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `set_sleep_timer`, `list_scheduled_sends`, `set_timer`, `cancel_scheduled_send`, `schedule_task`.
**Checks:** duration 30 minutes; sleep timer, not Mac shutdown read pending outbound queue; not timers duration 90s; label Route tea id route-old-ping; only pending outbound item text Route review checklist; minutes_from_now 25; notify only Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic players and persisted timer state
- Synthetic queue includes old reminder text ID route-old-ping
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea
- Known synthetic scheduled-send ID route-old-ping predates this case
- Synthetic notification scheduler; no arbitrary future tool execution

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0290 · Late constraints

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Start a 90-second countdown named Route tea. Show how much time remains on my active timers and alarms. Read the elapsed time on my already running stopwatch. Pause Music and Spotify in 30 minutes. Cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `set_timer`, `manage_timers`, `stopwatch`, `set_sleep_timer`, `cancel_scheduled_send`.
**Checks:** duration 90s; label Route tea action list; no cancellation action read duration 30 minutes; sleep timer, not Mac shutdown id route-old-ping; only pending outbound item Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea
- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action list; no cancellation
- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action read
- Synthetic players and persisted timer state
- Known synthetic scheduled-send ID route-old-ping predates this case

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0291 · Colloquial with interruptions

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Could you cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries; then remind me tomorrow at 9 AM to return the Route sample; then pause Music and Spotify in 30 minutes; then cancel the running timer with ID route-old-timer; then schedule a text to Mom for tomorrow at 6 PM saying "Route pickup is ready."? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `cancel_scheduled_send`, `add_reminder`, `set_sleep_timer`, `manage_timers`, `schedule_send`.
**Ordering constraints:** `cancel_scheduled_send` before `add_reminder`; `add_reminder` before `set_sleep_timer`; `set_sleep_timer` before `manage_timers`; `manage_timers` before `schedule_send`.
**Checks:** id route-old-ping; only pending outbound item local tomorrow 09:00; title Return the Route sample; no email or calendar event duration 30 minutes; sleep timer, not Mac shutdown action cancel; label route-old-timer; no cancel_all channel message; when tomorrow 18:00 local; no immediate send Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Known synthetic scheduled-send ID route-old-ping predates this case
- Tomorrow 09:00 is future; native mirror request is intercepted
- Synthetic players and persisted timer state
- Synthetic running timer route-old-timer and another timer exist
- In-memory synthetic outbound queue; no real scheduled delivery

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0292 · Colloquial with interruptions

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Could you cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries; then bring the text Route review checklist back to my attention in 25 minutes; then start a 90-second countdown named Route tea; then show all emails and texts currently queued for later delivery; then record a lap on my running stopwatch? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `cancel_scheduled_send`, `schedule_task`, `set_timer`, `list_scheduled_sends`, `stopwatch`.
**Ordering constraints:** `cancel_scheduled_send` before `schedule_task`; `schedule_task` before `set_timer`; `set_timer` before `list_scheduled_sends`; `list_scheduled_sends` before `stopwatch`.
**Checks:** id route-old-ping; only pending outbound item text Route review checklist; minutes_from_now 25; notify only duration 90s; label Route tea read pending outbound queue; not timers action lap Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Known synthetic scheduled-send ID route-old-ping predates this case
- Synthetic notification scheduler; no arbitrary future tool execution
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea
- Synthetic queue includes old reminder text ID route-old-ping
- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action lap

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0293 · Colloquial with interruptions

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Could you show all emails and texts currently queued for later delivery; then show how much time remains on my active timers and alarms; then pause Music and Spotify in 30 minutes; then read the elapsed time on my already running stopwatch; then schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready."? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `list_scheduled_sends`, `manage_timers`, `set_sleep_timer`, `stopwatch`, `schedule_send`.
**Ordering constraints:** `list_scheduled_sends` before `manage_timers`; `manage_timers` before `set_sleep_timer`; `set_sleep_timer` before `stopwatch`; `stopwatch` before `schedule_send`.
**Checks:** read pending outbound queue; not timers action list; no cancellation duration 30 minutes; sleep timer, not Mac shutdown action read channel email; when tomorrow 08:00; correct recipient/subject/body Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic queue includes old reminder text ID route-old-ping
- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action list; no cancellation
- Synthetic players and persisted timer state
- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action read
- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0294 · Colloquial with interruptions

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Could you show all emails and texts currently queued for later delivery; then bring the text Route review checklist back to my attention in 25 minutes; then start a 90-second countdown named Route tea; then schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready."; then cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `list_scheduled_sends`, `schedule_task`, `set_timer`, `schedule_send`, `cancel_scheduled_send`.
**Ordering constraints:** `list_scheduled_sends` before `schedule_task`; `schedule_task` before `set_timer`; `set_timer` before `schedule_send`; `schedule_send` before `cancel_scheduled_send`.
**Checks:** read pending outbound queue; not timers text Route review checklist; minutes_from_now 25; notify only duration 90s; label Route tea channel email; when tomorrow 08:00; correct recipient/subject/body id route-old-ping; only pending outbound item Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic queue includes old reminder text ID route-old-ping
- Synthetic notification scheduler; no arbitrary future tool execution
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea
- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Known synthetic scheduled-send ID route-old-ping predates this case

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0295 · Colloquial with interruptions

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Could you show all emails and texts currently queued for later delivery; then pause Music and Spotify in 30 minutes; then schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready."; then cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries; then start a 90-second countdown named Route tea? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `list_scheduled_sends`, `set_sleep_timer`, `schedule_send`, `cancel_scheduled_send`, `set_timer`.
**Ordering constraints:** `list_scheduled_sends` before `set_sleep_timer`; `set_sleep_timer` before `schedule_send`; `schedule_send` before `cancel_scheduled_send`; `cancel_scheduled_send` before `set_timer`.
**Checks:** read pending outbound queue; not timers duration 30 minutes; sleep timer, not Mac shutdown channel email; when tomorrow 08:00; correct recipient/subject/body id route-old-ping; only pending outbound item duration 90s; label Route tea Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic queue includes old reminder text ID route-old-ping
- Synthetic players and persisted timer state
- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Known synthetic scheduled-send ID route-old-ping predates this case
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0296 · Colloquial with interruptions

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Could you show how much time remains on my active timers and alarms; then cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries; then show all emails and texts currently queued for later delivery; then bring the text Route review checklist back to my attention in 25 minutes; then remind me tomorrow at 9 AM to return the Route sample? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `manage_timers`, `cancel_scheduled_send`, `list_scheduled_sends`, `schedule_task`, `add_reminder`.
**Ordering constraints:** `manage_timers` before `cancel_scheduled_send`; `cancel_scheduled_send` before `list_scheduled_sends`; `list_scheduled_sends` before `schedule_task`; `schedule_task` before `add_reminder`.
**Checks:** action list; no cancellation id route-old-ping; only pending outbound item read pending outbound queue; not timers text Route review checklist; minutes_from_now 25; notify only local tomorrow 09:00; title Return the Route sample; no email or calendar event Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action list; no cancellation
- Known synthetic scheduled-send ID route-old-ping predates this case
- Synthetic queue includes old reminder text ID route-old-ping
- Synthetic notification scheduler; no arbitrary future tool execution
- Tomorrow 09:00 is future; native mirror request is intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0297 · Colloquial with interruptions

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Could you schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready."; then cancel the old queued Route ping, ID route-old-ping, without cancelling other deliveries; then pause Music and Spotify in 30 minutes; then record a lap on my running stopwatch; then cancel all timers and alarms in this test session? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `schedule_send`, `cancel_scheduled_send`, `set_sleep_timer`, `stopwatch`, `manage_timers`.
**Ordering constraints:** `schedule_send` before `cancel_scheduled_send`; `cancel_scheduled_send` before `set_sleep_timer`; `set_sleep_timer` before `stopwatch`; `stopwatch` before `manage_timers`.
**Checks:** channel email; when tomorrow 08:00; correct recipient/subject/body id route-old-ping; only pending outbound item duration 30 minutes; sleep timer, not Mac shutdown action lap action cancel_all; synthetic session timers only Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Known synthetic scheduled-send ID route-old-ping predates this case
- Synthetic players and persisted timer state
- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action lap
- Synthetic running timer route-old-timer and another timer exist; variant-specific state must satisfy: action cancel_all; synthetic session timers only

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0298 · Colloquial with interruptions

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Could you set a one-time alarm for 6:45 PM labeled Route evening; then schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready."; then read the elapsed time on my already running stopwatch; then bring the text Route review checklist back to my attention in 25 minutes; then show all emails and texts currently queued for later delivery? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `set_alarm`, `schedule_send`, `stopwatch`, `schedule_task`, `list_scheduled_sends`.
**Ordering constraints:** `set_alarm` before `schedule_send`; `schedule_send` before `stopwatch`; `stopwatch` before `schedule_task`; `schedule_task` before `list_scheduled_sends`.
**Checks:** time 18:45; repeat_daily false channel email; when tomorrow 08:00; correct recipient/subject/body action read text Route review checklist; minutes_from_now 25; notify only read pending outbound queue; not timers Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false
- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action read
- Synthetic notification scheduler; no arbitrary future tool execution
- Synthetic queue includes old reminder text ID route-old-ping

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0299 · Colloquial with interruptions

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Could you set a one-time alarm for 6:45 PM labeled Route evening; then pause Music and Spotify in 30 minutes; then stop my running stopwatch and report the elapsed time; then schedule an email to johnstandark@gmail.com tomorrow at 8 AM, subject Route morning and body "The sample is ready."; then bring the text Route review checklist back to my attention in 25 minutes? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `set_alarm`, `set_sleep_timer`, `stopwatch`, `schedule_send`, `schedule_task`.
**Ordering constraints:** `set_alarm` before `set_sleep_timer`; `set_sleep_timer` before `stopwatch`; `stopwatch` before `schedule_send`; `schedule_send` before `schedule_task`.
**Checks:** time 18:45; repeat_daily false duration 30 minutes; sleep timer, not Mac shutdown action stop channel email; when tomorrow 08:00; correct recipient/subject/body text Route review checklist; minutes_from_now 25; notify only Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false
- Synthetic players and persisted timer state
- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action stop
- In-memory synthetic outbound queue; no real scheduled delivery; variant-specific state must satisfy: channel email; when tomorrow 08:00; correct recipient/subject/body
- Synthetic notification scheduler; no arbitrary future tool execution

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0300 · Colloquial with interruptions

**Focus:** Keep scheduled sends separate from timers, clock alarms, and reminders.

**Prompt:**

Could you record a lap on my running stopwatch; then remind me tomorrow at 9 AM to return the Route sample; then start a 90-second countdown named Route tea; then set a one-time alarm for 6:45 PM labeled Route evening; then bring the text Route review checklist back to my attention in 25 minutes? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `stopwatch`, `add_reminder`, `set_timer`, `set_alarm`, `schedule_task`.
**Ordering constraints:** `stopwatch` before `add_reminder`; `add_reminder` before `set_timer`; `set_timer` before `set_alarm`; `set_alarm` before `schedule_task`.
**Checks:** action lap local tomorrow 09:00; title Return the Route sample; no email or calendar event duration 90s; label Route tea time 18:45; repeat_daily false text Route review checklist; minutes_from_now 25; notify only Keep scheduled sends separate from timers, clock alarms, and reminders.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic in-memory stopwatch is stopped; variant-specific state must satisfy: action lap
- Tomorrow 09:00 is future; native mirror request is intercepted
- Synthetic timer daemon with notifications intercepted; variant-specific state must satisfy: duration 90s; label Route tea
- Synthetic timer clock; nearest future occurrence; no real alarm armed; variant-specific state must satisfy: time 18:45; repeat_daily false
- Synthetic notification scheduler; no arbitrary future tool execution

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.
