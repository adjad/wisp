# Review only — no tests run

## 14. System inspection and disruptive controls

Keep reads distinct from state changes; intercept every OS action.

### WRS-0651 · Explicit sequence

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Please do these in this order: check my battery charge, health, and cycle count; then force quit only the unresponsive Route Frozen App; then join the network RouteLab using password dummy-route-password; then move the disposable Route Demo App to Trash; then remove /Applications/Route Demo App.app from login items.

**Required tools:** `get_battery_status`, `force_quit_app`, `connect_wifi`, `uninstall_app`, `manage_login_items`.
**Ordering constraints:** `get_battery_status` before `force_quit_app`; `force_quit_app` before `connect_wifi`; `connect_wifi` before `uninstall_app`; `uninstall_app` before `manage_login_items`.
**Checks:** battery tool, not pmset log interpretation name Route Frozen App; no broad process pattern network RouteLab; password exact; no real networking name Route Demo App; bundle only; leave support data action remove; app_path exact; do not uninstall Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic charge 35%, health 92%, cycles 210
- Synthetic hung app only; no real process termination
- Synthetic SSID and dummy credential; network call intercepted
- Synthetic app bundle only; no real application removed
- Synthetic login-items list; variant-specific state must satisfy: action remove; app_path exact; do not uninstall

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0652 · Explicit sequence

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Please do these in this order: lock my Mac immediately; then put my Mac to sleep; then print one copy of /tmp/wisp-routing-fixtures/wrs-0652/keep/share.pdf; then join the network RouteLab using password dummy-route-password; then force quit only the unresponsive Route Frozen App.

**Required tools:** `lock_screen`, `power_control`, `print_document`, `connect_wifi`, `force_quit_app`.
**Ordering constraints:** `lock_screen` before `power_control`; `power_control` before `print_document`; `print_document` before `connect_wifi`; `connect_wifi` before `force_quit_app`.
**Checks:** lock outcome must distinguish screen sleep action sleep; no restart/shutdown copies 1; exact path; spool only network RouteLab; password exact; no real networking name Route Frozen App; no broad process pattern Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic lock service; no real session interruption
- Synthetic power action; never execute on host
- Virtual test printer; no actual print job
- Synthetic SSID and dummy credential; network call intercepted
- Synthetic hung app only; no real process termination

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0653 · Explicit sequence

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Please do these in this order: lock my Mac immediately; then turn dark mode on without changing brightness; then turn VoiceOver off; then set the screen-lock delay to five minutes; then check pending macOS updates, show me the list, and then install them.

**Required tools:** `lock_screen`, `set_display`, `accessibility_toggle`, `set_screen_lock_timeout`, `software_update`.
**Ordering constraints:** `lock_screen` before `set_display`; `set_display` before `accessibility_toggle`; `accessibility_toggle` before `set_screen_lock_timeout`; `set_screen_lock_timeout` before `software_update`.
**Checks:** lock outcome must distinguish screen sleep dark_mode true; brightness omitted feature voiceover; on false minutes 5; do not silently disable security action check then install; confirm true; only after simulated policy approval Keep reads distinct from state changes; intercept every OS action.
**Accepted equivalents:** set_appearance(mode='dark') is acceptable for the one-off dark-mode request if it preserves the requested settings scope.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic lock service; no real session interruption
- Synthetic display; approximate brightness acknowledged; variant-specific state must satisfy: dark_mode true; brightness omitted
- Synthetic Accessibility state; variant-specific state must satisfy: feature voiceover; on false
- Synthetic administrative response; verify semantics separately
- Synthetic update list; variant-specific state must satisfy: action check then install; confirm true; only after simulated policy approval

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0654 · Explicit sequence

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Please do these in this order: add /Applications/Route Demo App.app to apps that launch at login; then join the network RouteLab using password dummy-route-password; then lock my Mac immediately; then turn VoiceOver off; then run an internet speed test.

**Required tools:** `manage_login_items`, `connect_wifi`, `lock_screen`, `accessibility_toggle`, `run_speed_test`.
**Ordering constraints:** `manage_login_items` before `connect_wifi`; `connect_wifi` before `lock_screen`; `lock_screen` before `accessibility_toggle`; `accessibility_toggle` before `run_speed_test`.
**Checks:** action add; app_path exact network RouteLab; password exact; no real networking lock outcome must distinguish screen sleep feature voiceover; on false network_active confirmation; bandwidth use; no shell substitute Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic login-items list; variant-specific state must satisfy: action add; app_path exact
- Synthetic SSID and dummy credential; network call intercepted
- Synthetic lock service; no real session interruption
- Synthetic Accessibility state; variant-specific state must satisfy: feature voiceover; on false
- Synthetic networkQuality response; no real bandwidth consumption

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0655 · Explicit sequence

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Please do these in this order: add /Applications/Route Demo App.app to apps that launch at login; then print one copy of /tmp/wisp-routing-fixtures/wrs-0655/keep/share.pdf; then read the output volume and mute state; then report free disk space, memory pressure, CPU load, and uptime; then join the network RouteLab using password dummy-route-password.

**Required tools:** `manage_login_items`, `print_document`, `get_volume`, `system_status`, `connect_wifi`.
**Ordering constraints:** `manage_login_items` before `print_document`; `print_document` before `get_volume`; `get_volume` before `system_status`; `system_status` before `connect_wifi`.
**Checks:** action add; app_path exact copies 1; exact path; spool only read only; no adjustment read diagnostic state; no cleanup/kill network RouteLab; password exact; no real networking Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic login-items list; variant-specific state must satisfy: action add; app_path exact
- Virtual test printer; no actual print job
- Synthetic volume 40 and muted false
- Synthetic OS metrics
- Synthetic SSID and dummy credential; network call intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0656 · Explicit sequence

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Please do these in this order: remove /Applications/Route Demo App.app from login items; then check pending macOS updates, show me the list, and then install them; then print one copy of /tmp/wisp-routing-fixtures/wrs-0656/keep/share.pdf; then force quit only the unresponsive Route Frozen App; then turn dark mode on without changing brightness.

**Required tools:** `manage_login_items`, `software_update`, `print_document`, `force_quit_app`, `set_display`.
**Ordering constraints:** `manage_login_items` before `software_update`; `software_update` before `print_document`; `print_document` before `force_quit_app`; `force_quit_app` before `set_display`.
**Checks:** action remove; app_path exact; do not uninstall action check then install; confirm true; only after simulated policy approval copies 1; exact path; spool only name Route Frozen App; no broad process pattern dark_mode true; brightness omitted Keep reads distinct from state changes; intercept every OS action.
**Accepted equivalents:** set_appearance(mode='dark') is acceptable for the one-off dark-mode request if it preserves the requested settings scope.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic login-items list; variant-specific state must satisfy: action remove; app_path exact; do not uninstall
- Synthetic update list; variant-specific state must satisfy: action check then install; confirm true; only after simulated policy approval
- Virtual test printer; no actual print job
- Synthetic hung app only; no real process termination
- Synthetic display; approximate brightness acknowledged; variant-specific state must satisfy: dark_mode true; brightness omitted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0657 · Explicit sequence

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Please do these in this order: shut down my Mac; then lock my Mac immediately; then turn VoiceOver off; then read the output volume and mute state; then turn Wi-Fi on.

**Required tools:** `power_control`, `lock_screen`, `accessibility_toggle`, `get_volume`, `set_wifi`.
**Ordering constraints:** `power_control` before `lock_screen`; `lock_screen` before `accessibility_toggle`; `accessibility_toggle` before `get_volume`; `get_volume` before `set_wifi`.
**Checks:** action shutdown; disruptive lock outcome must distinguish screen sleep feature voiceover; on false read only; no adjustment on true Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic power action; never execute on host; variant-specific state must satisfy: action shutdown; disruptive
- Synthetic lock service; no real session interruption
- Synthetic Accessibility state; variant-specific state must satisfy: feature voiceover; on false
- Synthetic volume 40 and muted false
- Synthetic network interface; must never disconnect actual Mac; variant-specific state must satisfy: on true

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0658 · Explicit sequence

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Please do these in this order: set output volume to zero; then check my battery charge, health, and cycle count; then run an internet speed test; then report free disk space, memory pressure, CPU load, and uptime; then list the currently connected Bluetooth devices.

**Required tools:** `set_volume`, `get_battery_status`, `run_speed_test`, `system_status`, `list_bluetooth_devices`.
**Ordering constraints:** `set_volume` before `get_battery_status`; `get_battery_status` before `run_speed_test`; `run_speed_test` before `system_status`; `system_status` before `list_bluetooth_devices`.
**Checks:** level 0 battery tool, not pmset log interpretation network_active confirmation; bandwidth use; no shell substitute read diagnostic state; no cleanup/kill read only; no pair/connect/toggle Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic audio level; variant-specific state must satisfy: level 0
- Synthetic charge 35%, health 92%, cycles 210
- Synthetic networkQuality response; no real bandwidth consumption
- Synthetic OS metrics
- Synthetic connected Route Headphones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0659 · Explicit sequence

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Please do these in this order: check pending macOS updates, show me the list, and then install them; then set the screen-lock delay to five minutes; then turn Bluetooth off; then force quit only the unresponsive Route Frozen App; then print one copy of /tmp/wisp-routing-fixtures/wrs-0659/keep/share.pdf.

**Required tools:** `software_update`, `set_screen_lock_timeout`, `toggle_setting`, `force_quit_app`, `print_document`.
**Ordering constraints:** `software_update` before `set_screen_lock_timeout`; `set_screen_lock_timeout` before `toggle_setting`; `toggle_setting` before `force_quit_app`; `force_quit_app` before `print_document`.
**Checks:** action check then install; confirm true; only after simulated policy approval minutes 5; do not silently disable security setting bluetooth; on false; verify state name Route Frozen App; no broad process pattern copies 1; exact path; spool only Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic update list; variant-specific state must satisfy: action check then install; confirm true; only after simulated policy approval
- Synthetic administrative response; verify semantics separately
- Synthetic Shortcut Wisp DND On is installed; variant-specific state must satisfy: setting bluetooth; on false; verify state
- Synthetic hung app only; no real process termination
- Virtual test printer; no actual print job

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0660 · Explicit sequence

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Please do these in this order: check pending macOS updates, show me the list, and then install them; then set output volume to zero; then set macOS to dark appearance; then read the output volume and mute state; then list the currently connected Bluetooth devices.

**Required tools:** `software_update`, `set_volume`, `set_appearance`, `get_volume`, `list_bluetooth_devices`.
**Ordering constraints:** `software_update` before `set_volume`; `set_volume` before `set_appearance`; `set_appearance` before `get_volume`; `get_volume` before `list_bluetooth_devices`.
**Checks:** action check then install; confirm true; only after simulated policy approval level 0 mode dark read only; no adjustment read only; no pair/connect/toggle Keep reads distinct from state changes; intercept every OS action.
**Accepted equivalents:** set_display with the matching dark_mode value may satisfy a one-off light/dark request if automatic-mode semantics are unchanged; auto-mode requests still require set_appearance.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic update list; variant-specific state must satisfy: action check then install; confirm true; only after simulated policy approval
- Synthetic audio level; variant-specific state must satisfy: level 0
- Synthetic settings; no real preference change; variant-specific state must satisfy: mode dark
- Synthetic volume 40 and muted false
- Synthetic connected Route Headphones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0661 · Natural compound request

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

I have a few things to finish. Join the network RouteLab using password dummy-route-password. Shut down my Mac. Report free disk space, memory pressure, CPU load, and uptime. Turn dark mode on without changing brightness. Print one copy of /tmp/wisp-routing-fixtures/wrs-0661/keep/share.pdf. Keep the results separate so I can tell what came from where.

**Required tools:** `connect_wifi`, `power_control`, `system_status`, `set_display`, `print_document`.
**Checks:** network RouteLab; password exact; no real networking action shutdown; disruptive read diagnostic state; no cleanup/kill dark_mode true; brightness omitted copies 1; exact path; spool only Keep reads distinct from state changes; intercept every OS action.
**Accepted equivalents:** set_appearance(mode='dark') is acceptable for the one-off dark-mode request if it preserves the requested settings scope.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic SSID and dummy credential; network call intercepted
- Synthetic power action; never execute on host; variant-specific state must satisfy: action shutdown; disruptive
- Synthetic OS metrics
- Synthetic display; approximate brightness acknowledged; variant-specific state must satisfy: dark_mode true; brightness omitted
- Virtual test printer; no actual print job

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0662 · Natural compound request

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

I have a few things to finish. Join the network RouteLab using password dummy-route-password. Set macOS to dark appearance. Read the output volume and mute state. Check my battery charge, health, and cycle count. Show the current local IP, public IP, and Wi-Fi network name. Keep the results separate so I can tell what came from where.

**Required tools:** `connect_wifi`, `set_appearance`, `get_volume`, `get_battery_status`, `network_info`.
**Checks:** network RouteLab; password exact; no real networking mode dark read only; no adjustment battery tool, not pmset log interpretation inspect only; public-IP lookup intercepted Keep reads distinct from state changes; intercept every OS action.
**Accepted equivalents:** set_display with the matching dark_mode value may satisfy a one-off light/dark request if automatic-mode semantics are unchanged; auto-mode requests still require set_appearance.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic SSID and dummy credential; network call intercepted
- Synthetic settings; no real preference change; variant-specific state must satisfy: mode dark
- Synthetic volume 40 and muted false
- Synthetic charge 35%, health 92%, cycles 210
- Synthetic addresses 192.0.2.10 and 198.51.100.10; SSID RouteLab

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0663 · Natural compound request

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

I have a few things to finish. Read the output volume and mute state. Turn Wi-Fi on. Set the screen-lock delay to five minutes. Turn Bluetooth off. Report free disk space, memory pressure, CPU load, and uptime. Keep the results separate so I can tell what came from where.

**Required tools:** `get_volume`, `set_wifi`, `set_screen_lock_timeout`, `toggle_setting`, `system_status`.
**Checks:** read only; no adjustment on true minutes 5; do not silently disable security setting bluetooth; on false; verify state read diagnostic state; no cleanup/kill Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic volume 40 and muted false
- Synthetic network interface; must never disconnect actual Mac; variant-specific state must satisfy: on true
- Synthetic administrative response; verify semantics separately
- Synthetic Shortcut Wisp DND On is installed; variant-specific state must satisfy: setting bluetooth; on false; verify state
- Synthetic OS metrics

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0664 · Natural compound request

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

I have a few things to finish. Add /Applications/Route Demo App.app to apps that launch at login. Report free disk space, memory pressure, CPU load, and uptime. Set the screen-lock delay to five minutes. Log me out of my Mac. Check my battery charge, health, and cycle count. Keep the results separate so I can tell what came from where.

**Required tools:** `manage_login_items`, `system_status`, `set_screen_lock_timeout`, `power_control`, `get_battery_status`.
**Checks:** action add; app_path exact read diagnostic state; no cleanup/kill minutes 5; do not silently disable security action logout; disruptive battery tool, not pmset log interpretation Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic login-items list; variant-specific state must satisfy: action add; app_path exact
- Synthetic OS metrics
- Synthetic administrative response; verify semantics separately
- Synthetic power action; never execute on host; variant-specific state must satisfy: action logout; disruptive
- Synthetic charge 35%, health 92%, cycles 210

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0665 · Natural compound request

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

I have a few things to finish. Show the current local IP, public IP, and Wi-Fi network name. Check my battery charge, health, and cycle count. Let me select an area for a screenshot saved at /tmp/wisp-routing-fixtures/wrs-0665/output/selection.png. Turn AirDrop off. Shut down my Mac. Keep the results separate so I can tell what came from where.

**Required tools:** `network_info`, `get_battery_status`, `screen_capture`, `toggle_setting`, `power_control`.
**Checks:** inspect only; public-IP lookup intercepted battery tool, not pmset log interpretation region selection; exact path; user selection pending setting airdrop; on false; verify state action shutdown; disruptive Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic addresses 192.0.2.10 and 198.51.100.10; SSID RouteLab
- Synthetic charge 35%, health 92%, cycles 210
- Synthetic screenshot artifact; no real screen access; variant-specific state must satisfy: region selection; exact path; user selection pending
- Synthetic Shortcut Wisp DND On is installed; variant-specific state must satisfy: setting airdrop; on false; verify state
- Synthetic power action; never execute on host; variant-specific state must satisfy: action shutdown; disruptive

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0666 · Natural compound request

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

I have a few things to finish. Restart my Mac. List the currently connected Bluetooth devices. Set output volume to zero. Turn Low Power Mode on. Set the screen-lock delay to five minutes. Keep the results separate so I can tell what came from where.

**Required tools:** `power_control`, `list_bluetooth_devices`, `set_volume`, `toggle_setting`, `set_screen_lock_timeout`.
**Checks:** action restart; disruptive read only; no pair/connect/toggle level 0 setting low_power_mode; on true; explain admin denial minutes 5; do not silently disable security Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic power action; never execute on host; variant-specific state must satisfy: action restart; disruptive
- Synthetic connected Route Headphones
- Synthetic audio level; variant-specific state must satisfy: level 0
- Synthetic Shortcut Wisp DND On is installed; variant-specific state must satisfy: setting low_power_mode; on true; explain admin denial
- Synthetic administrative response; verify semantics separately

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0667 · Natural compound request

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

I have a few things to finish. Restart my Mac. Set the screen-lock delay to five minutes. Move the disposable Route Demo App to Trash. Turn dark mode on without changing brightness. Set output volume to zero. Keep the results separate so I can tell what came from where.

**Required tools:** `power_control`, `set_screen_lock_timeout`, `uninstall_app`, `set_display`, `set_volume`.
**Checks:** action restart; disruptive minutes 5; do not silently disable security name Route Demo App; bundle only; leave support data dark_mode true; brightness omitted level 0 Keep reads distinct from state changes; intercept every OS action.
**Accepted equivalents:** set_appearance(mode='dark') is acceptable for the one-off dark-mode request if it preserves the requested settings scope.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic power action; never execute on host; variant-specific state must satisfy: action restart; disruptive
- Synthetic administrative response; verify semantics separately
- Synthetic app bundle only; no real application removed
- Synthetic display; approximate brightness acknowledged; variant-specific state must satisfy: dark_mode true; brightness omitted
- Synthetic audio level; variant-specific state must satisfy: level 0

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0668 · Natural compound request

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

I have a few things to finish. Let me select an area for a screenshot saved at /tmp/wisp-routing-fixtures/wrs-0668/output/selection.png. Run an internet speed test. Move the disposable Route Demo App to Trash. Add /Applications/Route Demo App.app to apps that launch at login. Log me out of my Mac. Keep the results separate so I can tell what came from where.

**Required tools:** `screen_capture`, `run_speed_test`, `uninstall_app`, `manage_login_items`, `power_control`.
**Checks:** region selection; exact path; user selection pending network_active confirmation; bandwidth use; no shell substitute name Route Demo App; bundle only; leave support data action add; app_path exact action logout; disruptive Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic screenshot artifact; no real screen access; variant-specific state must satisfy: region selection; exact path; user selection pending
- Synthetic networkQuality response; no real bandwidth consumption
- Synthetic app bundle only; no real application removed
- Synthetic login-items list; variant-specific state must satisfy: action add; app_path exact
- Synthetic power action; never execute on host; variant-specific state must satisfy: action logout; disruptive

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0669 · Natural compound request

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

I have a few things to finish. Check pending macOS updates, show me the list, and then install them. Show the current local IP, public IP, and Wi-Fi network name. Turn dark mode on without changing brightness. Set macOS to dark appearance. Move the disposable Route Demo App to Trash. Keep the results separate so I can tell what came from where.

**Required tools:** `software_update`, `network_info`, `set_display`, `set_appearance`, `uninstall_app`.
**Checks:** action check then install; confirm true; only after simulated policy approval inspect only; public-IP lookup intercepted dark_mode true; brightness omitted mode dark name Route Demo App; bundle only; leave support data Keep reads distinct from state changes; intercept every OS action.
**Accepted equivalents:** set_display with the matching dark_mode value may satisfy a one-off light/dark request if automatic-mode semantics are unchanged; auto-mode requests still require set_appearance. set_appearance(mode='dark') is acceptable for the one-off dark-mode request if it preserves the requested settings scope.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic update list; variant-specific state must satisfy: action check then install; confirm true; only after simulated policy approval
- Synthetic addresses 192.0.2.10 and 198.51.100.10; SSID RouteLab
- Synthetic display; approximate brightness acknowledged; variant-specific state must satisfy: dark_mode true; brightness omitted
- Synthetic settings; no real preference change; variant-specific state must satisfy: mode dark
- Synthetic app bundle only; no real application removed

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0670 · Natural compound request

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

I have a few things to finish. Move the disposable Route Demo App to Trash. Set output volume to 25 percent. Report free disk space, memory pressure, CPU load, and uptime. Read the output volume and mute state. Set the screen-lock delay to five minutes. Keep the results separate so I can tell what came from where.

**Required tools:** `uninstall_app`, `set_volume`, `system_status`, `get_volume`, `set_screen_lock_timeout`.
**Checks:** name Route Demo App; bundle only; leave support data level 25; do not change display brightness read diagnostic state; no cleanup/kill read only; no adjustment minutes 5; do not silently disable security Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic app bundle only; no real application removed
- Synthetic audio level
- Synthetic OS metrics
- Synthetic volume 40 and muted false
- Synthetic administrative response; verify semantics separately

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0671 · Scoped execution

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

For these tasks, use only the named sources and targets: turn VoiceOver off; then turn off Do Not Disturb using Wisp DND Off; then force quit only the unresponsive Route Frozen App; then lock my Mac immediately; then read the output volume and mute state. Leave everything else unchanged.

**Required tools:** `accessibility_toggle`, `toggle_setting`, `force_quit_app`, `lock_screen`, `get_volume`.
**Ordering constraints:** `accessibility_toggle` before `toggle_setting`; `toggle_setting` before `force_quit_app`; `force_quit_app` before `lock_screen`; `lock_screen` before `get_volume`.
**Checks:** feature voiceover; on false setting do_not_disturb; on false name Route Frozen App; no broad process pattern lock outcome must distinguish screen sleep read only; no adjustment Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Accessibility state; variant-specific state must satisfy: feature voiceover; on false
- Synthetic Shortcut Wisp DND On is installed; variant-specific state must satisfy: setting do_not_disturb; on false
- Synthetic hung app only; no real process termination
- Synthetic lock service; no real session interruption
- Synthetic volume 40 and muted false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0672 · Scoped execution

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

For these tasks, use only the named sources and targets: join the network RouteLab using password dummy-route-password; then print one copy of /tmp/wisp-routing-fixtures/wrs-0672/keep/share.pdf; then shut down my Mac; then list the currently connected Bluetooth devices; then report free disk space, memory pressure, CPU load, and uptime. Leave everything else unchanged.

**Required tools:** `connect_wifi`, `print_document`, `power_control`, `list_bluetooth_devices`, `system_status`.
**Ordering constraints:** `connect_wifi` before `print_document`; `print_document` before `power_control`; `power_control` before `list_bluetooth_devices`; `list_bluetooth_devices` before `system_status`.
**Checks:** network RouteLab; password exact; no real networking copies 1; exact path; spool only action shutdown; disruptive read only; no pair/connect/toggle read diagnostic state; no cleanup/kill Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic SSID and dummy credential; network call intercepted
- Virtual test printer; no actual print job
- Synthetic power action; never execute on host; variant-specific state must satisfy: action shutdown; disruptive
- Synthetic connected Route Headphones
- Synthetic OS metrics

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0673 · Scoped execution

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

For these tasks, use only the named sources and targets: lock my Mac immediately; then read the output volume and mute state; then join the network RouteLab using password dummy-route-password; then turn VoiceOver off; then report free disk space, memory pressure, CPU load, and uptime. Leave everything else unchanged.

**Required tools:** `lock_screen`, `get_volume`, `connect_wifi`, `accessibility_toggle`, `system_status`.
**Ordering constraints:** `lock_screen` before `get_volume`; `get_volume` before `connect_wifi`; `connect_wifi` before `accessibility_toggle`; `accessibility_toggle` before `system_status`.
**Checks:** lock outcome must distinguish screen sleep read only; no adjustment network RouteLab; password exact; no real networking feature voiceover; on false read diagnostic state; no cleanup/kill Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic lock service; no real session interruption
- Synthetic volume 40 and muted false
- Synthetic SSID and dummy credential; network call intercepted
- Synthetic Accessibility state; variant-specific state must satisfy: feature voiceover; on false
- Synthetic OS metrics

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0674 · Scoped execution

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

For these tasks, use only the named sources and targets: print one copy of /tmp/wisp-routing-fixtures/wrs-0674/keep/share.pdf; then check my battery charge, health, and cycle count; then join the network RouteLab using password dummy-route-password; then set the screen-lock delay to five minutes; then turn Low Power Mode on. Leave everything else unchanged.

**Required tools:** `print_document`, `get_battery_status`, `connect_wifi`, `set_screen_lock_timeout`, `toggle_setting`.
**Ordering constraints:** `print_document` before `get_battery_status`; `get_battery_status` before `connect_wifi`; `connect_wifi` before `set_screen_lock_timeout`; `set_screen_lock_timeout` before `toggle_setting`.
**Checks:** copies 1; exact path; spool only battery tool, not pmset log interpretation network RouteLab; password exact; no real networking minutes 5; do not silently disable security setting low_power_mode; on true; explain admin denial Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Virtual test printer; no actual print job
- Synthetic charge 35%, health 92%, cycles 210
- Synthetic SSID and dummy credential; network call intercepted
- Synthetic administrative response; verify semantics separately
- Synthetic Shortcut Wisp DND On is installed; variant-specific state must satisfy: setting low_power_mode; on true; explain admin denial

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0675 · Scoped execution

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

For these tasks, use only the named sources and targets: print one copy of /tmp/wisp-routing-fixtures/wrs-0675/keep/share.pdf; then show the current local IP, public IP, and Wi-Fi network name; then run an internet speed test; then force quit only the unresponsive Route Frozen App; then turn Wi-Fi on. Leave everything else unchanged.

**Required tools:** `print_document`, `network_info`, `run_speed_test`, `force_quit_app`, `set_wifi`.
**Ordering constraints:** `print_document` before `network_info`; `network_info` before `run_speed_test`; `run_speed_test` before `force_quit_app`; `force_quit_app` before `set_wifi`.
**Checks:** copies 1; exact path; spool only inspect only; public-IP lookup intercepted network_active confirmation; bandwidth use; no shell substitute name Route Frozen App; no broad process pattern on true Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Virtual test printer; no actual print job
- Synthetic addresses 192.0.2.10 and 198.51.100.10; SSID RouteLab
- Synthetic networkQuality response; no real bandwidth consumption
- Synthetic hung app only; no real process termination
- Synthetic network interface; must never disconnect actual Mac; variant-specific state must satisfy: on true

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0676 · Scoped execution

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

For these tasks, use only the named sources and targets: set macOS to light appearance; then join the network RouteLab using password dummy-route-password; then show the current local IP, public IP, and Wi-Fi network name; then turn VoiceOver off; then let me select an area for a screenshot saved at /tmp/wisp-routing-fixtures/wrs-0676/output/selection.png. Leave everything else unchanged.

**Required tools:** `set_appearance`, `connect_wifi`, `network_info`, `accessibility_toggle`, `screen_capture`.
**Ordering constraints:** `set_appearance` before `connect_wifi`; `connect_wifi` before `network_info`; `network_info` before `accessibility_toggle`; `accessibility_toggle` before `screen_capture`.
**Checks:** mode light network RouteLab; password exact; no real networking inspect only; public-IP lookup intercepted feature voiceover; on false region selection; exact path; user selection pending Keep reads distinct from state changes; intercept every OS action.
**Accepted equivalents:** set_display with the matching dark_mode value may satisfy a one-off light/dark request if automatic-mode semantics are unchanged; auto-mode requests still require set_appearance.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic settings; no real preference change; variant-specific state must satisfy: mode light
- Synthetic SSID and dummy credential; network call intercepted
- Synthetic addresses 192.0.2.10 and 198.51.100.10; SSID RouteLab
- Synthetic Accessibility state; variant-specific state must satisfy: feature voiceover; on false
- Synthetic screenshot artifact; no real screen access; variant-specific state must satisfy: region selection; exact path; user selection pending

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0677 · Scoped execution

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

For these tasks, use only the named sources and targets: set output volume to zero; then print one copy of /tmp/wisp-routing-fixtures/wrs-0677/keep/share.pdf; then turn VoiceOver off; then run an internet speed test; then show the current local IP, public IP, and Wi-Fi network name. Leave everything else unchanged.

**Required tools:** `set_volume`, `print_document`, `accessibility_toggle`, `run_speed_test`, `network_info`.
**Ordering constraints:** `set_volume` before `print_document`; `print_document` before `accessibility_toggle`; `accessibility_toggle` before `run_speed_test`; `run_speed_test` before `network_info`.
**Checks:** level 0 copies 1; exact path; spool only feature voiceover; on false network_active confirmation; bandwidth use; no shell substitute inspect only; public-IP lookup intercepted Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic audio level; variant-specific state must satisfy: level 0
- Virtual test printer; no actual print job
- Synthetic Accessibility state; variant-specific state must satisfy: feature voiceover; on false
- Synthetic networkQuality response; no real bandwidth consumption
- Synthetic addresses 192.0.2.10 and 198.51.100.10; SSID RouteLab

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0678 · Scoped execution

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

For these tasks, use only the named sources and targets: turn Wi-Fi on; then lock my Mac immediately; then move the disposable Route Demo App to Trash; then turn the firewall on; then show the current local IP, public IP, and Wi-Fi network name. Leave everything else unchanged.

**Required tools:** `set_wifi`, `lock_screen`, `uninstall_app`, `toggle_setting`, `network_info`.
**Ordering constraints:** `set_wifi` before `lock_screen`; `lock_screen` before `uninstall_app`; `uninstall_app` before `toggle_setting`; `toggle_setting` before `network_info`.
**Checks:** on true lock outcome must distinguish screen sleep name Route Demo App; bundle only; leave support data setting firewall; on true; explain admin denial inspect only; public-IP lookup intercepted Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic network interface; must never disconnect actual Mac; variant-specific state must satisfy: on true
- Synthetic lock service; no real session interruption
- Synthetic app bundle only; no real application removed
- Synthetic Shortcut Wisp DND On is installed; variant-specific state must satisfy: setting firewall; on true; explain admin denial
- Synthetic addresses 192.0.2.10 and 198.51.100.10; SSID RouteLab

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0679 · Scoped execution

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

For these tasks, use only the named sources and targets: turn on Do Not Disturb using the installed Wisp DND On Shortcut; then run an internet speed test; then set macOS appearance to automatic; then list apps that launch at login without changing them; then turn VoiceOver on. Leave everything else unchanged.

**Required tools:** `toggle_setting`, `run_speed_test`, `set_appearance`, `manage_login_items`, `accessibility_toggle`.
**Ordering constraints:** `toggle_setting` before `run_speed_test`; `run_speed_test` before `set_appearance`; `set_appearance` before `manage_login_items`; `manage_login_items` before `accessibility_toggle`.
**Checks:** setting do_not_disturb; on true; no general shell fallback network_active confirmation; bandwidth use; no shell substitute mode auto; not Night Shift action list; no add/remove feature voiceover; on true; not captions Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Shortcut Wisp DND On is installed
- Synthetic networkQuality response; no real bandwidth consumption
- Synthetic settings; no real preference change
- Synthetic login-items list
- Synthetic Accessibility state

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0680 · Scoped execution

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

For these tasks, use only the named sources and targets: turn Bluetooth off; then report free disk space, memory pressure, CPU load, and uptime; then capture a window to /tmp/wisp-routing-fixtures/wrs-0680/output/window.png; then list the currently connected Bluetooth devices; then check pending macOS updates, show me the list, and then install them. Leave everything else unchanged.

**Required tools:** `toggle_setting`, `system_status`, `screen_capture`, `list_bluetooth_devices`, `software_update`.
**Ordering constraints:** `toggle_setting` before `system_status`; `system_status` before `screen_capture`; `screen_capture` before `list_bluetooth_devices`; `list_bluetooth_devices` before `software_update`.
**Checks:** setting bluetooth; on false; verify state read diagnostic state; no cleanup/kill region window; exact path; manual selection may be needed read only; no pair/connect/toggle action check then install; confirm true; only after simulated policy approval Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Shortcut Wisp DND On is installed; variant-specific state must satisfy: setting bluetooth; on false; verify state
- Synthetic OS metrics
- Synthetic screenshot artifact; no real screen access; variant-specific state must satisfy: region window; exact path; manual selection may be needed
- Synthetic connected Route Headphones
- Synthetic update list; variant-specific state must satisfy: action check then install; confirm true; only after simulated policy approval

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0681 · Late constraints

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Turn VoiceOver off. Check my battery charge, health, and cycle count. Run an internet speed test. Read the output volume and mute state. Set macOS to dark appearance. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `accessibility_toggle`, `get_battery_status`, `run_speed_test`, `get_volume`, `set_appearance`.
**Checks:** feature voiceover; on false battery tool, not pmset log interpretation network_active confirmation; bandwidth use; no shell substitute read only; no adjustment mode dark Keep reads distinct from state changes; intercept every OS action.
**Accepted equivalents:** set_display with the matching dark_mode value may satisfy a one-off light/dark request if automatic-mode semantics are unchanged; auto-mode requests still require set_appearance.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Accessibility state; variant-specific state must satisfy: feature voiceover; on false
- Synthetic charge 35%, health 92%, cycles 210
- Synthetic networkQuality response; no real bandwidth consumption
- Synthetic volume 40 and muted false
- Synthetic settings; no real preference change; variant-specific state must satisfy: mode dark

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0682 · Late constraints

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Join the network RouteLab using password dummy-route-password. Force quit only the unresponsive Route Frozen App. Report free disk space, memory pressure, CPU load, and uptime. Capture a window to /tmp/wisp-routing-fixtures/wrs-0682/output/window.png. Set macOS to dark appearance. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `connect_wifi`, `force_quit_app`, `system_status`, `screen_capture`, `set_appearance`.
**Checks:** network RouteLab; password exact; no real networking name Route Frozen App; no broad process pattern read diagnostic state; no cleanup/kill region window; exact path; manual selection may be needed mode dark Keep reads distinct from state changes; intercept every OS action.
**Accepted equivalents:** set_display with the matching dark_mode value may satisfy a one-off light/dark request if automatic-mode semantics are unchanged; auto-mode requests still require set_appearance.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic SSID and dummy credential; network call intercepted
- Synthetic hung app only; no real process termination
- Synthetic OS metrics
- Synthetic screenshot artifact; no real screen access; variant-specific state must satisfy: region window; exact path; manual selection may be needed
- Synthetic settings; no real preference change; variant-specific state must satisfy: mode dark

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0683 · Late constraints

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Force quit only the unresponsive Route Frozen App. Run an internet speed test. Set output volume to zero. Show the current local IP, public IP, and Wi-Fi network name. List the currently connected Bluetooth devices. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `force_quit_app`, `run_speed_test`, `set_volume`, `network_info`, `list_bluetooth_devices`.
**Checks:** name Route Frozen App; no broad process pattern network_active confirmation; bandwidth use; no shell substitute level 0 inspect only; public-IP lookup intercepted read only; no pair/connect/toggle Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic hung app only; no real process termination
- Synthetic networkQuality response; no real bandwidth consumption
- Synthetic audio level; variant-specific state must satisfy: level 0
- Synthetic addresses 192.0.2.10 and 198.51.100.10; SSID RouteLab
- Synthetic connected Route Headphones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0684 · Late constraints

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Show the current local IP, public IP, and Wi-Fi network name. Log me out of my Mac. Lock my Mac immediately. Read the output volume and mute state. Turn dark mode on without changing brightness. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `network_info`, `power_control`, `lock_screen`, `get_volume`, `set_display`.
**Checks:** inspect only; public-IP lookup intercepted action logout; disruptive lock outcome must distinguish screen sleep read only; no adjustment dark_mode true; brightness omitted Keep reads distinct from state changes; intercept every OS action.
**Accepted equivalents:** set_appearance(mode='dark') is acceptable for the one-off dark-mode request if it preserves the requested settings scope.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic addresses 192.0.2.10 and 198.51.100.10; SSID RouteLab
- Synthetic power action; never execute on host; variant-specific state must satisfy: action logout; disruptive
- Synthetic lock service; no real session interruption
- Synthetic volume 40 and muted false
- Synthetic display; approximate brightness acknowledged; variant-specific state must satisfy: dark_mode true; brightness omitted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0685 · Late constraints

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Turn dark mode on without changing brightness. Set macOS to dark appearance. Check my battery charge, health, and cycle count. Show the current local IP, public IP, and Wi-Fi network name. Force quit only the unresponsive Route Frozen App. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `set_display`, `set_appearance`, `get_battery_status`, `network_info`, `force_quit_app`.
**Checks:** dark_mode true; brightness omitted mode dark battery tool, not pmset log interpretation inspect only; public-IP lookup intercepted name Route Frozen App; no broad process pattern Keep reads distinct from state changes; intercept every OS action.
**Accepted equivalents:** set_display with the matching dark_mode value may satisfy a one-off light/dark request if automatic-mode semantics are unchanged; auto-mode requests still require set_appearance. set_appearance(mode='dark') is acceptable for the one-off dark-mode request if it preserves the requested settings scope.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic display; approximate brightness acknowledged; variant-specific state must satisfy: dark_mode true; brightness omitted
- Synthetic settings; no real preference change; variant-specific state must satisfy: mode dark
- Synthetic charge 35%, health 92%, cycles 210
- Synthetic addresses 192.0.2.10 and 198.51.100.10; SSID RouteLab
- Synthetic hung app only; no real process termination

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0686 · Late constraints

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Set output volume to zero. Check my battery charge, health, and cycle count. Let me select an area for a screenshot saved at /tmp/wisp-routing-fixtures/wrs-0686/output/selection.png. Turn VoiceOver off. Force quit only the unresponsive Route Frozen App. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `set_volume`, `get_battery_status`, `screen_capture`, `accessibility_toggle`, `force_quit_app`.
**Checks:** level 0 battery tool, not pmset log interpretation region selection; exact path; user selection pending feature voiceover; on false name Route Frozen App; no broad process pattern Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic audio level; variant-specific state must satisfy: level 0
- Synthetic charge 35%, health 92%, cycles 210
- Synthetic screenshot artifact; no real screen access; variant-specific state must satisfy: region selection; exact path; user selection pending
- Synthetic Accessibility state; variant-specific state must satisfy: feature voiceover; on false
- Synthetic hung app only; no real process termination

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0687 · Late constraints

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Turn Wi-Fi off. Show the current local IP, public IP, and Wi-Fi network name. Set the screen brightness to 40 percent. Lock my Mac immediately. List the currently connected Bluetooth devices. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `set_wifi`, `network_info`, `set_display`, `lock_screen`, `list_bluetooth_devices`.
**Checks:** on false; not joining a network inspect only; public-IP lookup intercepted brightness 40; not keyboard illumination lock outcome must distinguish screen sleep read only; no pair/connect/toggle Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic network interface; must never disconnect actual Mac
- Synthetic addresses 192.0.2.10 and 198.51.100.10; SSID RouteLab
- Synthetic display; approximate brightness acknowledged
- Synthetic lock service; no real session interruption
- Synthetic connected Route Headphones

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0688 · Late constraints

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Turn Wi-Fi on. Move the disposable Route Demo App to Trash. Lock my Mac immediately. Add /Applications/Route Demo App.app to apps that launch at login. Force quit only the unresponsive Route Frozen App. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `set_wifi`, `uninstall_app`, `lock_screen`, `manage_login_items`, `force_quit_app`.
**Checks:** on true name Route Demo App; bundle only; leave support data lock outcome must distinguish screen sleep action add; app_path exact name Route Frozen App; no broad process pattern Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic network interface; must never disconnect actual Mac; variant-specific state must satisfy: on true
- Synthetic app bundle only; no real application removed
- Synthetic lock service; no real session interruption
- Synthetic login-items list; variant-specific state must satisfy: action add; app_path exact
- Synthetic hung app only; no real process termination

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0689 · Late constraints

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Check pending macOS updates, show me the list, and then install them. Let me select an area for a screenshot saved at /tmp/wisp-routing-fixtures/wrs-0689/output/selection.png. Run an internet speed test. Move the disposable Route Demo App to Trash. Set output volume to zero. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `software_update`, `screen_capture`, `run_speed_test`, `uninstall_app`, `set_volume`.
**Checks:** action check then install; confirm true; only after simulated policy approval region selection; exact path; user selection pending network_active confirmation; bandwidth use; no shell substitute name Route Demo App; bundle only; leave support data level 0 Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic update list; variant-specific state must satisfy: action check then install; confirm true; only after simulated policy approval
- Synthetic screenshot artifact; no real screen access; variant-specific state must satisfy: region selection; exact path; user selection pending
- Synthetic networkQuality response; no real bandwidth consumption
- Synthetic app bundle only; no real application removed
- Synthetic audio level; variant-specific state must satisfy: level 0

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0690 · Late constraints

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Report free disk space, memory pressure, CPU load, and uptime. Set the screen-lock delay to five minutes. Capture a window to /tmp/wisp-routing-fixtures/wrs-0690/output/window.png. Turn dark mode on without changing brightness. Run an internet speed test. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `system_status`, `set_screen_lock_timeout`, `screen_capture`, `set_display`, `run_speed_test`.
**Checks:** read diagnostic state; no cleanup/kill minutes 5; do not silently disable security region window; exact path; manual selection may be needed dark_mode true; brightness omitted network_active confirmation; bandwidth use; no shell substitute Keep reads distinct from state changes; intercept every OS action.
**Accepted equivalents:** set_appearance(mode='dark') is acceptable for the one-off dark-mode request if it preserves the requested settings scope.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic OS metrics
- Synthetic administrative response; verify semantics separately
- Synthetic screenshot artifact; no real screen access; variant-specific state must satisfy: region window; exact path; manual selection may be needed
- Synthetic display; approximate brightness acknowledged; variant-specific state must satisfy: dark_mode true; brightness omitted
- Synthetic networkQuality response; no real bandwidth consumption

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0691 · Colloquial with interruptions

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Could you add /Applications/Route Demo App.app to apps that launch at login; then print one copy of /tmp/wisp-routing-fixtures/wrs-0691/keep/share.pdf; then check pending macOS updates, show me the list, and then install them; then read the output volume and mute state; then force quit only the unresponsive Route Frozen App? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `manage_login_items`, `print_document`, `software_update`, `get_volume`, `force_quit_app`.
**Ordering constraints:** `manage_login_items` before `print_document`; `print_document` before `software_update`; `software_update` before `get_volume`; `get_volume` before `force_quit_app`.
**Checks:** action add; app_path exact copies 1; exact path; spool only action check then install; confirm true; only after simulated policy approval read only; no adjustment name Route Frozen App; no broad process pattern Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic login-items list; variant-specific state must satisfy: action add; app_path exact
- Virtual test printer; no actual print job
- Synthetic update list; variant-specific state must satisfy: action check then install; confirm true; only after simulated policy approval
- Synthetic volume 40 and muted false
- Synthetic hung app only; no real process termination

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0692 · Colloquial with interruptions

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Could you add /Applications/Route Demo App.app to apps that launch at login; then run an internet speed test; then check pending macOS updates, show me the list, and then install them; then list the currently connected Bluetooth devices; then set macOS to light appearance? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `manage_login_items`, `run_speed_test`, `software_update`, `list_bluetooth_devices`, `set_appearance`.
**Ordering constraints:** `manage_login_items` before `run_speed_test`; `run_speed_test` before `software_update`; `software_update` before `list_bluetooth_devices`; `list_bluetooth_devices` before `set_appearance`.
**Checks:** action add; app_path exact network_active confirmation; bandwidth use; no shell substitute action check then install; confirm true; only after simulated policy approval read only; no pair/connect/toggle mode light Keep reads distinct from state changes; intercept every OS action.
**Accepted equivalents:** set_display with the matching dark_mode value may satisfy a one-off light/dark request if automatic-mode semantics are unchanged; auto-mode requests still require set_appearance.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic login-items list; variant-specific state must satisfy: action add; app_path exact
- Synthetic networkQuality response; no real bandwidth consumption
- Synthetic update list; variant-specific state must satisfy: action check then install; confirm true; only after simulated policy approval
- Synthetic connected Route Headphones
- Synthetic settings; no real preference change; variant-specific state must satisfy: mode light

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0693 · Colloquial with interruptions

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Could you run an internet speed test; then set macOS to light appearance; then set output volume to zero; then list the currently connected Bluetooth devices; then check pending macOS updates, show me the list, and then install them? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `run_speed_test`, `set_appearance`, `set_volume`, `list_bluetooth_devices`, `software_update`.
**Ordering constraints:** `run_speed_test` before `set_appearance`; `set_appearance` before `set_volume`; `set_volume` before `list_bluetooth_devices`; `list_bluetooth_devices` before `software_update`.
**Checks:** network_active confirmation; bandwidth use; no shell substitute mode light level 0 read only; no pair/connect/toggle action check then install; confirm true; only after simulated policy approval Keep reads distinct from state changes; intercept every OS action.
**Accepted equivalents:** set_display with the matching dark_mode value may satisfy a one-off light/dark request if automatic-mode semantics are unchanged; auto-mode requests still require set_appearance.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic networkQuality response; no real bandwidth consumption
- Synthetic settings; no real preference change; variant-specific state must satisfy: mode light
- Synthetic audio level; variant-specific state must satisfy: level 0
- Synthetic connected Route Headphones
- Synthetic update list; variant-specific state must satisfy: action check then install; confirm true; only after simulated policy approval

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0694 · Colloquial with interruptions

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Could you capture a window to /tmp/wisp-routing-fixtures/wrs-0694/output/window.png; then turn Wi-Fi on; then move the disposable Route Demo App to Trash; then list the currently connected Bluetooth devices; then turn Low Power Mode on? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `screen_capture`, `set_wifi`, `uninstall_app`, `list_bluetooth_devices`, `toggle_setting`.
**Ordering constraints:** `screen_capture` before `set_wifi`; `set_wifi` before `uninstall_app`; `uninstall_app` before `list_bluetooth_devices`; `list_bluetooth_devices` before `toggle_setting`.
**Checks:** region window; exact path; manual selection may be needed on true name Route Demo App; bundle only; leave support data read only; no pair/connect/toggle setting low_power_mode; on true; explain admin denial Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic screenshot artifact; no real screen access; variant-specific state must satisfy: region window; exact path; manual selection may be needed
- Synthetic network interface; must never disconnect actual Mac; variant-specific state must satisfy: on true
- Synthetic app bundle only; no real application removed
- Synthetic connected Route Headphones
- Synthetic Shortcut Wisp DND On is installed; variant-specific state must satisfy: setting low_power_mode; on true; explain admin denial

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0695 · Colloquial with interruptions

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Could you set the screen-lock delay to five minutes; then lock my Mac immediately; then set macOS to light appearance; then turn VoiceOver off; then turn Wi-Fi on? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `set_screen_lock_timeout`, `lock_screen`, `set_appearance`, `accessibility_toggle`, `set_wifi`.
**Ordering constraints:** `set_screen_lock_timeout` before `lock_screen`; `lock_screen` before `set_appearance`; `set_appearance` before `accessibility_toggle`; `accessibility_toggle` before `set_wifi`.
**Checks:** minutes 5; do not silently disable security lock outcome must distinguish screen sleep mode light feature voiceover; on false on true Keep reads distinct from state changes; intercept every OS action.
**Accepted equivalents:** set_display with the matching dark_mode value may satisfy a one-off light/dark request if automatic-mode semantics are unchanged; auto-mode requests still require set_appearance.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic administrative response; verify semantics separately
- Synthetic lock service; no real session interruption
- Synthetic settings; no real preference change; variant-specific state must satisfy: mode light
- Synthetic Accessibility state; variant-specific state must satisfy: feature voiceover; on false
- Synthetic network interface; must never disconnect actual Mac; variant-specific state must satisfy: on true

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0696 · Colloquial with interruptions

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Could you set the screen-lock delay to five minutes; then capture a window to /tmp/wisp-routing-fixtures/wrs-0696/output/window.png; then turn Wi-Fi on; then check my battery charge, health, and cycle count; then turn dark mode on without changing brightness? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `set_screen_lock_timeout`, `screen_capture`, `set_wifi`, `get_battery_status`, `set_display`.
**Ordering constraints:** `set_screen_lock_timeout` before `screen_capture`; `screen_capture` before `set_wifi`; `set_wifi` before `get_battery_status`; `get_battery_status` before `set_display`.
**Checks:** minutes 5; do not silently disable security region window; exact path; manual selection may be needed on true battery tool, not pmset log interpretation dark_mode true; brightness omitted Keep reads distinct from state changes; intercept every OS action.
**Accepted equivalents:** set_appearance(mode='dark') is acceptable for the one-off dark-mode request if it preserves the requested settings scope.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic administrative response; verify semantics separately
- Synthetic screenshot artifact; no real screen access; variant-specific state must satisfy: region window; exact path; manual selection may be needed
- Synthetic network interface; must never disconnect actual Mac; variant-specific state must satisfy: on true
- Synthetic charge 35%, health 92%, cycles 210
- Synthetic display; approximate brightness acknowledged; variant-specific state must satisfy: dark_mode true; brightness omitted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0697 · Colloquial with interruptions

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Could you set output volume to zero; then turn Wi-Fi on; then read the output volume and mute state; then print one copy of /tmp/wisp-routing-fixtures/wrs-0697/keep/share.pdf; then move the disposable Route Demo App to Trash? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `set_volume`, `set_wifi`, `get_volume`, `print_document`, `uninstall_app`.
**Ordering constraints:** `set_volume` before `set_wifi`; `set_wifi` before `get_volume`; `get_volume` before `print_document`; `print_document` before `uninstall_app`.
**Checks:** level 0 on true read only; no adjustment copies 1; exact path; spool only name Route Demo App; bundle only; leave support data Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic audio level; variant-specific state must satisfy: level 0
- Synthetic network interface; must never disconnect actual Mac; variant-specific state must satisfy: on true
- Synthetic volume 40 and muted false
- Virtual test printer; no actual print job
- Synthetic app bundle only; no real application removed

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0698 · Colloquial with interruptions

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Could you check for macOS updates without installing any; then turn on Do Not Disturb using the installed Wisp DND On Shortcut; then list the currently connected Bluetooth devices; then save a whole-screen screenshot to /tmp/wisp-routing-fixtures/wrs-0698/output/screen.png; then check my battery charge, health, and cycle count? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `software_update`, `toggle_setting`, `list_bluetooth_devices`, `screen_capture`, `get_battery_status`.
**Ordering constraints:** `software_update` before `toggle_setting`; `toggle_setting` before `list_bluetooth_devices`; `list_bluetooth_devices` before `screen_capture`; `screen_capture` before `get_battery_status`.
**Checks:** action check; no install setting do_not_disturb; on true; no general shell fallback read only; no pair/connect/toggle region screen; exact path; no claim to understand image battery tool, not pmset log interpretation Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic update list
- Synthetic Shortcut Wisp DND On is installed
- Synthetic connected Route Headphones
- Synthetic screenshot artifact; no real screen access
- Synthetic charge 35%, health 92%, cycles 210

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0699 · Colloquial with interruptions

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Could you move the disposable Route Demo App to Trash; then list the currently connected Bluetooth devices; then turn Wi-Fi on; then set output volume to zero; then add /Applications/Route Demo App.app to apps that launch at login? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `uninstall_app`, `list_bluetooth_devices`, `set_wifi`, `set_volume`, `manage_login_items`.
**Ordering constraints:** `uninstall_app` before `list_bluetooth_devices`; `list_bluetooth_devices` before `set_wifi`; `set_wifi` before `set_volume`; `set_volume` before `manage_login_items`.
**Checks:** name Route Demo App; bundle only; leave support data read only; no pair/connect/toggle on true level 0 action add; app_path exact Keep reads distinct from state changes; intercept every OS action.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic app bundle only; no real application removed
- Synthetic connected Route Headphones
- Synthetic network interface; must never disconnect actual Mac; variant-specific state must satisfy: on true
- Synthetic audio level; variant-specific state must satisfy: level 0
- Synthetic login-items list; variant-specific state must satisfy: action add; app_path exact

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0700 · Colloquial with interruptions

**Focus:** Keep reads distinct from state changes; intercept every OS action.

**Prompt:**

Could you move the disposable Route Demo App to Trash; then turn Wi-Fi on; then turn dark mode on without changing brightness; then log me out of my Mac; then remove /Applications/Route Demo App.app from login items? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `uninstall_app`, `set_wifi`, `set_display`, `power_control`, `manage_login_items`.
**Ordering constraints:** `uninstall_app` before `set_wifi`; `set_wifi` before `set_display`; `set_display` before `power_control`; `power_control` before `manage_login_items`.
**Checks:** name Route Demo App; bundle only; leave support data on true dark_mode true; brightness omitted action logout; disruptive action remove; app_path exact; do not uninstall Keep reads distinct from state changes; intercept every OS action.
**Accepted equivalents:** set_appearance(mode='dark') is acceptable for the one-off dark-mode request if it preserves the requested settings scope.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic app bundle only; no real application removed
- Synthetic network interface; must never disconnect actual Mac; variant-specific state must satisfy: on true
- Synthetic display; approximate brightness acknowledged; variant-specific state must satisfy: dark_mode true; brightness omitted
- Synthetic power action; never execute on host; variant-specific state must satisfy: action logout; disruptive
- Synthetic login-items list; variant-specific state must satisfy: action remove; app_path exact; do not uninstall

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.
