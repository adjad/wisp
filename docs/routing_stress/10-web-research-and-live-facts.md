# Review only — no tests run

## 10. Web research and live facts

Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

### WRS-0451 · Explicit sequence

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Please do these in this order: check the AQI in Oakland, California; then show NVIDIA's price change over exactly three weeks; then show Oakland's hourly chance of rain over the next six hours; then look up a reference summary of the Golden Gate Bridge; then check the latest NBA scoreboard.

**Required tools:** `air_quality`, `get_stock_price`, `rain_radar`, `wikipedia_summary`, `get_sports_scores`.
**Ordering constraints:** `air_quality` before `get_stock_price`; `get_stock_price` before `rain_radar`; `rain_radar` before `wikipedia_summary`; `wikipedia_summary` before `get_sports_scores`.
**Checks:** location Oakland,CA; use current provider data symbols array containing NVIDIA only; period 3 weeks location Oakland,CA; hours 6; text forecast not radar image topic Golden Gate Bridge; no invented quote team_or_league NBA; no unrelated league Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic AQI 42 and measured pollutant fields
- Synthetic quote/history provider includes actual start/end dates
- Six synthetic hourly precipitation entries
- Synthetic reference extract and title
- Synthetic current NBA scoreboard

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0452 · Explicit sequence

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Please do these in this order: check the AQI in Oakland, California; then look up a reference summary of the Golden Gate Bridge; then find recipe ideas that use chickpeas; then get current prices for NVIDIA and AMD, and no other stocks; then look up the dictionary meaning of serendipity.

**Required tools:** `air_quality`, `wikipedia_summary`, `recipe_lookup`, `get_stock_price`, `define_word`.
**Ordering constraints:** `air_quality` before `wikipedia_summary`; `wikipedia_summary` before `recipe_lookup`; `recipe_lookup` before `get_stock_price`; `get_stock_price` before `define_word`.
**Checks:** location Oakland,CA; use current provider data topic Golden Gate Bridge; no invented quote ingredient chickpeas; names before claiming full instructions symbols array NVIDIA and AMD; no historical period word serendipity; dictionary lookup Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic AQI 42 and measured pollutant fields
- Synthetic reference extract and title
- Synthetic recipe provider response; no purchase; variant-specific state must satisfy: ingredient chickpeas; names before claiming full instructions
- Synthetic quote/history provider includes actual start/end dates; variant-specific state must satisfy: symbols array NVIDIA and AMD; no historical period
- Synthetic English dictionary entry

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0453 · Explicit sequence

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Please do these in this order: find today's sunrise and sunset for Seattle, Washington; then check the latest NBA scoreboard; then check the AQI in Oakland, California; then read https://museum.example.test/visitors and quote its opening hours; then show Oakland's hourly chance of rain over the next six hours.

**Required tools:** `astronomy`, `get_sports_scores`, `air_quality`, `web_fetch`, `rain_radar`.
**Ordering constraints:** `astronomy` before `get_sports_scores`; `get_sports_scores` before `air_quality`; `air_quality` before `web_fetch`; `web_fetch` before `rain_radar`.
**Checks:** location Seattle,WA; actual current-day solar data team_or_league NBA; no unrelated league location Oakland,CA; use current provider data exact supplied URL; GET; no shell location Oakland,CA; hours 6; text forecast not radar image Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic sunrise/sunset with timezone
- Synthetic current NBA scoreboard
- Synthetic AQI 42 and measured pollutant fields
- Synthetic page says open Tuesday-Sunday 10 AM-5 PM; no auth
- Six synthetic hourly precipitation entries

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0454 · Explicit sequence

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Please do these in this order: look up the dictionary meaning of serendipity; then get tomorrow's weather for Dublin, California, not Dublin in Ireland; then show Oakland's hourly chance of rain over the next six hours; then find recipe ideas that use chickpeas; then search the public web for the current Route Museum visitor policy.

**Required tools:** `define_word`, `get_weather`, `rain_radar`, `recipe_lookup`, `web_search`.
**Ordering constraints:** `define_word` before `get_weather`; `get_weather` before `rain_radar`; `rain_radar` before `recipe_lookup`; `recipe_lookup` before `web_search`.
**Checks:** word serendipity; dictionary lookup location Dublin,CA; verify resolved place/date location Oakland,CA; hours 6; text forecast not radar image ingredient chickpeas; names before claiming full instructions query current Route Museum visitor policy; real result URLs Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic English dictionary entry
- Synthetic three-day forecast resolves Dublin California
- Six synthetic hourly precipitation entries
- Synthetic recipe provider response; no purchase; variant-specific state must satisfy: ingredient chickpeas; names before claiming full instructions
- Synthetic search hit https://museum.example.test/visitors with short snippet

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0455 · Explicit sequence

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Please do these in this order: look up the dictionary meaning of serendipity; then show Oakland's hourly chance of rain over the next six hours; then check active severe-weather warnings for Miami, Florida; then check the latest NBA scoreboard; then look up a reference summary of the Golden Gate Bridge.

**Required tools:** `define_word`, `rain_radar`, `weather_alerts`, `get_sports_scores`, `wikipedia_summary`.
**Ordering constraints:** `define_word` before `rain_radar`; `rain_radar` before `weather_alerts`; `weather_alerts` before `get_sports_scores`; `get_sports_scores` before `wikipedia_summary`.
**Checks:** word serendipity; dictionary lookup location Oakland,CA; hours 6; text forecast not radar image US location Miami,FL; no false all-clear on fetch failure team_or_league NBA; no unrelated league topic Golden Gate Bridge; no invented quote Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic English dictionary entry
- Six synthetic hourly precipitation entries
- Synthetic NWS feed with explicit US geocode
- Synthetic current NBA scoreboard
- Synthetic reference extract and title

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0456 · Explicit sequence

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Please do these in this order: get current prices for NVIDIA and AMD, and no other stocks; then check the AQI in Oakland, California; then read https://museum.example.test/visitors and quote its opening hours; then show Oakland's hourly chance of rain over the next six hours; then find recipe ideas that use chickpeas.

**Required tools:** `get_stock_price`, `air_quality`, `web_fetch`, `rain_radar`, `recipe_lookup`.
**Ordering constraints:** `get_stock_price` before `air_quality`; `air_quality` before `web_fetch`; `web_fetch` before `rain_radar`; `rain_radar` before `recipe_lookup`.
**Checks:** symbols array NVIDIA and AMD; no historical period location Oakland,CA; use current provider data exact supplied URL; GET; no shell location Oakland,CA; hours 6; text forecast not radar image ingredient chickpeas; names before claiming full instructions Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic quote/history provider includes actual start/end dates; variant-specific state must satisfy: symbols array NVIDIA and AMD; no historical period
- Synthetic AQI 42 and measured pollutant fields
- Synthetic page says open Tuesday-Sunday 10 AM-5 PM; no auth
- Six synthetic hourly precipitation entries
- Synthetic recipe provider response; no purchase; variant-specific state must satisfy: ingredient chickpeas; names before claiming full instructions

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0457 · Explicit sequence

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Please do these in this order: show Oakland's hourly chance of rain over the next six hours; then look up the dictionary meaning of serendipity; then check the latest NBA scoreboard; then get tomorrow's weather for Dublin, California, not Dublin in Ireland; then look up a reference summary of the Golden Gate Bridge.

**Required tools:** `rain_radar`, `define_word`, `get_sports_scores`, `get_weather`, `wikipedia_summary`.
**Ordering constraints:** `rain_radar` before `define_word`; `define_word` before `get_sports_scores`; `get_sports_scores` before `get_weather`; `get_weather` before `wikipedia_summary`.
**Checks:** location Oakland,CA; hours 6; text forecast not radar image word serendipity; dictionary lookup team_or_league NBA; no unrelated league location Dublin,CA; verify resolved place/date topic Golden Gate Bridge; no invented quote Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Six synthetic hourly precipitation entries
- Synthetic English dictionary entry
- Synthetic current NBA scoreboard
- Synthetic three-day forecast resolves Dublin California
- Synthetic reference extract and title

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0458 · Explicit sequence

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Please do these in this order: show Oakland's hourly chance of rain over the next six hours; then check active severe-weather warnings for Miami, Florida; then find recipe ideas that use chickpeas; then read https://museum.example.test/visitors and quote its opening hours; then find today's sunrise and sunset for Seattle, Washington.

**Required tools:** `rain_radar`, `weather_alerts`, `recipe_lookup`, `web_fetch`, `astronomy`.
**Ordering constraints:** `rain_radar` before `weather_alerts`; `weather_alerts` before `recipe_lookup`; `recipe_lookup` before `web_fetch`; `web_fetch` before `astronomy`.
**Checks:** location Oakland,CA; hours 6; text forecast not radar image US location Miami,FL; no false all-clear on fetch failure ingredient chickpeas; names before claiming full instructions exact supplied URL; GET; no shell location Seattle,WA; actual current-day solar data Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Six synthetic hourly precipitation entries
- Synthetic NWS feed with explicit US geocode
- Synthetic recipe provider response; no purchase; variant-specific state must satisfy: ingredient chickpeas; names before claiming full instructions
- Synthetic page says open Tuesday-Sunday 10 AM-5 PM; no auth
- Synthetic sunrise/sunset with timezone

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0459 · Explicit sequence

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Please do these in this order: read https://museum.example.test/visitors and quote its opening hours; then find today's sunrise and sunset for Seattle, Washington; then look up the dictionary meaning of serendipity; then look up a reference summary of the Golden Gate Bridge; then get tomorrow's weather for Dublin, California, not Dublin in Ireland.

**Required tools:** `web_fetch`, `astronomy`, `define_word`, `wikipedia_summary`, `get_weather`.
**Ordering constraints:** `web_fetch` before `astronomy`; `astronomy` before `define_word`; `define_word` before `wikipedia_summary`; `wikipedia_summary` before `get_weather`.
**Checks:** exact supplied URL; GET; no shell location Seattle,WA; actual current-day solar data word serendipity; dictionary lookup topic Golden Gate Bridge; no invented quote location Dublin,CA; verify resolved place/date Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic page says open Tuesday-Sunday 10 AM-5 PM; no auth
- Synthetic sunrise/sunset with timezone
- Synthetic English dictionary entry
- Synthetic reference extract and title
- Synthetic three-day forecast resolves Dublin California

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0460 · Explicit sequence

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Please do these in this order: search the public web for the current Route Museum visitor policy; then read https://museum.example.test/visitors and quote its opening hours; then get current prices for NVIDIA and AMD, and no other stocks; then find today's sunrise and sunset for Seattle, Washington; then look up the dictionary meaning of serendipity.

**Required tools:** `web_search`, `web_fetch`, `get_stock_price`, `astronomy`, `define_word`.
**Ordering constraints:** `web_search` before `web_fetch`; `web_fetch` before `get_stock_price`; `get_stock_price` before `astronomy`; `astronomy` before `define_word`.
**Checks:** query current Route Museum visitor policy; real result URLs exact supplied URL; GET; no shell symbols array NVIDIA and AMD; no historical period location Seattle,WA; actual current-day solar data word serendipity; dictionary lookup Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic search hit https://museum.example.test/visitors with short snippet
- Synthetic page says open Tuesday-Sunday 10 AM-5 PM; no auth
- Synthetic quote/history provider includes actual start/end dates; variant-specific state must satisfy: symbols array NVIDIA and AMD; no historical period
- Synthetic sunrise/sunset with timezone
- Synthetic English dictionary entry

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0461 · Natural compound request

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

I have a few things to finish. Check the AQI in Oakland, California. Look up the dictionary meaning of serendipity. Search the public web for the current Route Museum visitor policy. Check active severe-weather warnings for Miami, Florida. Get current prices for NVIDIA and AMD, and no other stocks. Keep the results separate so I can tell what came from where.

**Required tools:** `air_quality`, `define_word`, `web_search`, `weather_alerts`, `get_stock_price`.
**Checks:** location Oakland,CA; use current provider data word serendipity; dictionary lookup query current Route Museum visitor policy; real result URLs US location Miami,FL; no false all-clear on fetch failure symbols array NVIDIA and AMD; no historical period Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic AQI 42 and measured pollutant fields
- Synthetic English dictionary entry
- Synthetic search hit https://museum.example.test/visitors with short snippet
- Synthetic NWS feed with explicit US geocode
- Synthetic quote/history provider includes actual start/end dates; variant-specific state must satisfy: symbols array NVIDIA and AMD; no historical period

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0462 · Natural compound request

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

I have a few things to finish. Check the AQI in Oakland, California. Get tomorrow's weather for Dublin, California, not Dublin in Ireland. Look up a reference summary of the Golden Gate Bridge. Check the latest NBA scoreboard. Get current prices for NVIDIA and AMD, and no other stocks. Keep the results separate so I can tell what came from where.

**Required tools:** `air_quality`, `get_weather`, `wikipedia_summary`, `get_sports_scores`, `get_stock_price`.
**Checks:** location Oakland,CA; use current provider data location Dublin,CA; verify resolved place/date topic Golden Gate Bridge; no invented quote team_or_league NBA; no unrelated league symbols array NVIDIA and AMD; no historical period Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic AQI 42 and measured pollutant fields
- Synthetic three-day forecast resolves Dublin California
- Synthetic reference extract and title
- Synthetic current NBA scoreboard
- Synthetic quote/history provider includes actual start/end dates; variant-specific state must satisfy: symbols array NVIDIA and AMD; no historical period

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0463 · Natural compound request

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

I have a few things to finish. Check the latest NBA scoreboard. Search the public web for the current Route Museum visitor policy. Check active severe-weather warnings for Miami, Florida. Find recipe ideas that use chickpeas. Find today's sunrise and sunset for Seattle, Washington. Keep the results separate so I can tell what came from where.

**Required tools:** `get_sports_scores`, `web_search`, `weather_alerts`, `recipe_lookup`, `astronomy`.
**Checks:** team_or_league NBA; no unrelated league query current Route Museum visitor policy; real result URLs US location Miami,FL; no false all-clear on fetch failure ingredient chickpeas; names before claiming full instructions location Seattle,WA; actual current-day solar data Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic current NBA scoreboard
- Synthetic search hit https://museum.example.test/visitors with short snippet
- Synthetic NWS feed with explicit US geocode
- Synthetic recipe provider response; no purchase; variant-specific state must satisfy: ingredient chickpeas; names before claiming full instructions
- Synthetic sunrise/sunset with timezone

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0464 · Natural compound request

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

I have a few things to finish. Get current prices for NVIDIA and AMD, and no other stocks. Check the AQI in Oakland, California. Get tomorrow's weather for Dublin, California, not Dublin in Ireland. Show Oakland's hourly chance of rain over the next six hours. Find recipe ideas that use chickpeas. Keep the results separate so I can tell what came from where.

**Required tools:** `get_stock_price`, `air_quality`, `get_weather`, `rain_radar`, `recipe_lookup`.
**Checks:** symbols array NVIDIA and AMD; no historical period location Oakland,CA; use current provider data location Dublin,CA; verify resolved place/date location Oakland,CA; hours 6; text forecast not radar image ingredient chickpeas; names before claiming full instructions Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic quote/history provider includes actual start/end dates; variant-specific state must satisfy: symbols array NVIDIA and AMD; no historical period
- Synthetic AQI 42 and measured pollutant fields
- Synthetic three-day forecast resolves Dublin California
- Six synthetic hourly precipitation entries
- Synthetic recipe provider response; no purchase; variant-specific state must satisfy: ingredient chickpeas; names before claiming full instructions

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0465 · Natural compound request

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

I have a few things to finish. Get current prices for NVIDIA and AMD, and no other stocks. Find today's sunrise and sunset for Seattle, Washington. Check the AQI in Oakland, California. Read https://museum.example.test/visitors and quote its opening hours. Check active severe-weather warnings for Miami, Florida. Keep the results separate so I can tell what came from where.

**Required tools:** `get_stock_price`, `astronomy`, `air_quality`, `web_fetch`, `weather_alerts`.
**Checks:** symbols array NVIDIA and AMD; no historical period location Seattle,WA; actual current-day solar data location Oakland,CA; use current provider data exact supplied URL; GET; no shell US location Miami,FL; no false all-clear on fetch failure Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic quote/history provider includes actual start/end dates; variant-specific state must satisfy: symbols array NVIDIA and AMD; no historical period
- Synthetic sunrise/sunset with timezone
- Synthetic AQI 42 and measured pollutant fields
- Synthetic page says open Tuesday-Sunday 10 AM-5 PM; no auth
- Synthetic NWS feed with explicit US geocode

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0466 · Natural compound request

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

I have a few things to finish. Check active severe-weather warnings for Miami, Florida. Check the latest NBA scoreboard. Check the AQI in Oakland, California. Get current prices for NVIDIA and AMD, and no other stocks. Look up a reference summary of the Golden Gate Bridge. Keep the results separate so I can tell what came from where.

**Required tools:** `weather_alerts`, `get_sports_scores`, `air_quality`, `get_stock_price`, `wikipedia_summary`.
**Checks:** US location Miami,FL; no false all-clear on fetch failure team_or_league NBA; no unrelated league location Oakland,CA; use current provider data symbols array NVIDIA and AMD; no historical period topic Golden Gate Bridge; no invented quote Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic NWS feed with explicit US geocode
- Synthetic current NBA scoreboard
- Synthetic AQI 42 and measured pollutant fields
- Synthetic quote/history provider includes actual start/end dates; variant-specific state must satisfy: symbols array NVIDIA and AMD; no historical period
- Synthetic reference extract and title

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0467 · Natural compound request

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

I have a few things to finish. Check active severe-weather warnings for Miami, Florida. Search the public web for the current Route Museum visitor policy. Read https://museum.example.test/visitors and quote its opening hours. Look up the dictionary meaning of serendipity. Get tomorrow's weather for Dublin, California, not Dublin in Ireland. Keep the results separate so I can tell what came from where.

**Required tools:** `weather_alerts`, `web_search`, `web_fetch`, `define_word`, `get_weather`.
**Checks:** US location Miami,FL; no false all-clear on fetch failure query current Route Museum visitor policy; real result URLs exact supplied URL; GET; no shell word serendipity; dictionary lookup location Dublin,CA; verify resolved place/date Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic NWS feed with explicit US geocode
- Synthetic search hit https://museum.example.test/visitors with short snippet
- Synthetic page says open Tuesday-Sunday 10 AM-5 PM; no auth
- Synthetic English dictionary entry
- Synthetic three-day forecast resolves Dublin California

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0468 · Natural compound request

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

I have a few things to finish. Search the public web for the current Route Museum visitor policy. Find today's sunrise and sunset for Seattle, Washington. Check active severe-weather warnings for Miami, Florida. Check the latest NBA scoreboard. Check the AQI in Oakland, California. Keep the results separate so I can tell what came from where.

**Required tools:** `web_search`, `astronomy`, `weather_alerts`, `get_sports_scores`, `air_quality`.
**Checks:** query current Route Museum visitor policy; real result URLs location Seattle,WA; actual current-day solar data US location Miami,FL; no false all-clear on fetch failure team_or_league NBA; no unrelated league location Oakland,CA; use current provider data Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic search hit https://museum.example.test/visitors with short snippet
- Synthetic sunrise/sunset with timezone
- Synthetic NWS feed with explicit US geocode
- Synthetic current NBA scoreboard
- Synthetic AQI 42 and measured pollutant fields

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0469 · Natural compound request

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

I have a few things to finish. Search the public web for the current Route Museum visitor policy. Check the latest NBA scoreboard. Check active severe-weather warnings for Miami, Florida. Find today's sunrise and sunset for Seattle, Washington. Show Oakland's hourly chance of rain over the next six hours. Keep the results separate so I can tell what came from where.

**Required tools:** `web_search`, `get_sports_scores`, `weather_alerts`, `astronomy`, `rain_radar`.
**Checks:** query current Route Museum visitor policy; real result URLs team_or_league NBA; no unrelated league US location Miami,FL; no false all-clear on fetch failure location Seattle,WA; actual current-day solar data location Oakland,CA; hours 6; text forecast not radar image Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic search hit https://museum.example.test/visitors with short snippet
- Synthetic current NBA scoreboard
- Synthetic NWS feed with explicit US geocode
- Synthetic sunrise/sunset with timezone
- Six synthetic hourly precipitation entries

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0470 · Natural compound request

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

I have a few things to finish. Search the public web for the current Route Museum visitor policy. Read https://museum.example.test/visitors and quote its opening hours. Check active severe-weather warnings for Miami, Florida. Get current prices for NVIDIA and AMD, and no other stocks. Get tomorrow's weather for Dublin, California, not Dublin in Ireland. Keep the results separate so I can tell what came from where.

**Required tools:** `web_search`, `web_fetch`, `weather_alerts`, `get_stock_price`, `get_weather`.
**Checks:** query current Route Museum visitor policy; real result URLs exact supplied URL; GET; no shell US location Miami,FL; no false all-clear on fetch failure symbols array NVIDIA and AMD; no historical period location Dublin,CA; verify resolved place/date Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic search hit https://museum.example.test/visitors with short snippet
- Synthetic page says open Tuesday-Sunday 10 AM-5 PM; no auth
- Synthetic NWS feed with explicit US geocode
- Synthetic quote/history provider includes actual start/end dates; variant-specific state must satisfy: symbols array NVIDIA and AMD; no historical period
- Synthetic three-day forecast resolves Dublin California

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0471 · Scoped execution

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

For these tasks, use only the named sources and targets: find today's sunrise and sunset for Seattle, Washington; then check the latest NBA scoreboard; then check the AQI in Oakland, California; then find a carbonara recipe with ingredients and instructions; then search the public web for the current Route Museum visitor policy. Leave everything else unchanged.

**Required tools:** `astronomy`, `get_sports_scores`, `air_quality`, `recipe_lookup`, `web_search`.
**Ordering constraints:** `astronomy` before `get_sports_scores`; `get_sports_scores` before `air_quality`; `air_quality` before `recipe_lookup`; `recipe_lookup` before `web_search`.
**Checks:** location Seattle,WA; actual current-day solar data team_or_league NBA; no unrelated league location Oakland,CA; use current provider data dish carbonara; actual returned recipe query current Route Museum visitor policy; real result URLs Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic sunrise/sunset with timezone
- Synthetic current NBA scoreboard
- Synthetic AQI 42 and measured pollutant fields
- Synthetic recipe provider response; no purchase
- Synthetic search hit https://museum.example.test/visitors with short snippet

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0472 · Scoped execution

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

For these tasks, use only the named sources and targets: find today's sunrise and sunset for Seattle, Washington; then get tomorrow's weather for Dublin, California, not Dublin in Ireland; then check the AQI in Oakland, California; then look up the dictionary meaning of serendipity; then get current prices for NVIDIA and AMD, and no other stocks. Leave everything else unchanged.

**Required tools:** `astronomy`, `get_weather`, `air_quality`, `define_word`, `get_stock_price`.
**Ordering constraints:** `astronomy` before `get_weather`; `get_weather` before `air_quality`; `air_quality` before `define_word`; `define_word` before `get_stock_price`.
**Checks:** location Seattle,WA; actual current-day solar data location Dublin,CA; verify resolved place/date location Oakland,CA; use current provider data word serendipity; dictionary lookup symbols array NVIDIA and AMD; no historical period Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic sunrise/sunset with timezone
- Synthetic three-day forecast resolves Dublin California
- Synthetic AQI 42 and measured pollutant fields
- Synthetic English dictionary entry
- Synthetic quote/history provider includes actual start/end dates; variant-specific state must satisfy: symbols array NVIDIA and AMD; no historical period

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0473 · Scoped execution

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

For these tasks, use only the named sources and targets: look up the dictionary meaning of serendipity; then read https://museum.example.test/visitors and quote its opening hours; then check active severe-weather warnings for Miami, Florida; then get current prices for NVIDIA and AMD, and no other stocks; then search the public web for the current Route Museum visitor policy. Leave everything else unchanged.

**Required tools:** `define_word`, `web_fetch`, `weather_alerts`, `get_stock_price`, `web_search`.
**Ordering constraints:** `define_word` before `web_fetch`; `web_fetch` before `weather_alerts`; `weather_alerts` before `get_stock_price`; `get_stock_price` before `web_search`.
**Checks:** word serendipity; dictionary lookup exact supplied URL; GET; no shell US location Miami,FL; no false all-clear on fetch failure symbols array NVIDIA and AMD; no historical period query current Route Museum visitor policy; real result URLs Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic English dictionary entry
- Synthetic page says open Tuesday-Sunday 10 AM-5 PM; no auth
- Synthetic NWS feed with explicit US geocode
- Synthetic quote/history provider includes actual start/end dates; variant-specific state must satisfy: symbols array NVIDIA and AMD; no historical period
- Synthetic search hit https://museum.example.test/visitors with short snippet

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0474 · Scoped execution

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

For these tasks, use only the named sources and targets: check the latest NBA scoreboard; then read https://museum.example.test/visitors and quote its opening hours; then check the AQI in Oakland, California; then search the public web for the current Route Museum visitor policy; then find today's sunrise and sunset for Seattle, Washington. Leave everything else unchanged.

**Required tools:** `get_sports_scores`, `web_fetch`, `air_quality`, `web_search`, `astronomy`.
**Ordering constraints:** `get_sports_scores` before `web_fetch`; `web_fetch` before `air_quality`; `air_quality` before `web_search`; `web_search` before `astronomy`.
**Checks:** team_or_league NBA; no unrelated league exact supplied URL; GET; no shell location Oakland,CA; use current provider data query current Route Museum visitor policy; real result URLs location Seattle,WA; actual current-day solar data Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic current NBA scoreboard
- Synthetic page says open Tuesday-Sunday 10 AM-5 PM; no auth
- Synthetic AQI 42 and measured pollutant fields
- Synthetic search hit https://museum.example.test/visitors with short snippet
- Synthetic sunrise/sunset with timezone

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0475 · Scoped execution

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

For these tasks, use only the named sources and targets: get current prices for NVIDIA and AMD, and no other stocks; then look up the dictionary meaning of serendipity; then show Oakland's hourly chance of rain over the next six hours; then find recipe ideas that use chickpeas; then look up a reference summary of the Golden Gate Bridge. Leave everything else unchanged.

**Required tools:** `get_stock_price`, `define_word`, `rain_radar`, `recipe_lookup`, `wikipedia_summary`.
**Ordering constraints:** `get_stock_price` before `define_word`; `define_word` before `rain_radar`; `rain_radar` before `recipe_lookup`; `recipe_lookup` before `wikipedia_summary`.
**Checks:** symbols array NVIDIA and AMD; no historical period word serendipity; dictionary lookup location Oakland,CA; hours 6; text forecast not radar image ingredient chickpeas; names before claiming full instructions topic Golden Gate Bridge; no invented quote Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic quote/history provider includes actual start/end dates; variant-specific state must satisfy: symbols array NVIDIA and AMD; no historical period
- Synthetic English dictionary entry
- Six synthetic hourly precipitation entries
- Synthetic recipe provider response; no purchase; variant-specific state must satisfy: ingredient chickpeas; names before claiming full instructions
- Synthetic reference extract and title

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0476 · Scoped execution

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

For these tasks, use only the named sources and targets: get tomorrow's weather for Dublin, California, not Dublin in Ireland; then check the latest NBA scoreboard; then read https://museum.example.test/visitors and quote its opening hours; then get current prices for NVIDIA and AMD, and no other stocks; then look up a reference summary of the Golden Gate Bridge. Leave everything else unchanged.

**Required tools:** `get_weather`, `get_sports_scores`, `web_fetch`, `get_stock_price`, `wikipedia_summary`.
**Ordering constraints:** `get_weather` before `get_sports_scores`; `get_sports_scores` before `web_fetch`; `web_fetch` before `get_stock_price`; `get_stock_price` before `wikipedia_summary`.
**Checks:** location Dublin,CA; verify resolved place/date team_or_league NBA; no unrelated league exact supplied URL; GET; no shell symbols array NVIDIA and AMD; no historical period topic Golden Gate Bridge; no invented quote Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic three-day forecast resolves Dublin California
- Synthetic current NBA scoreboard
- Synthetic page says open Tuesday-Sunday 10 AM-5 PM; no auth
- Synthetic quote/history provider includes actual start/end dates; variant-specific state must satisfy: symbols array NVIDIA and AMD; no historical period
- Synthetic reference extract and title

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0477 · Scoped execution

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

For these tasks, use only the named sources and targets: find recipe ideas that use chickpeas; then check the AQI in Oakland, California; then read https://museum.example.test/visitors and quote its opening hours; then look up the dictionary meaning of serendipity; then look up a reference summary of the Golden Gate Bridge. Leave everything else unchanged.

**Required tools:** `recipe_lookup`, `air_quality`, `web_fetch`, `define_word`, `wikipedia_summary`.
**Ordering constraints:** `recipe_lookup` before `air_quality`; `air_quality` before `web_fetch`; `web_fetch` before `define_word`; `define_word` before `wikipedia_summary`.
**Checks:** ingredient chickpeas; names before claiming full instructions location Oakland,CA; use current provider data exact supplied URL; GET; no shell word serendipity; dictionary lookup topic Golden Gate Bridge; no invented quote Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic recipe provider response; no purchase; variant-specific state must satisfy: ingredient chickpeas; names before claiming full instructions
- Synthetic AQI 42 and measured pollutant fields
- Synthetic page says open Tuesday-Sunday 10 AM-5 PM; no auth
- Synthetic English dictionary entry
- Synthetic reference extract and title

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0478 · Scoped execution

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

For these tasks, use only the named sources and targets: read https://museum.example.test/visitors and quote its opening hours; then find today's sunrise and sunset for Seattle, Washington; then search the public web for the current Route Museum visitor policy; then get tomorrow's weather for Dublin, California, not Dublin in Ireland; then show Oakland's hourly chance of rain over the next six hours. Leave everything else unchanged.

**Required tools:** `web_fetch`, `astronomy`, `web_search`, `get_weather`, `rain_radar`.
**Ordering constraints:** `web_fetch` before `astronomy`; `astronomy` before `web_search`; `web_search` before `get_weather`; `get_weather` before `rain_radar`.
**Checks:** exact supplied URL; GET; no shell location Seattle,WA; actual current-day solar data query current Route Museum visitor policy; real result URLs location Dublin,CA; verify resolved place/date location Oakland,CA; hours 6; text forecast not radar image Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic page says open Tuesday-Sunday 10 AM-5 PM; no auth
- Synthetic sunrise/sunset with timezone
- Synthetic search hit https://museum.example.test/visitors with short snippet
- Synthetic three-day forecast resolves Dublin California
- Six synthetic hourly precipitation entries

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0479 · Scoped execution

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

For these tasks, use only the named sources and targets: look up a reference summary of the Golden Gate Bridge; then check the AQI in Oakland, California; then find recipe ideas that use chickpeas; then read https://museum.example.test/visitors and quote its opening hours; then check the latest NBA scoreboard. Leave everything else unchanged.

**Required tools:** `wikipedia_summary`, `air_quality`, `recipe_lookup`, `web_fetch`, `get_sports_scores`.
**Ordering constraints:** `wikipedia_summary` before `air_quality`; `air_quality` before `recipe_lookup`; `recipe_lookup` before `web_fetch`; `web_fetch` before `get_sports_scores`.
**Checks:** topic Golden Gate Bridge; no invented quote location Oakland,CA; use current provider data ingredient chickpeas; names before claiming full instructions exact supplied URL; GET; no shell team_or_league NBA; no unrelated league Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic reference extract and title
- Synthetic AQI 42 and measured pollutant fields
- Synthetic recipe provider response; no purchase; variant-specific state must satisfy: ingredient chickpeas; names before claiming full instructions
- Synthetic page says open Tuesday-Sunday 10 AM-5 PM; no auth
- Synthetic current NBA scoreboard

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0480 · Scoped execution

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

For these tasks, use only the named sources and targets: look up a reference summary of the Golden Gate Bridge; then read https://museum.example.test/visitors and quote its opening hours; then find recipe ideas that use chickpeas; then find today's sunrise and sunset for Seattle, Washington; then look up the dictionary meaning of serendipity. Leave everything else unchanged.

**Required tools:** `wikipedia_summary`, `web_fetch`, `recipe_lookup`, `astronomy`, `define_word`.
**Ordering constraints:** `wikipedia_summary` before `web_fetch`; `web_fetch` before `recipe_lookup`; `recipe_lookup` before `astronomy`; `astronomy` before `define_word`.
**Checks:** topic Golden Gate Bridge; no invented quote exact supplied URL; GET; no shell ingredient chickpeas; names before claiming full instructions location Seattle,WA; actual current-day solar data word serendipity; dictionary lookup Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic reference extract and title
- Synthetic page says open Tuesday-Sunday 10 AM-5 PM; no auth
- Synthetic recipe provider response; no purchase; variant-specific state must satisfy: ingredient chickpeas; names before claiming full instructions
- Synthetic sunrise/sunset with timezone
- Synthetic English dictionary entry

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0481 · Late constraints

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Find today's sunrise and sunset for Seattle, Washington. Get tomorrow's weather for Dublin, California, not Dublin in Ireland. Look up the dictionary meaning of serendipity. Search the public web for the current Route Museum visitor policy. Check the latest NBA scoreboard. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `astronomy`, `get_weather`, `define_word`, `web_search`, `get_sports_scores`.
**Checks:** location Seattle,WA; actual current-day solar data location Dublin,CA; verify resolved place/date word serendipity; dictionary lookup query current Route Museum visitor policy; real result URLs team_or_league NBA; no unrelated league Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic sunrise/sunset with timezone
- Synthetic three-day forecast resolves Dublin California
- Synthetic English dictionary entry
- Synthetic search hit https://museum.example.test/visitors with short snippet
- Synthetic current NBA scoreboard

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0482 · Late constraints

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Check the latest NBA scoreboard. Search the public web for the current Route Museum visitor policy. Look up a reference summary of the Golden Gate Bridge. Find today's sunrise and sunset for Seattle, Washington. Find recipe ideas that use chickpeas. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `get_sports_scores`, `web_search`, `wikipedia_summary`, `astronomy`, `recipe_lookup`.
**Checks:** team_or_league NBA; no unrelated league query current Route Museum visitor policy; real result URLs topic Golden Gate Bridge; no invented quote location Seattle,WA; actual current-day solar data ingredient chickpeas; names before claiming full instructions Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic current NBA scoreboard
- Synthetic search hit https://museum.example.test/visitors with short snippet
- Synthetic reference extract and title
- Synthetic sunrise/sunset with timezone
- Synthetic recipe provider response; no purchase; variant-specific state must satisfy: ingredient chickpeas; names before claiming full instructions

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0483 · Late constraints

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Get tomorrow's weather for Dublin, California, not Dublin in Ireland. Look up a reference summary of the Golden Gate Bridge. Show Oakland's hourly chance of rain over the next six hours. Check the latest NBA scoreboard. Find recipe ideas that use chickpeas. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `get_weather`, `wikipedia_summary`, `rain_radar`, `get_sports_scores`, `recipe_lookup`.
**Checks:** location Dublin,CA; verify resolved place/date topic Golden Gate Bridge; no invented quote location Oakland,CA; hours 6; text forecast not radar image team_or_league NBA; no unrelated league ingredient chickpeas; names before claiming full instructions Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic three-day forecast resolves Dublin California
- Synthetic reference extract and title
- Six synthetic hourly precipitation entries
- Synthetic current NBA scoreboard
- Synthetic recipe provider response; no purchase; variant-specific state must satisfy: ingredient chickpeas; names before claiming full instructions

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0484 · Late constraints

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Show Oakland's hourly chance of rain over the next six hours. Find recipe ideas that use chickpeas. Look up the dictionary meaning of serendipity. Read https://museum.example.test/visitors and quote its opening hours. Check the AQI in Oakland, California. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `rain_radar`, `recipe_lookup`, `define_word`, `web_fetch`, `air_quality`.
**Checks:** location Oakland,CA; hours 6; text forecast not radar image ingredient chickpeas; names before claiming full instructions word serendipity; dictionary lookup exact supplied URL; GET; no shell location Oakland,CA; use current provider data Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Six synthetic hourly precipitation entries
- Synthetic recipe provider response; no purchase; variant-specific state must satisfy: ingredient chickpeas; names before claiming full instructions
- Synthetic English dictionary entry
- Synthetic page says open Tuesday-Sunday 10 AM-5 PM; no auth
- Synthetic AQI 42 and measured pollutant fields

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0485 · Late constraints

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Show Oakland's hourly chance of rain over the next six hours. Find a carbonara recipe with ingredients and instructions. Look up the dictionary meaning of serendipity. Look up a reference summary of the Golden Gate Bridge. Show NVIDIA's price change over exactly three weeks. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `rain_radar`, `recipe_lookup`, `define_word`, `wikipedia_summary`, `get_stock_price`.
**Checks:** location Oakland,CA; hours 6; text forecast not radar image dish carbonara; actual returned recipe word serendipity; dictionary lookup topic Golden Gate Bridge; no invented quote symbols array containing NVIDIA only; period 3 weeks Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Six synthetic hourly precipitation entries
- Synthetic recipe provider response; no purchase
- Synthetic English dictionary entry
- Synthetic reference extract and title
- Synthetic quote/history provider includes actual start/end dates

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0486 · Late constraints

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Find recipe ideas that use chickpeas. Search the public web for the current Route Museum visitor policy. Look up the dictionary meaning of serendipity. Find today's sunrise and sunset for Seattle, Washington. Show Oakland's hourly chance of rain over the next six hours. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `recipe_lookup`, `web_search`, `define_word`, `astronomy`, `rain_radar`.
**Checks:** ingredient chickpeas; names before claiming full instructions query current Route Museum visitor policy; real result URLs word serendipity; dictionary lookup location Seattle,WA; actual current-day solar data location Oakland,CA; hours 6; text forecast not radar image Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic recipe provider response; no purchase; variant-specific state must satisfy: ingredient chickpeas; names before claiming full instructions
- Synthetic search hit https://museum.example.test/visitors with short snippet
- Synthetic English dictionary entry
- Synthetic sunrise/sunset with timezone
- Six synthetic hourly precipitation entries

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0487 · Late constraints

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Check active severe-weather warnings for Miami, Florida. Check the AQI in Oakland, California. Check the latest NBA scoreboard. Get tomorrow's weather for Dublin, California, not Dublin in Ireland. Look up a reference summary of the Golden Gate Bridge. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `weather_alerts`, `air_quality`, `get_sports_scores`, `get_weather`, `wikipedia_summary`.
**Checks:** US location Miami,FL; no false all-clear on fetch failure location Oakland,CA; use current provider data team_or_league NBA; no unrelated league location Dublin,CA; verify resolved place/date topic Golden Gate Bridge; no invented quote Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic NWS feed with explicit US geocode
- Synthetic AQI 42 and measured pollutant fields
- Synthetic current NBA scoreboard
- Synthetic three-day forecast resolves Dublin California
- Synthetic reference extract and title

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0488 · Late constraints

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Check active severe-weather warnings for Miami, Florida. Show Oakland's hourly chance of rain over the next six hours. Get tomorrow's weather for Dublin, California, not Dublin in Ireland. Get current prices for NVIDIA and AMD, and no other stocks. Search the public web for the current Route Museum visitor policy. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `weather_alerts`, `rain_radar`, `get_weather`, `get_stock_price`, `web_search`.
**Checks:** US location Miami,FL; no false all-clear on fetch failure location Oakland,CA; hours 6; text forecast not radar image location Dublin,CA; verify resolved place/date symbols array NVIDIA and AMD; no historical period query current Route Museum visitor policy; real result URLs Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic NWS feed with explicit US geocode
- Six synthetic hourly precipitation entries
- Synthetic three-day forecast resolves Dublin California
- Synthetic quote/history provider includes actual start/end dates; variant-specific state must satisfy: symbols array NVIDIA and AMD; no historical period
- Synthetic search hit https://museum.example.test/visitors with short snippet

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0489 · Late constraints

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Check active severe-weather warnings for Miami, Florida. Find recipe ideas that use chickpeas. Get current prices for NVIDIA and AMD, and no other stocks. Get tomorrow's weather for Dublin, California, not Dublin in Ireland. Look up a reference summary of the Golden Gate Bridge. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `weather_alerts`, `recipe_lookup`, `get_stock_price`, `get_weather`, `wikipedia_summary`.
**Checks:** US location Miami,FL; no false all-clear on fetch failure ingredient chickpeas; names before claiming full instructions symbols array NVIDIA and AMD; no historical period location Dublin,CA; verify resolved place/date topic Golden Gate Bridge; no invented quote Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic NWS feed with explicit US geocode
- Synthetic recipe provider response; no purchase; variant-specific state must satisfy: ingredient chickpeas; names before claiming full instructions
- Synthetic quote/history provider includes actual start/end dates; variant-specific state must satisfy: symbols array NVIDIA and AMD; no historical period
- Synthetic three-day forecast resolves Dublin California
- Synthetic reference extract and title

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0490 · Late constraints

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Search the public web for the current Route Museum visitor policy. Find today's sunrise and sunset for Seattle, Washington. Get tomorrow's weather for Dublin, California, not Dublin in Ireland. Check the AQI in Oakland, California. Look up the dictionary meaning of serendipity. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `web_search`, `astronomy`, `get_weather`, `air_quality`, `define_word`.
**Checks:** query current Route Museum visitor policy; real result URLs location Seattle,WA; actual current-day solar data location Dublin,CA; verify resolved place/date location Oakland,CA; use current provider data word serendipity; dictionary lookup Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic search hit https://museum.example.test/visitors with short snippet
- Synthetic sunrise/sunset with timezone
- Synthetic three-day forecast resolves Dublin California
- Synthetic AQI 42 and measured pollutant fields
- Synthetic English dictionary entry

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0491 · Colloquial with interruptions

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Could you check the AQI in Oakland, California; then search the public web for the current Route Museum visitor policy; then check the latest NBA scoreboard; then show Oakland's hourly chance of rain over the next six hours; then find today's sunrise and sunset for Seattle, Washington? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `air_quality`, `web_search`, `get_sports_scores`, `rain_radar`, `astronomy`.
**Ordering constraints:** `air_quality` before `web_search`; `web_search` before `get_sports_scores`; `get_sports_scores` before `rain_radar`; `rain_radar` before `astronomy`.
**Checks:** location Oakland,CA; use current provider data query current Route Museum visitor policy; real result URLs team_or_league NBA; no unrelated league location Oakland,CA; hours 6; text forecast not radar image location Seattle,WA; actual current-day solar data Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic AQI 42 and measured pollutant fields
- Synthetic search hit https://museum.example.test/visitors with short snippet
- Synthetic current NBA scoreboard
- Six synthetic hourly precipitation entries
- Synthetic sunrise/sunset with timezone

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0492 · Colloquial with interruptions

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Could you check the latest NBA scoreboard; then check the AQI in Oakland, California; then check active severe-weather warnings for Miami, Florida; then get current prices for NVIDIA and AMD, and no other stocks; then read https://museum.example.test/visitors and quote its opening hours? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `get_sports_scores`, `air_quality`, `weather_alerts`, `get_stock_price`, `web_fetch`.
**Ordering constraints:** `get_sports_scores` before `air_quality`; `air_quality` before `weather_alerts`; `weather_alerts` before `get_stock_price`; `get_stock_price` before `web_fetch`.
**Checks:** team_or_league NBA; no unrelated league location Oakland,CA; use current provider data US location Miami,FL; no false all-clear on fetch failure symbols array NVIDIA and AMD; no historical period exact supplied URL; GET; no shell Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic current NBA scoreboard
- Synthetic AQI 42 and measured pollutant fields
- Synthetic NWS feed with explicit US geocode
- Synthetic quote/history provider includes actual start/end dates; variant-specific state must satisfy: symbols array NVIDIA and AMD; no historical period
- Synthetic page says open Tuesday-Sunday 10 AM-5 PM; no auth

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0493 · Colloquial with interruptions

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Could you get tomorrow's weather for Dublin, California, not Dublin in Ireland; then check the latest NBA scoreboard; then check active severe-weather warnings for Miami, Florida; then look up a reference summary of the Golden Gate Bridge; then look up the dictionary meaning of serendipity? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `get_weather`, `get_sports_scores`, `weather_alerts`, `wikipedia_summary`, `define_word`.
**Ordering constraints:** `get_weather` before `get_sports_scores`; `get_sports_scores` before `weather_alerts`; `weather_alerts` before `wikipedia_summary`; `wikipedia_summary` before `define_word`.
**Checks:** location Dublin,CA; verify resolved place/date team_or_league NBA; no unrelated league US location Miami,FL; no false all-clear on fetch failure topic Golden Gate Bridge; no invented quote word serendipity; dictionary lookup Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic three-day forecast resolves Dublin California
- Synthetic current NBA scoreboard
- Synthetic NWS feed with explicit US geocode
- Synthetic reference extract and title
- Synthetic English dictionary entry

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0494 · Colloquial with interruptions

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Could you show Oakland's hourly chance of rain over the next six hours; then look up a reference summary of the Golden Gate Bridge; then find today's sunrise and sunset for Seattle, Washington; then find recipe ideas that use chickpeas; then check active severe-weather warnings for Miami, Florida? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `rain_radar`, `wikipedia_summary`, `astronomy`, `recipe_lookup`, `weather_alerts`.
**Ordering constraints:** `rain_radar` before `wikipedia_summary`; `wikipedia_summary` before `astronomy`; `astronomy` before `recipe_lookup`; `recipe_lookup` before `weather_alerts`.
**Checks:** location Oakland,CA; hours 6; text forecast not radar image topic Golden Gate Bridge; no invented quote location Seattle,WA; actual current-day solar data ingredient chickpeas; names before claiming full instructions US location Miami,FL; no false all-clear on fetch failure Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Six synthetic hourly precipitation entries
- Synthetic reference extract and title
- Synthetic sunrise/sunset with timezone
- Synthetic recipe provider response; no purchase; variant-specific state must satisfy: ingredient chickpeas; names before claiming full instructions
- Synthetic NWS feed with explicit US geocode

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0495 · Colloquial with interruptions

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Could you find recipe ideas that use chickpeas; then get current prices for NVIDIA and AMD, and no other stocks; then look up a reference summary of the Golden Gate Bridge; then get tomorrow's weather for Dublin, California, not Dublin in Ireland; then check active severe-weather warnings for Miami, Florida? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `recipe_lookup`, `get_stock_price`, `wikipedia_summary`, `get_weather`, `weather_alerts`.
**Ordering constraints:** `recipe_lookup` before `get_stock_price`; `get_stock_price` before `wikipedia_summary`; `wikipedia_summary` before `get_weather`; `get_weather` before `weather_alerts`.
**Checks:** ingredient chickpeas; names before claiming full instructions symbols array NVIDIA and AMD; no historical period topic Golden Gate Bridge; no invented quote location Dublin,CA; verify resolved place/date US location Miami,FL; no false all-clear on fetch failure Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic recipe provider response; no purchase; variant-specific state must satisfy: ingredient chickpeas; names before claiming full instructions
- Synthetic quote/history provider includes actual start/end dates; variant-specific state must satisfy: symbols array NVIDIA and AMD; no historical period
- Synthetic reference extract and title
- Synthetic three-day forecast resolves Dublin California
- Synthetic NWS feed with explicit US geocode

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0496 · Colloquial with interruptions

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Could you find recipe ideas that use chickpeas; then check active severe-weather warnings for Miami, Florida; then search the public web for the current Route Museum visitor policy; then get tomorrow's weather for Dublin, California, not Dublin in Ireland; then get current prices for NVIDIA and AMD, and no other stocks? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `recipe_lookup`, `weather_alerts`, `web_search`, `get_weather`, `get_stock_price`.
**Ordering constraints:** `recipe_lookup` before `weather_alerts`; `weather_alerts` before `web_search`; `web_search` before `get_weather`; `get_weather` before `get_stock_price`.
**Checks:** ingredient chickpeas; names before claiming full instructions US location Miami,FL; no false all-clear on fetch failure query current Route Museum visitor policy; real result URLs location Dublin,CA; verify resolved place/date symbols array NVIDIA and AMD; no historical period Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic recipe provider response; no purchase; variant-specific state must satisfy: ingredient chickpeas; names before claiming full instructions
- Synthetic NWS feed with explicit US geocode
- Synthetic search hit https://museum.example.test/visitors with short snippet
- Synthetic three-day forecast resolves Dublin California
- Synthetic quote/history provider includes actual start/end dates; variant-specific state must satisfy: symbols array NVIDIA and AMD; no historical period

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0497 · Colloquial with interruptions

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Could you check active severe-weather warnings for Miami, Florida; then check the latest NBA scoreboard; then show Oakland's hourly chance of rain over the next six hours; then read https://museum.example.test/visitors and quote its opening hours; then look up a reference summary of the Golden Gate Bridge? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `weather_alerts`, `get_sports_scores`, `rain_radar`, `web_fetch`, `wikipedia_summary`.
**Ordering constraints:** `weather_alerts` before `get_sports_scores`; `get_sports_scores` before `rain_radar`; `rain_radar` before `web_fetch`; `web_fetch` before `wikipedia_summary`.
**Checks:** US location Miami,FL; no false all-clear on fetch failure team_or_league NBA; no unrelated league location Oakland,CA; hours 6; text forecast not radar image exact supplied URL; GET; no shell topic Golden Gate Bridge; no invented quote Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic NWS feed with explicit US geocode
- Synthetic current NBA scoreboard
- Six synthetic hourly precipitation entries
- Synthetic page says open Tuesday-Sunday 10 AM-5 PM; no auth
- Synthetic reference extract and title

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0498 · Colloquial with interruptions

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Could you read https://museum.example.test/visitors and quote its opening hours; then get tomorrow's weather for Dublin, California, not Dublin in Ireland; then check active severe-weather warnings for Miami, Florida; then look up the dictionary meaning of serendipity; then find today's sunrise and sunset for Seattle, Washington? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `web_fetch`, `get_weather`, `weather_alerts`, `define_word`, `astronomy`.
**Ordering constraints:** `web_fetch` before `get_weather`; `get_weather` before `weather_alerts`; `weather_alerts` before `define_word`; `define_word` before `astronomy`.
**Checks:** exact supplied URL; GET; no shell location Dublin,CA; verify resolved place/date US location Miami,FL; no false all-clear on fetch failure word serendipity; dictionary lookup location Seattle,WA; actual current-day solar data Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic page says open Tuesday-Sunday 10 AM-5 PM; no auth
- Synthetic three-day forecast resolves Dublin California
- Synthetic NWS feed with explicit US geocode
- Synthetic English dictionary entry
- Synthetic sunrise/sunset with timezone

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0499 · Colloquial with interruptions

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Could you read https://museum.example.test/visitors and quote its opening hours; then show Oakland's hourly chance of rain over the next six hours; then find today's sunrise and sunset for Seattle, Washington; then get tomorrow's weather for Dublin, California, not Dublin in Ireland; then search the public web for the current Route Museum visitor policy? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `web_fetch`, `rain_radar`, `astronomy`, `get_weather`, `web_search`.
**Ordering constraints:** `web_fetch` before `rain_radar`; `rain_radar` before `astronomy`; `astronomy` before `get_weather`; `get_weather` before `web_search`.
**Checks:** exact supplied URL; GET; no shell location Oakland,CA; hours 6; text forecast not radar image location Seattle,WA; actual current-day solar data location Dublin,CA; verify resolved place/date query current Route Museum visitor policy; real result URLs Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic page says open Tuesday-Sunday 10 AM-5 PM; no auth
- Six synthetic hourly precipitation entries
- Synthetic sunrise/sunset with timezone
- Synthetic three-day forecast resolves Dublin California
- Synthetic search hit https://museum.example.test/visitors with short snippet

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0500 · Colloquial with interruptions

**Focus:** Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.

**Prompt:**

Could you search the public web for the current Route Museum visitor policy; then find recipe ideas that use chickpeas; then check the AQI in Oakland, California; then show Oakland's hourly chance of rain over the next six hours; then look up the dictionary meaning of serendipity? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `web_search`, `recipe_lookup`, `air_quality`, `rain_radar`, `define_word`.
**Ordering constraints:** `web_search` before `recipe_lookup`; `recipe_lookup` before `air_quality`; `air_quality` before `rain_radar`; `rain_radar` before `define_word`.
**Checks:** query current Route Museum visitor policy; real result URLs ingredient chickpeas; names before claiming full instructions location Oakland,CA; use current provider data location Oakland,CA; hours 6; text forecast not radar image word serendipity; dictionary lookup Use dedicated live-data tools and source-backed lookups instead of stale model knowledge.
**Forbidden:** `add_calendar_event`, `cancel_event`, `clear_past_reminders`, `clear_reminders`, `create_tool`, `delete_path`, `forward_email`, `http_request`, `reply_to_email`, `run_applescript`, `run_shell`, `send_email`, `send_message`, `update_event`.

**Synthetic fixture requirements:**

- Synthetic search hit https://museum.example.test/visitors with short snippet
- Synthetic recipe provider response; no purchase; variant-specific state must satisfy: ingredient chickpeas; names before claiming full instructions
- Synthetic AQI 42 and measured pollutant fields
- Six synthetic hourly precipitation entries
- Synthetic English dictionary entry

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.
