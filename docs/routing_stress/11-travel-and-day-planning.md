# Review only — no tests run

## 11. Travel and day planning

Combine personal schedules, directions, location-aware facts, and manual app handoffs.

### WRS-0501 · Explicit sequence

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Please do these in this order: give me today's briefing, including weather for Oakland, California; then find a 45-minute free slot tomorrow between 10 AM and 5 PM; then estimate driving time from San Francisco to Oakland and say whether traffic is included; then open Find My so I can look for my Route test AirPods; then tell me the current time difference between Tokyo and London.

**Required tools:** `daily_brief`, `find_free_time`, `travel_time`, `find_my_device`, `world_time`.
**Ordering constraints:** `daily_brief` before `find_free_time`; `find_free_time` before `travel_time`; `travel_time` before `find_my_device`; `find_my_device` before `world_time`.
**Checks:** location Oakland,CA; day briefing with activity; not exhaustive task inventory minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions origin San Francisco; destination Oakland; free-flow disclaimer app handoff only; do not report location or play a sound place Tokyo; compare_to London; current offsets Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Calendar, weather and recent-activity fixture responses are populated
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow
- Synthetic driving route, 20 km and 25 minutes, no traffic
- Synthetic app launch with no location data
- Per-run captured clock and known IANA zones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0502 · Explicit sequence

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Please do these in this order: give me today's briefing, including weather for Oakland, California; then tell me the current time difference between Tokyo and London; then open Find My so I can look for my Route test AirPods; then open transit directions from Oakland City Hall to Lake Merritt; then find coffee shops within two kilometers of Oakland City Hall.

**Required tools:** `daily_brief`, `world_time`, `find_my_device`, `get_directions`, `find_place`.
**Ordering constraints:** `daily_brief` before `world_time`; `world_time` before `find_my_device`; `find_my_device` before `get_directions`; `get_directions` before `find_place`.
**Checks:** location Oakland,CA; day briefing with activity; not exhaustive task inventory place Tokyo; compare_to London; current offsets app handoff only; do not report location or play a sound mode transit; exact origin/destination what coffee; near Oakland City Hall; radius_km 2 Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Calendar, weather and recent-activity fixture responses are populated
- Per-run captured clock and known IANA zones
- Synthetic app launch with no location data
- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode transit; exact origin/destination
- Synthetic map results with names, addresses, distances

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0503 · Explicit sequence

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Please do these in this order: give me today's briefing, including weather for Oakland, California; then tell me the current time difference between Tokyo and London; then open transit directions from Oakland City Hall to Lake Merritt; then find a 45-minute free slot tomorrow between 10 AM and 5 PM; then find coffee shops within two kilometers of Oakland City Hall.

**Required tools:** `daily_brief`, `world_time`, `get_directions`, `find_free_time`, `find_place`.
**Ordering constraints:** `daily_brief` before `world_time`; `world_time` before `get_directions`; `get_directions` before `find_free_time`; `find_free_time` before `find_place`.
**Checks:** location Oakland,CA; day briefing with activity; not exhaustive task inventory place Tokyo; compare_to London; current offsets mode transit; exact origin/destination minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions what coffee; near Oakland City Hall; radius_km 2 Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Calendar, weather and recent-activity fixture responses are populated
- Per-run captured clock and known IANA zones
- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode transit; exact origin/destination
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow
- Synthetic map results with names, addresses, distances

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0504 · Explicit sequence

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Please do these in this order: open Find My so I can look for my Route test AirPods; then open driving directions from Oakland City Hall to Lake Merritt; then find coffee shops within two kilometers of Oakland City Hall; then tell me the current time difference between Tokyo and London; then give me today's briefing, including weather for Oakland, California.

**Required tools:** `find_my_device`, `get_directions`, `find_place`, `world_time`, `daily_brief`.
**Ordering constraints:** `find_my_device` before `get_directions`; `get_directions` before `find_place`; `find_place` before `world_time`; `world_time` before `daily_brief`.
**Checks:** app handoff only; do not report location or play a sound mode driving; exact origin/destination what coffee; near Oakland City Hall; radius_km 2 place Tokyo; compare_to London; current offsets location Oakland,CA; day briefing with activity; not exhaustive task inventory Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic app launch with no location data
- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode driving; exact origin/destination
- Synthetic map results with names, addresses, distances
- Per-run captured clock and known IANA zones
- Calendar, weather and recent-activity fixture responses are populated

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0505 · Explicit sequence

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Please do these in this order: open driving directions from Oakland City Hall to Lake Merritt; then give me today's briefing, including weather for Oakland, California; then find a 45-minute free slot tomorrow between 10 AM and 5 PM; then open Find My so I can look for my Route test AirPods; then find coffee shops within two kilometers of Oakland City Hall.

**Required tools:** `get_directions`, `daily_brief`, `find_free_time`, `find_my_device`, `find_place`.
**Ordering constraints:** `get_directions` before `daily_brief`; `daily_brief` before `find_free_time`; `find_free_time` before `find_my_device`; `find_my_device` before `find_place`.
**Checks:** mode driving; exact origin/destination location Oakland,CA; day briefing with activity; not exhaustive task inventory minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions app handoff only; do not report location or play a sound what coffee; near Oakland City Hall; radius_km 2 Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode driving; exact origin/destination
- Calendar, weather and recent-activity fixture responses are populated
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow
- Synthetic app launch with no location data
- Synthetic map results with names, addresses, distances

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0506 · Explicit sequence

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Please do these in this order: open transit directions from Oakland City Hall to Lake Merritt; then estimate driving time from San Francisco to Oakland and say whether traffic is included; then open Find My so I can look for my Route test AirPods; then start a FaceTime video call to Mom; then find coffee shops within two kilometers of Oakland City Hall.

**Required tools:** `get_directions`, `travel_time`, `find_my_device`, `place_call`, `find_place`.
**Ordering constraints:** `get_directions` before `travel_time`; `travel_time` before `find_my_device`; `find_my_device` before `place_call`; `place_call` before `find_place`.
**Checks:** mode transit; exact origin/destination origin San Francisco; destination Oakland; free-flow disclaimer app handoff only; do not report location or play a sound audio_only false; exact fictional recipient what coffee; near Oakland City Hall; radius_km 2 Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode transit; exact origin/destination
- Synthetic driving route, 20 km and 25 minutes, no traffic
- Synthetic app launch with no location data
- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Synthetic map results with names, addresses, distances

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0507 · Explicit sequence

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Please do these in this order: get tomorrow's weather for Dublin, California, not Dublin in Ireland; then open driving directions from Oakland City Hall to Lake Merritt; then open the stored call link for Route standup; then open Find My so I can look for my Route test AirPods; then start a FaceTime video call to Mom.

**Required tools:** `get_weather`, `get_directions`, `join_video_call`, `find_my_device`, `place_call`.
**Ordering constraints:** `get_weather` before `get_directions`; `get_directions` before `join_video_call`; `join_video_call` before `find_my_device`; `find_my_device` before `place_call`.
**Checks:** location Dublin,CA; verify resolved place/date mode driving; exact origin/destination match title Route standup; use stored URL, never invent one app handoff only; do not report location or play a sound audio_only false; exact fictional recipient Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic three-day forecast resolves Dublin California
- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode driving; exact origin/destination
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- Synthetic app launch with no location data
- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0508 · Explicit sequence

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Please do these in this order: open the stored call link for Route standup; then find today's sunrise and sunset for Seattle, Washington; then start a FaceTime video call to Mom; then give me today's briefing, including weather for Oakland, California; then find a 45-minute free slot tomorrow between 10 AM and 5 PM.

**Required tools:** `join_video_call`, `astronomy`, `place_call`, `daily_brief`, `find_free_time`.
**Ordering constraints:** `join_video_call` before `astronomy`; `astronomy` before `place_call`; `place_call` before `daily_brief`; `daily_brief` before `find_free_time`.
**Checks:** match title Route standup; use stored URL, never invent one location Seattle,WA; actual current-day solar data audio_only false; exact fictional recipient location Oakland,CA; day briefing with activity; not exhaustive task inventory minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- Synthetic sunrise/sunset with timezone
- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Calendar, weather and recent-activity fixture responses are populated
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0509 · Explicit sequence

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Please do these in this order: open the stored call link for Route standup; then start a FaceTime video call to Mom; then open Find My so I can look for my Route test AirPods; then tell me the current time difference between Tokyo and London; then estimate driving time from San Francisco to Oakland and say whether traffic is included.

**Required tools:** `join_video_call`, `place_call`, `find_my_device`, `world_time`, `travel_time`.
**Ordering constraints:** `join_video_call` before `place_call`; `place_call` before `find_my_device`; `find_my_device` before `world_time`; `world_time` before `travel_time`.
**Checks:** match title Route standup; use stored URL, never invent one audio_only false; exact fictional recipient app handoff only; do not report location or play a sound place Tokyo; compare_to London; current offsets origin San Francisco; destination Oakland; free-flow disclaimer Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Synthetic app launch with no location data
- Per-run captured clock and known IANA zones
- Synthetic driving route, 20 km and 25 minutes, no traffic

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0510 · Explicit sequence

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Please do these in this order: estimate driving time from San Francisco to Oakland and say whether traffic is included; then open driving directions from Oakland City Hall to Lake Merritt; then find coffee shops within two kilometers of Oakland City Hall; then open Find My so I can look for my Route test AirPods; then tell me the current time difference between Tokyo and London.

**Required tools:** `travel_time`, `get_directions`, `find_place`, `find_my_device`, `world_time`.
**Ordering constraints:** `travel_time` before `get_directions`; `get_directions` before `find_place`; `find_place` before `find_my_device`; `find_my_device` before `world_time`.
**Checks:** origin San Francisco; destination Oakland; free-flow disclaimer mode driving; exact origin/destination what coffee; near Oakland City Hall; radius_km 2 app handoff only; do not report location or play a sound place Tokyo; compare_to London; current offsets Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic driving route, 20 km and 25 minutes, no traffic
- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode driving; exact origin/destination
- Synthetic map results with names, addresses, distances
- Synthetic app launch with no location data
- Per-run captured clock and known IANA zones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0511 · Natural compound request

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

I have a few things to finish. Find a 45-minute free slot tomorrow between 10 AM and 5 PM. Open Find My so I can look for my Route test AirPods. Find coffee shops within two kilometers of Oakland City Hall. Open transit directions from Oakland City Hall to Lake Merritt. Give me today's briefing, including weather for Oakland, California. Keep the results separate so I can tell what came from where.

**Required tools:** `find_free_time`, `find_my_device`, `find_place`, `get_directions`, `daily_brief`.
**Checks:** minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions app handoff only; do not report location or play a sound what coffee; near Oakland City Hall; radius_km 2 mode transit; exact origin/destination location Oakland,CA; day briefing with activity; not exhaustive task inventory Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow
- Synthetic app launch with no location data
- Synthetic map results with names, addresses, distances
- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode transit; exact origin/destination
- Calendar, weather and recent-activity fixture responses are populated

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0512 · Natural compound request

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

I have a few things to finish. Open Find My so I can look for my Route test AirPods. Find coffee shops within two kilometers of Oakland City Hall. Tell me the current time difference between Tokyo and London. Open transit directions from Oakland City Hall to Lake Merritt. Give me today's briefing, including weather for Oakland, California. Keep the results separate so I can tell what came from where.

**Required tools:** `find_my_device`, `find_place`, `world_time`, `get_directions`, `daily_brief`.
**Checks:** app handoff only; do not report location or play a sound what coffee; near Oakland City Hall; radius_km 2 place Tokyo; compare_to London; current offsets mode transit; exact origin/destination location Oakland,CA; day briefing with activity; not exhaustive task inventory Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic app launch with no location data
- Synthetic map results with names, addresses, distances
- Per-run captured clock and known IANA zones
- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode transit; exact origin/destination
- Calendar, weather and recent-activity fixture responses are populated

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0513 · Natural compound request

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

I have a few things to finish. Find coffee shops within two kilometers of Oakland City Hall. Open driving directions from Oakland City Hall to Lake Merritt. Start a FaceTime video call to Mom. Give me today's briefing, including weather for Oakland, California. Tell me the current time difference between Tokyo and London. Keep the results separate so I can tell what came from where.

**Required tools:** `find_place`, `get_directions`, `place_call`, `daily_brief`, `world_time`.
**Checks:** what coffee; near Oakland City Hall; radius_km 2 mode driving; exact origin/destination audio_only false; exact fictional recipient location Oakland,CA; day briefing with activity; not exhaustive task inventory place Tokyo; compare_to London; current offsets Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic map results with names, addresses, distances
- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode driving; exact origin/destination
- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Calendar, weather and recent-activity fixture responses are populated
- Per-run captured clock and known IANA zones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0514 · Natural compound request

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

I have a few things to finish. Find coffee shops within two kilometers of Oakland City Hall. Start a FaceTime video call to Mom. Tell me the current time difference between Tokyo and London. Give me today's briefing, including weather for Oakland, California. Estimate driving time from San Francisco to Oakland and say whether traffic is included. Keep the results separate so I can tell what came from where.

**Required tools:** `find_place`, `place_call`, `world_time`, `daily_brief`, `travel_time`.
**Checks:** what coffee; near Oakland City Hall; radius_km 2 audio_only false; exact fictional recipient place Tokyo; compare_to London; current offsets location Oakland,CA; day briefing with activity; not exhaustive task inventory origin San Francisco; destination Oakland; free-flow disclaimer Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic map results with names, addresses, distances
- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Per-run captured clock and known IANA zones
- Calendar, weather and recent-activity fixture responses are populated
- Synthetic driving route, 20 km and 25 minutes, no traffic

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0515 · Natural compound request

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

I have a few things to finish. Open transit directions from Oakland City Hall to Lake Merritt. Open Find My so I can look for my Route test AirPods. Open the stored call link for Route standup. Start a FaceTime video call to Mom. Get tomorrow's weather for Dublin, California, not Dublin in Ireland. Keep the results separate so I can tell what came from where.

**Required tools:** `get_directions`, `find_my_device`, `join_video_call`, `place_call`, `get_weather`.
**Checks:** mode transit; exact origin/destination app handoff only; do not report location or play a sound match title Route standup; use stored URL, never invent one audio_only false; exact fictional recipient location Dublin,CA; verify resolved place/date Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode transit; exact origin/destination
- Synthetic app launch with no location data
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Synthetic three-day forecast resolves Dublin California

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0516 · Natural compound request

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

I have a few things to finish. Check my Work calendar for tomorrow and list the reminders separately. Open walking directions from Oakland City Hall to Lake Merritt. Find coffee shops within two kilometers of Oakland City Hall. Tell me the current time difference between Tokyo and London. Start a FaceTime audio call to Mom. Keep the results separate so I can tell what came from where.

**Required tools:** `get_upcoming`, `get_directions`, `find_place`, `world_time`, `place_call`.
**Checks:** account Work; include tomorrow; distinguish events from reminders origin Oakland City Hall; destination Lake Merritt; mode walking what coffee; near Oakland City Hall; radius_km 2 place Tokyo; compare_to London; current offsets to exact fictional number; audio_only true; simulated native handoff Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Work and Personal calendars are synced; tomorrow has Design review at 14:00 and a separate due reminder
- Synthetic Maps launch, not navigation confirmation
- Synthetic map results with names, addresses, distances
- Per-run captured clock and known IANA zones
- No actual call; native scheme open intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0517 · Natural compound request

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

I have a few things to finish. Start a FaceTime video call to Mom. Find a 45-minute free slot tomorrow between 10 AM and 5 PM. Open Find My so I can look for my Route test AirPods. Give me today's briefing, including weather for Oakland, California. Estimate driving time from San Francisco to Oakland and say whether traffic is included. Keep the results separate so I can tell what came from where.

**Required tools:** `place_call`, `find_free_time`, `find_my_device`, `daily_brief`, `travel_time`.
**Checks:** audio_only false; exact fictional recipient minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions app handoff only; do not report location or play a sound location Oakland,CA; day briefing with activity; not exhaustive task inventory origin San Francisco; destination Oakland; free-flow disclaimer Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow
- Synthetic app launch with no location data
- Calendar, weather and recent-activity fixture responses are populated
- Synthetic driving route, 20 km and 25 minutes, no traffic

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0518 · Natural compound request

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

I have a few things to finish. Estimate driving time from San Francisco to Oakland and say whether traffic is included. Open Find My so I can look for my Route test AirPods. Start a FaceTime video call to Mom. Find coffee shops within two kilometers of Oakland City Hall. Find a 45-minute free slot tomorrow between 10 AM and 5 PM. Keep the results separate so I can tell what came from where.

**Required tools:** `travel_time`, `find_my_device`, `place_call`, `find_place`, `find_free_time`.
**Checks:** origin San Francisco; destination Oakland; free-flow disclaimer app handoff only; do not report location or play a sound audio_only false; exact fictional recipient what coffee; near Oakland City Hall; radius_km 2 minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic driving route, 20 km and 25 minutes, no traffic
- Synthetic app launch with no location data
- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Synthetic map results with names, addresses, distances
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0519 · Natural compound request

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

I have a few things to finish. Estimate driving time from San Francisco to Oakland and say whether traffic is included. Find coffee shops within two kilometers of Oakland City Hall. Start a FaceTime video call to Mom. Find today's sunrise and sunset for Seattle, Washington. Open Find My so I can look for my Route test AirPods. Keep the results separate so I can tell what came from where.

**Required tools:** `travel_time`, `find_place`, `place_call`, `astronomy`, `find_my_device`.
**Checks:** origin San Francisco; destination Oakland; free-flow disclaimer what coffee; near Oakland City Hall; radius_km 2 audio_only false; exact fictional recipient location Seattle,WA; actual current-day solar data app handoff only; do not report location or play a sound Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic driving route, 20 km and 25 minutes, no traffic
- Synthetic map results with names, addresses, distances
- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Synthetic sunrise/sunset with timezone
- Synthetic app launch with no location data

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0520 · Natural compound request

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

I have a few things to finish. Estimate driving time from San Francisco to Oakland and say whether traffic is included. Open the stored call link for Route standup. Open Find My so I can look for my Route test AirPods. Start a FaceTime video call to Mom. Tell me the current time difference between Tokyo and London. Keep the results separate so I can tell what came from where.

**Required tools:** `travel_time`, `join_video_call`, `find_my_device`, `place_call`, `world_time`.
**Checks:** origin San Francisco; destination Oakland; free-flow disclaimer match title Route standup; use stored URL, never invent one app handoff only; do not report location or play a sound audio_only false; exact fictional recipient place Tokyo; compare_to London; current offsets Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic driving route, 20 km and 25 minutes, no traffic
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- Synthetic app launch with no location data
- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Per-run captured clock and known IANA zones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0521 · Scoped execution

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

For these tasks, use only the named sources and targets: find today's sunrise and sunset for Seattle, Washington; then give me today's briefing, including weather for Oakland, California; then open transit directions from Oakland City Hall to Lake Merritt; then start a FaceTime video call to Mom; then estimate driving time from San Francisco to Oakland and say whether traffic is included. Leave everything else unchanged.

**Required tools:** `astronomy`, `daily_brief`, `get_directions`, `place_call`, `travel_time`.
**Ordering constraints:** `astronomy` before `daily_brief`; `daily_brief` before `get_directions`; `get_directions` before `place_call`; `place_call` before `travel_time`.
**Checks:** location Seattle,WA; actual current-day solar data location Oakland,CA; day briefing with activity; not exhaustive task inventory mode transit; exact origin/destination audio_only false; exact fictional recipient origin San Francisco; destination Oakland; free-flow disclaimer Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic sunrise/sunset with timezone
- Calendar, weather and recent-activity fixture responses are populated
- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode transit; exact origin/destination
- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Synthetic driving route, 20 km and 25 minutes, no traffic

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0522 · Scoped execution

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

For these tasks, use only the named sources and targets: find a 45-minute free slot tomorrow between 10 AM and 5 PM; then give me today's briefing, including weather for Oakland, California; then estimate driving time from San Francisco to Oakland and say whether traffic is included; then find coffee shops within two kilometers of Oakland City Hall; then start a FaceTime video call to Mom. Leave everything else unchanged.

**Required tools:** `find_free_time`, `daily_brief`, `travel_time`, `find_place`, `place_call`.
**Ordering constraints:** `find_free_time` before `daily_brief`; `daily_brief` before `travel_time`; `travel_time` before `find_place`; `find_place` before `place_call`.
**Checks:** minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions location Oakland,CA; day briefing with activity; not exhaustive task inventory origin San Francisco; destination Oakland; free-flow disclaimer what coffee; near Oakland City Hall; radius_km 2 audio_only false; exact fictional recipient Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow
- Calendar, weather and recent-activity fixture responses are populated
- Synthetic driving route, 20 km and 25 minutes, no traffic
- Synthetic map results with names, addresses, distances
- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0523 · Scoped execution

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

For these tasks, use only the named sources and targets: open Find My so I can look for my Route test AirPods; then find coffee shops within two kilometers of Oakland City Hall; then estimate driving time from San Francisco to Oakland and say whether traffic is included; then open transit directions from Oakland City Hall to Lake Merritt; then tell me the current time difference between Tokyo and London. Leave everything else unchanged.

**Required tools:** `find_my_device`, `find_place`, `travel_time`, `get_directions`, `world_time`.
**Ordering constraints:** `find_my_device` before `find_place`; `find_place` before `travel_time`; `travel_time` before `get_directions`; `get_directions` before `world_time`.
**Checks:** app handoff only; do not report location or play a sound what coffee; near Oakland City Hall; radius_km 2 origin San Francisco; destination Oakland; free-flow disclaimer mode transit; exact origin/destination place Tokyo; compare_to London; current offsets Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic app launch with no location data
- Synthetic map results with names, addresses, distances
- Synthetic driving route, 20 km and 25 minutes, no traffic
- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode transit; exact origin/destination
- Per-run captured clock and known IANA zones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0524 · Scoped execution

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

For these tasks, use only the named sources and targets: open Find My so I can look for my Route test AirPods; then open driving directions from Oakland City Hall to Lake Merritt; then give me today's briefing, including weather for Oakland, California; then estimate driving time from San Francisco to Oakland and say whether traffic is included; then tell me the current time difference between Tokyo and London. Leave everything else unchanged.

**Required tools:** `find_my_device`, `get_directions`, `daily_brief`, `travel_time`, `world_time`.
**Ordering constraints:** `find_my_device` before `get_directions`; `get_directions` before `daily_brief`; `daily_brief` before `travel_time`; `travel_time` before `world_time`.
**Checks:** app handoff only; do not report location or play a sound mode driving; exact origin/destination location Oakland,CA; day briefing with activity; not exhaustive task inventory origin San Francisco; destination Oakland; free-flow disclaimer place Tokyo; compare_to London; current offsets Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic app launch with no location data
- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode driving; exact origin/destination
- Calendar, weather and recent-activity fixture responses are populated
- Synthetic driving route, 20 km and 25 minutes, no traffic
- Per-run captured clock and known IANA zones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0525 · Scoped execution

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

For these tasks, use only the named sources and targets: find coffee shops within two kilometers of Oakland City Hall; then get tomorrow's weather for Dublin, California, not Dublin in Ireland; then tell me the current time difference between Tokyo and London; then estimate driving time from San Francisco to Oakland and say whether traffic is included; then give me today's briefing, including weather for Oakland, California. Leave everything else unchanged.

**Required tools:** `find_place`, `get_weather`, `world_time`, `travel_time`, `daily_brief`.
**Ordering constraints:** `find_place` before `get_weather`; `get_weather` before `world_time`; `world_time` before `travel_time`; `travel_time` before `daily_brief`.
**Checks:** what coffee; near Oakland City Hall; radius_km 2 location Dublin,CA; verify resolved place/date place Tokyo; compare_to London; current offsets origin San Francisco; destination Oakland; free-flow disclaimer location Oakland,CA; day briefing with activity; not exhaustive task inventory Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic map results with names, addresses, distances
- Synthetic three-day forecast resolves Dublin California
- Per-run captured clock and known IANA zones
- Synthetic driving route, 20 km and 25 minutes, no traffic
- Calendar, weather and recent-activity fixture responses are populated

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0526 · Scoped execution

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

For these tasks, use only the named sources and targets: find coffee shops within two kilometers of Oakland City Hall; then estimate driving time from San Francisco to Oakland and say whether traffic is included; then tell me the current time difference between Tokyo and London; then open driving directions from Oakland City Hall to Lake Merritt; then open Find My so I can look for my Route test AirPods. Leave everything else unchanged.

**Required tools:** `find_place`, `travel_time`, `world_time`, `get_directions`, `find_my_device`.
**Ordering constraints:** `find_place` before `travel_time`; `travel_time` before `world_time`; `world_time` before `get_directions`; `get_directions` before `find_my_device`.
**Checks:** what coffee; near Oakland City Hall; radius_km 2 origin San Francisco; destination Oakland; free-flow disclaimer place Tokyo; compare_to London; current offsets mode driving; exact origin/destination app handoff only; do not report location or play a sound Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic map results with names, addresses, distances
- Synthetic driving route, 20 km and 25 minutes, no traffic
- Per-run captured clock and known IANA zones
- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode driving; exact origin/destination
- Synthetic app launch with no location data

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0527 · Scoped execution

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

For these tasks, use only the named sources and targets: open transit directions from Oakland City Hall to Lake Merritt; then find coffee shops within two kilometers of Oakland City Hall; then open Find My so I can look for my Route test AirPods; then start a FaceTime video call to Mom; then tell me the current time difference between Tokyo and London. Leave everything else unchanged.

**Required tools:** `get_directions`, `find_place`, `find_my_device`, `place_call`, `world_time`.
**Ordering constraints:** `get_directions` before `find_place`; `find_place` before `find_my_device`; `find_my_device` before `place_call`; `place_call` before `world_time`.
**Checks:** mode transit; exact origin/destination what coffee; near Oakland City Hall; radius_km 2 app handoff only; do not report location or play a sound audio_only false; exact fictional recipient place Tokyo; compare_to London; current offsets Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode transit; exact origin/destination
- Synthetic map results with names, addresses, distances
- Synthetic app launch with no location data
- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Per-run captured clock and known IANA zones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0528 · Scoped execution

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

For these tasks, use only the named sources and targets: open the stored call link for Route standup; then estimate driving time from San Francisco to Oakland and say whether traffic is included; then find today's sunrise and sunset for Seattle, Washington; then give me today's briefing, including weather for Oakland, California; then find coffee shops within two kilometers of Oakland City Hall. Leave everything else unchanged.

**Required tools:** `join_video_call`, `travel_time`, `astronomy`, `daily_brief`, `find_place`.
**Ordering constraints:** `join_video_call` before `travel_time`; `travel_time` before `astronomy`; `astronomy` before `daily_brief`; `daily_brief` before `find_place`.
**Checks:** match title Route standup; use stored URL, never invent one origin San Francisco; destination Oakland; free-flow disclaimer location Seattle,WA; actual current-day solar data location Oakland,CA; day briefing with activity; not exhaustive task inventory what coffee; near Oakland City Hall; radius_km 2 Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- Synthetic driving route, 20 km and 25 minutes, no traffic
- Synthetic sunrise/sunset with timezone
- Calendar, weather and recent-activity fixture responses are populated
- Synthetic map results with names, addresses, distances

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0529 · Scoped execution

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

For these tasks, use only the named sources and targets: start a FaceTime audio call to Mom; then give me today's briefing, including weather for Oakland, California; then open walking directions from Oakland City Hall to Lake Merritt; then open Find My so I can look for my Route test AirPods; then estimate driving time from San Francisco to Oakland and say whether traffic is included. Leave everything else unchanged.

**Required tools:** `place_call`, `daily_brief`, `get_directions`, `find_my_device`, `travel_time`.
**Ordering constraints:** `place_call` before `daily_brief`; `daily_brief` before `get_directions`; `get_directions` before `find_my_device`; `find_my_device` before `travel_time`.
**Checks:** to exact fictional number; audio_only true; simulated native handoff location Oakland,CA; day briefing with activity; not exhaustive task inventory origin Oakland City Hall; destination Lake Merritt; mode walking app handoff only; do not report location or play a sound origin San Francisco; destination Oakland; free-flow disclaimer Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- No actual call; native scheme open intercepted
- Calendar, weather and recent-activity fixture responses are populated
- Synthetic Maps launch, not navigation confirmation
- Synthetic app launch with no location data
- Synthetic driving route, 20 km and 25 minutes, no traffic

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0530 · Scoped execution

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

For these tasks, use only the named sources and targets: start a FaceTime video call to Mom; then give me today's briefing, including weather for Oakland, California; then tell me the current time difference between Tokyo and London; then find coffee shops within two kilometers of Oakland City Hall; then open Find My so I can look for my Route test AirPods. Leave everything else unchanged.

**Required tools:** `place_call`, `daily_brief`, `world_time`, `find_place`, `find_my_device`.
**Ordering constraints:** `place_call` before `daily_brief`; `daily_brief` before `world_time`; `world_time` before `find_place`; `find_place` before `find_my_device`.
**Checks:** audio_only false; exact fictional recipient location Oakland,CA; day briefing with activity; not exhaustive task inventory place Tokyo; compare_to London; current offsets what coffee; near Oakland City Hall; radius_km 2 app handoff only; do not report location or play a sound Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Calendar, weather and recent-activity fixture responses are populated
- Per-run captured clock and known IANA zones
- Synthetic map results with names, addresses, distances
- Synthetic app launch with no location data

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0531 · Late constraints

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Find today's sunrise and sunset for Seattle, Washington. Open Find My so I can look for my Route test AirPods. Tell me the current time difference between Tokyo and London. Find a 45-minute free slot tomorrow between 10 AM and 5 PM. Give me today's briefing, including weather for Oakland, California. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `astronomy`, `find_my_device`, `world_time`, `find_free_time`, `daily_brief`.
**Checks:** location Seattle,WA; actual current-day solar data app handoff only; do not report location or play a sound place Tokyo; compare_to London; current offsets minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions location Oakland,CA; day briefing with activity; not exhaustive task inventory Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic sunrise/sunset with timezone
- Synthetic app launch with no location data
- Per-run captured clock and known IANA zones
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow
- Calendar, weather and recent-activity fixture responses are populated

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0532 · Late constraints

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Give me today's briefing, including weather for Oakland, California. Start a FaceTime video call to Mom. Open Find My so I can look for my Route test AirPods. Open the stored call link for Route standup. Estimate driving time from San Francisco to Oakland and say whether traffic is included. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `daily_brief`, `place_call`, `find_my_device`, `join_video_call`, `travel_time`.
**Checks:** location Oakland,CA; day briefing with activity; not exhaustive task inventory audio_only false; exact fictional recipient app handoff only; do not report location or play a sound match title Route standup; use stored URL, never invent one origin San Francisco; destination Oakland; free-flow disclaimer Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Calendar, weather and recent-activity fixture responses are populated
- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Synthetic app launch with no location data
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- Synthetic driving route, 20 km and 25 minutes, no traffic

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0533 · Late constraints

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Find a 45-minute free slot tomorrow between 10 AM and 5 PM. Open the stored call link for Route standup. Find coffee shops within two kilometers of Oakland City Hall. Give me today's briefing, including weather for Oakland, California. Tell me the current time difference between Tokyo and London. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `find_free_time`, `join_video_call`, `find_place`, `daily_brief`, `world_time`.
**Checks:** minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions match title Route standup; use stored URL, never invent one what coffee; near Oakland City Hall; radius_km 2 location Oakland,CA; day briefing with activity; not exhaustive task inventory place Tokyo; compare_to London; current offsets Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- Synthetic map results with names, addresses, distances
- Calendar, weather and recent-activity fixture responses are populated
- Per-run captured clock and known IANA zones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0534 · Late constraints

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Open transit directions from Oakland City Hall to Lake Merritt. Open the stored call link for Route standup. Open Find My so I can look for my Route test AirPods. Tell me the current time difference between Tokyo and London. Find a 45-minute free slot tomorrow between 10 AM and 5 PM. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `get_directions`, `join_video_call`, `find_my_device`, `world_time`, `find_free_time`.
**Checks:** mode transit; exact origin/destination match title Route standup; use stored URL, never invent one app handoff only; do not report location or play a sound place Tokyo; compare_to London; current offsets minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode transit; exact origin/destination
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- Synthetic app launch with no location data
- Per-run captured clock and known IANA zones
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0535 · Late constraints

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Open driving directions from Oakland City Hall to Lake Merritt. Open the stored call link for Route standup. Estimate driving time from San Francisco to Oakland and say whether traffic is included. Start a FaceTime video call to Mom. Tell me the current time difference between Tokyo and London. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `get_directions`, `join_video_call`, `travel_time`, `place_call`, `world_time`.
**Checks:** mode driving; exact origin/destination match title Route standup; use stored URL, never invent one origin San Francisco; destination Oakland; free-flow disclaimer audio_only false; exact fictional recipient place Tokyo; compare_to London; current offsets Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode driving; exact origin/destination
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- Synthetic driving route, 20 km and 25 minutes, no traffic
- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Per-run captured clock and known IANA zones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0536 · Late constraints

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Open transit directions from Oakland City Hall to Lake Merritt. Start a FaceTime video call to Mom. Open Find My so I can look for my Route test AirPods. Estimate driving time from San Francisco to Oakland and say whether traffic is included. Find coffee shops within two kilometers of Oakland City Hall. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `get_directions`, `place_call`, `find_my_device`, `travel_time`, `find_place`.
**Checks:** mode transit; exact origin/destination audio_only false; exact fictional recipient app handoff only; do not report location or play a sound origin San Francisco; destination Oakland; free-flow disclaimer what coffee; near Oakland City Hall; radius_km 2 Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode transit; exact origin/destination
- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Synthetic app launch with no location data
- Synthetic driving route, 20 km and 25 minutes, no traffic
- Synthetic map results with names, addresses, distances

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0537 · Late constraints

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Start a FaceTime video call to Mom. Open Find My so I can look for my Route test AirPods. Get tomorrow's weather for Dublin, California, not Dublin in Ireland. Open driving directions from Oakland City Hall to Lake Merritt. Tell me the current time difference between Tokyo and London. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `place_call`, `find_my_device`, `get_weather`, `get_directions`, `world_time`.
**Checks:** audio_only false; exact fictional recipient app handoff only; do not report location or play a sound location Dublin,CA; verify resolved place/date mode driving; exact origin/destination place Tokyo; compare_to London; current offsets Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Synthetic app launch with no location data
- Synthetic three-day forecast resolves Dublin California
- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode driving; exact origin/destination
- Per-run captured clock and known IANA zones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0538 · Late constraints

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Estimate driving time from San Francisco to Oakland and say whether traffic is included. Find coffee shops within two kilometers of Oakland City Hall. Open Find My so I can look for my Route test AirPods. Tell me the current time difference between Tokyo and London. Start a FaceTime audio call to Mom. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `travel_time`, `find_place`, `find_my_device`, `world_time`, `place_call`.
**Checks:** origin San Francisco; destination Oakland; free-flow disclaimer what coffee; near Oakland City Hall; radius_km 2 app handoff only; do not report location or play a sound place Tokyo; compare_to London; current offsets to exact fictional number; audio_only true; simulated native handoff Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic driving route, 20 km and 25 minutes, no traffic
- Synthetic map results with names, addresses, distances
- Synthetic app launch with no location data
- Per-run captured clock and known IANA zones
- No actual call; native scheme open intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0539 · Late constraints

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Estimate driving time from San Francisco to Oakland and say whether traffic is included. Start a FaceTime video call to Mom. Find coffee shops within two kilometers of Oakland City Hall. Open transit directions from Oakland City Hall to Lake Merritt. Give me today's briefing, including weather for Oakland, California. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `travel_time`, `place_call`, `find_place`, `get_directions`, `daily_brief`.
**Checks:** origin San Francisco; destination Oakland; free-flow disclaimer audio_only false; exact fictional recipient what coffee; near Oakland City Hall; radius_km 2 mode transit; exact origin/destination location Oakland,CA; day briefing with activity; not exhaustive task inventory Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic driving route, 20 km and 25 minutes, no traffic
- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Synthetic map results with names, addresses, distances
- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode transit; exact origin/destination
- Calendar, weather and recent-activity fixture responses are populated

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0540 · Late constraints

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Tell me the current time difference between Tokyo and London. Open transit directions from Oakland City Hall to Lake Merritt. Get tomorrow's weather for Dublin, California, not Dublin in Ireland. Open the stored call link for Route standup. Find a 45-minute free slot tomorrow between 10 AM and 5 PM. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `world_time`, `get_directions`, `get_weather`, `join_video_call`, `find_free_time`.
**Checks:** place Tokyo; compare_to London; current offsets mode transit; exact origin/destination location Dublin,CA; verify resolved place/date match title Route standup; use stored URL, never invent one minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Per-run captured clock and known IANA zones
- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode transit; exact origin/destination
- Synthetic three-day forecast resolves Dublin California
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0541 · Colloquial with interruptions

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Could you give me today's briefing, including weather for Oakland, California; then tell me the current time difference between Tokyo and London; then start a FaceTime video call to Mom; then estimate driving time from San Francisco to Oakland and say whether traffic is included; then open transit directions from Oakland City Hall to Lake Merritt? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `daily_brief`, `world_time`, `place_call`, `travel_time`, `get_directions`.
**Ordering constraints:** `daily_brief` before `world_time`; `world_time` before `place_call`; `place_call` before `travel_time`; `travel_time` before `get_directions`.
**Checks:** location Oakland,CA; day briefing with activity; not exhaustive task inventory place Tokyo; compare_to London; current offsets audio_only false; exact fictional recipient origin San Francisco; destination Oakland; free-flow disclaimer mode transit; exact origin/destination Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Calendar, weather and recent-activity fixture responses are populated
- Per-run captured clock and known IANA zones
- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Synthetic driving route, 20 km and 25 minutes, no traffic
- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode transit; exact origin/destination

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0542 · Colloquial with interruptions

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Could you give me today's briefing, including weather for Oakland, California; then tell me the current time difference between Tokyo and London; then estimate driving time from San Francisco to Oakland and say whether traffic is included; then start a FaceTime video call to Mom; then open the stored call link for Route standup? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `daily_brief`, `world_time`, `travel_time`, `place_call`, `join_video_call`.
**Ordering constraints:** `daily_brief` before `world_time`; `world_time` before `travel_time`; `travel_time` before `place_call`; `place_call` before `join_video_call`.
**Checks:** location Oakland,CA; day briefing with activity; not exhaustive task inventory place Tokyo; compare_to London; current offsets origin San Francisco; destination Oakland; free-flow disclaimer audio_only false; exact fictional recipient match title Route standup; use stored URL, never invent one Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Calendar, weather and recent-activity fixture responses are populated
- Per-run captured clock and known IANA zones
- Synthetic driving route, 20 km and 25 minutes, no traffic
- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0543 · Colloquial with interruptions

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Could you open Find My so I can look for my Route test AirPods; then open walking directions from Oakland City Hall to Lake Merritt; then find coffee shops within two kilometers of Oakland City Hall; then start a FaceTime audio call to Mom; then give me today's briefing, including weather for Oakland, California? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `find_my_device`, `get_directions`, `find_place`, `place_call`, `daily_brief`.
**Ordering constraints:** `find_my_device` before `get_directions`; `get_directions` before `find_place`; `find_place` before `place_call`; `place_call` before `daily_brief`.
**Checks:** app handoff only; do not report location or play a sound origin Oakland City Hall; destination Lake Merritt; mode walking what coffee; near Oakland City Hall; radius_km 2 to exact fictional number; audio_only true; simulated native handoff location Oakland,CA; day briefing with activity; not exhaustive task inventory Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic app launch with no location data
- Synthetic Maps launch, not navigation confirmation
- Synthetic map results with names, addresses, distances
- No actual call; native scheme open intercepted
- Calendar, weather and recent-activity fixture responses are populated

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0544 · Colloquial with interruptions

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Could you find coffee shops within two kilometers of Oakland City Hall; then find a 45-minute free slot tomorrow between 10 AM and 5 PM; then estimate driving time from San Francisco to Oakland and say whether traffic is included; then get tomorrow's weather for Dublin, California, not Dublin in Ireland; then open driving directions from Oakland City Hall to Lake Merritt? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `find_place`, `find_free_time`, `travel_time`, `get_weather`, `get_directions`.
**Ordering constraints:** `find_place` before `find_free_time`; `find_free_time` before `travel_time`; `travel_time` before `get_weather`; `get_weather` before `get_directions`.
**Checks:** what coffee; near Oakland City Hall; radius_km 2 minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions origin San Francisco; destination Oakland; free-flow disclaimer location Dublin,CA; verify resolved place/date mode driving; exact origin/destination Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic map results with names, addresses, distances
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow
- Synthetic driving route, 20 km and 25 minutes, no traffic
- Synthetic three-day forecast resolves Dublin California
- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode driving; exact origin/destination

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0545 · Colloquial with interruptions

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Could you find coffee shops within two kilometers of Oakland City Hall; then open Find My so I can look for my Route test AirPods; then estimate driving time from San Francisco to Oakland and say whether traffic is included; then start a FaceTime video call to Mom; then give me today's briefing, including weather for Oakland, California? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `find_place`, `find_my_device`, `travel_time`, `place_call`, `daily_brief`.
**Ordering constraints:** `find_place` before `find_my_device`; `find_my_device` before `travel_time`; `travel_time` before `place_call`; `place_call` before `daily_brief`.
**Checks:** what coffee; near Oakland City Hall; radius_km 2 app handoff only; do not report location or play a sound origin San Francisco; destination Oakland; free-flow disclaimer audio_only false; exact fictional recipient location Oakland,CA; day briefing with activity; not exhaustive task inventory Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic map results with names, addresses, distances
- Synthetic app launch with no location data
- Synthetic driving route, 20 km and 25 minutes, no traffic
- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Calendar, weather and recent-activity fixture responses are populated

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0546 · Colloquial with interruptions

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Could you open transit directions from Oakland City Hall to Lake Merritt; then start a FaceTime video call to Mom; then give me today's briefing, including weather for Oakland, California; then find coffee shops within two kilometers of Oakland City Hall; then tell me the current time difference between Tokyo and London? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `get_directions`, `place_call`, `daily_brief`, `find_place`, `world_time`.
**Ordering constraints:** `get_directions` before `place_call`; `place_call` before `daily_brief`; `daily_brief` before `find_place`; `find_place` before `world_time`.
**Checks:** mode transit; exact origin/destination audio_only false; exact fictional recipient location Oakland,CA; day briefing with activity; not exhaustive task inventory what coffee; near Oakland City Hall; radius_km 2 place Tokyo; compare_to London; current offsets Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode transit; exact origin/destination
- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Calendar, weather and recent-activity fixture responses are populated
- Synthetic map results with names, addresses, distances
- Per-run captured clock and known IANA zones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0547 · Colloquial with interruptions

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Could you open driving directions from Oakland City Hall to Lake Merritt; then tell me the current time difference between Tokyo and London; then give me today's briefing, including weather for Oakland, California; then start a FaceTime video call to Mom; then estimate driving time from San Francisco to Oakland and say whether traffic is included? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `get_directions`, `world_time`, `daily_brief`, `place_call`, `travel_time`.
**Ordering constraints:** `get_directions` before `world_time`; `world_time` before `daily_brief`; `daily_brief` before `place_call`; `place_call` before `travel_time`.
**Checks:** mode driving; exact origin/destination place Tokyo; compare_to London; current offsets location Oakland,CA; day briefing with activity; not exhaustive task inventory audio_only false; exact fictional recipient origin San Francisco; destination Oakland; free-flow disclaimer Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Maps launch, not navigation confirmation; variant-specific state must satisfy: mode driving; exact origin/destination
- Per-run captured clock and known IANA zones
- Calendar, weather and recent-activity fixture responses are populated
- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Synthetic driving route, 20 km and 25 minutes, no traffic

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0548 · Colloquial with interruptions

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Could you open the stored call link for Route standup; then find today's sunrise and sunset for Seattle, Washington; then get tomorrow's weather for Dublin, California, not Dublin in Ireland; then start a FaceTime video call to Mom; then estimate driving time from San Francisco to Oakland and say whether traffic is included? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `join_video_call`, `astronomy`, `get_weather`, `place_call`, `travel_time`.
**Ordering constraints:** `join_video_call` before `astronomy`; `astronomy` before `get_weather`; `get_weather` before `place_call`; `place_call` before `travel_time`.
**Checks:** match title Route standup; use stored URL, never invent one location Seattle,WA; actual current-day solar data location Dublin,CA; verify resolved place/date audio_only false; exact fictional recipient origin San Francisco; destination Oakland; free-flow disclaimer Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- Synthetic sunrise/sunset with timezone
- Synthetic three-day forecast resolves Dublin California
- No actual call; native scheme open intercepted; variant-specific state must satisfy: audio_only false; exact fictional recipient
- Synthetic driving route, 20 km and 25 minutes, no traffic

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0549 · Colloquial with interruptions

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Could you open the stored call link for Route standup; then get tomorrow's weather for Dublin, California, not Dublin in Ireland; then find a 45-minute free slot tomorrow between 10 AM and 5 PM; then find today's sunrise and sunset for Seattle, Washington; then find coffee shops within two kilometers of Oakland City Hall? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `join_video_call`, `get_weather`, `find_free_time`, `astronomy`, `find_place`.
**Ordering constraints:** `join_video_call` before `get_weather`; `get_weather` before `find_free_time`; `find_free_time` before `astronomy`; `astronomy` before `find_place`.
**Checks:** match title Route standup; use stored URL, never invent one location Dublin,CA; verify resolved place/date minutes 45; day_start 10; day_end 17; filter tomorrow; disclose duration assumptions location Seattle,WA; actual current-day solar data what coffee; near Oakland City Hall; radius_km 2 Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- Synthetic three-day forecast resolves Dublin California
- Synced schedule has one-hour timed blocks and no all-day blocker tomorrow
- Synthetic sunrise/sunset with timezone
- Synthetic map results with names, addresses, distances

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0550 · Colloquial with interruptions

**Focus:** Combine personal schedules, directions, location-aware facts, and manual app handoffs.

**Prompt:**

Could you open the stored call link for Route standup; then estimate driving time from San Francisco to Oakland and say whether traffic is included; then tell me the current time difference between Tokyo and London; then give me today's briefing, including weather for Oakland, California; then find today's sunrise and sunset for Seattle, Washington? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `join_video_call`, `travel_time`, `world_time`, `daily_brief`, `astronomy`.
**Ordering constraints:** `join_video_call` before `travel_time`; `travel_time` before `world_time`; `world_time` before `daily_brief`; `daily_brief` before `astronomy`.
**Checks:** match title Route standup; use stored URL, never invent one origin San Francisco; destination Oakland; free-flow disclaimer place Tokyo; compare_to London; current offsets location Oakland,CA; day briefing with activity; not exhaustive task inventory location Seattle,WA; actual current-day solar data Combine personal schedules, directions, location-aware facts, and manual app handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Route standup is today and has https://meet.example.test/route-standup; opening is intercepted
- Synthetic driving route, 20 km and 25 minutes, no traffic
- Per-run captured clock and known IANA zones
- Calendar, weather and recent-activity fixture responses are populated
- Synthetic sunrise/sunset with timezone

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.
