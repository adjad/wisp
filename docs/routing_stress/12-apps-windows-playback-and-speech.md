# Review only — no tests run

## 12. Apps, windows, playback, and speech

Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

### WRS-0551 · Explicit sequence

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Please do these in this order: list the apps currently open and identify the frontmost one; then open Calculator; then switch one desktop to the right; then maximize Safari's front window; then stop the ambient sound you started earlier.

**Required tools:** `list_running_apps`, `open_app`, `manage_spaces`, `window_control`, `play_ambient`.
**Ordering constraints:** `list_running_apps` before `open_app`; `open_app` before `manage_spaces`; `manage_spaces` before `window_control`; `window_control` before `play_ambient`.
**Checks:** read process list; no launch/quit name Calculator; launch only direction right action zoom; app Safari kind stop; no invented stop_ambient tool Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Calculator, Safari and Music processes
- Synthetic app launch
- Synthetic Mission Control shortcuts enabled; variant-specific state must satisfy: direction right
- Synthetic Accessibility window bounds; variant-specific state must satisfy: action zoom; app Safari
- Synthetic playback process only; variant-specific state must satisfy: kind stop; no invented stop_ambient tool

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0552 · Explicit sequence

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Please do these in this order: switch one desktop to the right; then open Calculator; then stop the ambient sound you started earlier; then open Books so I can choose my audiobook; then open the Netflix app so I can pick a show.

**Required tools:** `manage_spaces`, `open_app`, `play_ambient`, `play_audiobook`, `play_streaming`.
**Ordering constraints:** `manage_spaces` before `open_app`; `open_app` before `play_ambient`; `play_ambient` before `play_audiobook`; `play_audiobook` before `play_streaming`.
**Checks:** direction right name Calculator; launch only kind stop; no invented stop_ambient tool app handoff only; no claim of resuming playback app Netflix; launch only Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Mission Control shortcuts enabled; variant-specific state must satisfy: direction right
- Synthetic app launch
- Synthetic playback process only; variant-specific state must satisfy: kind stop; no invented stop_ambient tool
- Synthetic Books launch
- Synthetic installed Netflix app

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0553 · Explicit sequence

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Please do these in this order: pause Apple Music; then open Books so I can choose my audiobook; then minimize Safari's front window; then skip to the next Spotify track; then find and play BBC Radio 1.

**Required tools:** `music`, `play_audiobook`, `window_control`, `spotify`, `play_radio`.
**Ordering constraints:** `music` before `play_audiobook`; `play_audiobook` before `window_control`; `window_control` before `spotify`; `spotify` before `play_radio`.
**Checks:** action pause app handoff only; no claim of resuming playback action minimize; app Safari action next query BBC Radio 1; distinguish launch from verified playback Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic named Music playlist; variant-specific state must satisfy: action pause
- Synthetic Books launch
- Synthetic Accessibility window bounds; variant-specific state must satisfy: action minimize; app Safari
- Synthetic Spotify playback state; variant-specific state must satisfy: action next
- Synthetic station result and intercepted player

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0554 · Explicit sequence

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Please do these in this order: open podcast search for Route Science so I can choose an episode; then stop the internet radio you started earlier; then list the apps currently open and identify the frontmost one; then return to the previous Apple Music track; then exit full screen for Safari's front window.

**Required tools:** `play_podcast`, `stop_radio`, `list_running_apps`, `music`, `window_control`.
**Ordering constraints:** `play_podcast` before `stop_radio`; `stop_radio` before `list_running_apps`; `list_running_apps` before `music`; `music` before `window_control`.
**Checks:** query Route Science; manual play handoff stop radio; acknowledge broad afplay implementation risk read process list; no launch/quit action previous action exit_fullscreen; app Safari Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Podcasts search URI
- Synthetic prior radio session; no real audio processes
- Synthetic Calculator, Safari and Music processes
- Synthetic named Music playlist; variant-specific state must satisfy: action previous
- Synthetic Accessibility window bounds; variant-specific state must satisfy: action exit_fullscreen; app Safari

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0555 · Explicit sequence

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Please do these in this order: open podcast search for Route Science so I can choose an episode; then bring Safari forward without hiding the other apps; then pause Apple Music; then quit the Route Scratch app normally; then open Calculator.

**Required tools:** `play_podcast`, `switch_app`, `music`, `quit_app`, `open_app`.
**Ordering constraints:** `play_podcast` before `switch_app`; `switch_app` before `music`; `music` before `quit_app`; `quit_app` before `open_app`.
**Checks:** query Route Science; manual play handoff name Safari; hide_others false action pause name Route Scratch; graceful quit only name Calculator; launch only Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Podcasts search URI
- Safari already running in synthetic app state; variant-specific state must satisfy: name Safari; hide_others false
- Synthetic named Music playlist; variant-specific state must satisfy: action pause
- Disposable synthetic app with no unsaved work
- Synthetic app launch

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0556 · Explicit sequence

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Please do these in this order: open podcast search for Route Science so I can choose an episode; then tile Safari's front window to the left half of the screen; then open the Netflix app so I can pick a show; then open Books so I can choose my audiobook; then play the Route Focus playlist in Apple Music.

**Required tools:** `play_podcast`, `window_control`, `play_streaming`, `play_audiobook`, `music`.
**Ordering constraints:** `play_podcast` before `window_control`; `window_control` before `play_streaming`; `play_streaming` before `play_audiobook`; `play_audiobook` before `music`.
**Checks:** query Route Science; manual play handoff action left; app Safari; only target window app Netflix; launch only app handoff only; no claim of resuming playback action play_playlist; playlist Route Focus Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Podcasts search URI
- Synthetic Accessibility window bounds
- Synthetic installed Netflix app
- Synthetic Books launch
- Synthetic named Music playlist

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0557 · Explicit sequence

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Please do these in this order: open the Netflix app so I can pick a show; then play white noise for ten minutes; then quit the Route Scratch app normally; then switch one desktop to the left; then bring Safari forward without hiding the other apps.

**Required tools:** `play_streaming`, `play_ambient`, `quit_app`, `manage_spaces`, `switch_app`.
**Ordering constraints:** `play_streaming` before `play_ambient`; `play_ambient` before `quit_app`; `quit_app` before `manage_spaces`; `manage_spaces` before `switch_app`.
**Checks:** app Netflix; launch only kind white_noise; minutes 10 name Route Scratch; graceful quit only direction left name Safari; hide_others false Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic installed Netflix app
- Synthetic playback process only; variant-specific state must satisfy: kind white_noise; minutes 10
- Disposable synthetic app with no unsaved work
- Synthetic Mission Control shortcuts enabled; variant-specific state must satisfy: direction left
- Safari already running in synthetic app state; variant-specific state must satisfy: name Safari; hide_others false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0558 · Explicit sequence

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Please do these in this order: return to the previous Spotify track; then find and play BBC Radio 1; then tile Safari's front window to the right; then list the apps currently open and identify the frontmost one; then open the Netflix app so I can pick a show.

**Required tools:** `spotify`, `play_radio`, `window_control`, `list_running_apps`, `play_streaming`.
**Ordering constraints:** `spotify` before `play_radio`; `play_radio` before `window_control`; `window_control` before `list_running_apps`; `list_running_apps` before `play_streaming`.
**Checks:** action previous query BBC Radio 1; distinguish launch from verified playback action right; app Safari read process list; no launch/quit app Netflix; launch only Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Spotify playback state; variant-specific state must satisfy: action previous
- Synthetic station result and intercepted player
- Synthetic Accessibility window bounds; variant-specific state must satisfy: action right; app Safari
- Synthetic Calculator, Safari and Music processes
- Synthetic installed Netflix app

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0559 · Explicit sequence

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Please do these in this order: bring Safari forward without hiding the other apps; then minimize Safari's front window; then open Calculator; then open podcast search for Route Science so I can choose an episode; then open the Netflix app so I can pick a show.

**Required tools:** `switch_app`, `window_control`, `open_app`, `play_podcast`, `play_streaming`.
**Ordering constraints:** `switch_app` before `window_control`; `window_control` before `open_app`; `open_app` before `play_podcast`; `play_podcast` before `play_streaming`.
**Checks:** name Safari; hide_others false action minimize; app Safari name Calculator; launch only query Route Science; manual play handoff app Netflix; launch only Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Safari already running in synthetic app state; variant-specific state must satisfy: name Safari; hide_others false
- Synthetic Accessibility window bounds; variant-specific state must satisfy: action minimize; app Safari
- Synthetic app launch
- Synthetic Podcasts search URI
- Synthetic installed Netflix app

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0560 · Explicit sequence

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Please do these in this order: maximize Safari's front window; then return to the previous Apple Music track; then stop the ambient sound you started earlier; then return to the previous Spotify track; then open Calculator.

**Required tools:** `window_control`, `music`, `play_ambient`, `spotify`, `open_app`.
**Ordering constraints:** `window_control` before `music`; `music` before `play_ambient`; `play_ambient` before `spotify`; `spotify` before `open_app`.
**Checks:** action zoom; app Safari action previous kind stop; no invented stop_ambient tool action previous name Calculator; launch only Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Accessibility window bounds; variant-specific state must satisfy: action zoom; app Safari
- Synthetic named Music playlist; variant-specific state must satisfy: action previous
- Synthetic playback process only; variant-specific state must satisfy: kind stop; no invented stop_ambient tool
- Synthetic Spotify playback state; variant-specific state must satisfy: action previous
- Synthetic app launch

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0561 · Natural compound request

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

I have a few things to finish. Switch one desktop to the right. Return to the previous Apple Music track. Open Books so I can choose my audiobook. Stop the internet radio you started earlier. Quit the Route Scratch app normally. Keep the results separate so I can tell what came from where.

**Required tools:** `manage_spaces`, `music`, `play_audiobook`, `stop_radio`, `quit_app`.
**Checks:** direction right action previous app handoff only; no claim of resuming playback stop radio; acknowledge broad afplay implementation risk name Route Scratch; graceful quit only Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Mission Control shortcuts enabled; variant-specific state must satisfy: direction right
- Synthetic named Music playlist; variant-specific state must satisfy: action previous
- Synthetic Books launch
- Synthetic prior radio session; no real audio processes
- Disposable synthetic app with no unsaved work

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0562 · Natural compound request

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

I have a few things to finish. Switch one desktop to the right. Skip to the next Spotify track. List the apps currently open and identify the frontmost one. Quit the Route Scratch app normally. Open Calculator. Keep the results separate so I can tell what came from where.

**Required tools:** `manage_spaces`, `spotify`, `list_running_apps`, `quit_app`, `open_app`.
**Checks:** direction right action next read process list; no launch/quit name Route Scratch; graceful quit only name Calculator; launch only Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Mission Control shortcuts enabled; variant-specific state must satisfy: direction right
- Synthetic Spotify playback state; variant-specific state must satisfy: action next
- Synthetic Calculator, Safari and Music processes
- Disposable synthetic app with no unsaved work
- Synthetic app launch

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0563 · Natural compound request

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

I have a few things to finish. Pause Apple Music. Stop the internet radio you started earlier. Open Calculator. Return to the previous Spotify track. Bring Safari forward without hiding the other apps. Keep the results separate so I can tell what came from where.

**Required tools:** `music`, `stop_radio`, `open_app`, `spotify`, `switch_app`.
**Checks:** action pause stop radio; acknowledge broad afplay implementation risk name Calculator; launch only action previous name Safari; hide_others false Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic named Music playlist; variant-specific state must satisfy: action pause
- Synthetic prior radio session; no real audio processes
- Synthetic app launch
- Synthetic Spotify playback state; variant-specific state must satisfy: action previous
- Safari already running in synthetic app state; variant-specific state must satisfy: name Safari; hide_others false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0564 · Natural compound request

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

I have a few things to finish. Open Calculator. Stop the internet radio you started earlier. Switch one desktop to the left. Resume Spotify playback. Skip to the next Apple Music track. Keep the results separate so I can tell what came from where.

**Required tools:** `open_app`, `stop_radio`, `manage_spaces`, `spotify`, `music`.
**Checks:** name Calculator; launch only stop radio; acknowledge broad afplay implementation risk direction left action play action next Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic app launch
- Synthetic prior radio session; no real audio processes
- Synthetic Mission Control shortcuts enabled; variant-specific state must satisfy: direction left
- Synthetic Spotify playback state; variant-specific state must satisfy: action play
- Synthetic named Music playlist; variant-specific state must satisfy: action next

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0565 · Natural compound request

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

I have a few things to finish. Open Calculator. Stop the internet radio you started earlier. Play white noise for ten minutes. Switch one desktop to the left. List the apps currently open and identify the frontmost one. Keep the results separate so I can tell what came from where.

**Required tools:** `open_app`, `stop_radio`, `play_ambient`, `manage_spaces`, `list_running_apps`.
**Checks:** name Calculator; launch only stop radio; acknowledge broad afplay implementation risk kind white_noise; minutes 10 direction left read process list; no launch/quit Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic app launch
- Synthetic prior radio session; no real audio processes
- Synthetic playback process only; variant-specific state must satisfy: kind white_noise; minutes 10
- Synthetic Mission Control shortcuts enabled; variant-specific state must satisfy: direction left
- Synthetic Calculator, Safari and Music processes

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0566 · Natural compound request

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

I have a few things to finish. Open Books so I can choose my audiobook. Find and play BBC Radio 1. List the apps currently open and identify the frontmost one. Resume Spotify playback. Open podcast search for Route Science so I can choose an episode. Keep the results separate so I can tell what came from where.

**Required tools:** `play_audiobook`, `play_radio`, `list_running_apps`, `spotify`, `play_podcast`.
**Checks:** app handoff only; no claim of resuming playback query BBC Radio 1; distinguish launch from verified playback read process list; no launch/quit action play query Route Science; manual play handoff Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Books launch
- Synthetic station result and intercepted player
- Synthetic Calculator, Safari and Music processes
- Synthetic Spotify playback state; variant-specific state must satisfy: action play
- Synthetic Podcasts search URI

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0567 · Natural compound request

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

I have a few things to finish. Open Books so I can choose my audiobook. Find and play BBC Radio 1. Resume Apple Music. Open the Netflix app so I can pick a show. Bring Safari forward without hiding the other apps. Keep the results separate so I can tell what came from where.

**Required tools:** `play_audiobook`, `play_radio`, `music`, `play_streaming`, `switch_app`.
**Checks:** app handoff only; no claim of resuming playback query BBC Radio 1; distinguish launch from verified playback action play app Netflix; launch only name Safari; hide_others false Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Books launch
- Synthetic station result and intercepted player
- Synthetic named Music playlist; variant-specific state must satisfy: action play
- Synthetic installed Netflix app
- Safari already running in synthetic app state; variant-specific state must satisfy: name Safari; hide_others false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0568 · Natural compound request

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

I have a few things to finish. Open the Netflix app so I can pick a show. Stop the internet radio you started earlier. Find and play BBC Radio 1. Enter full screen for Safari's front window. Resume Spotify playback. Keep the results separate so I can tell what came from where.

**Required tools:** `play_streaming`, `stop_radio`, `play_radio`, `window_control`, `spotify`.
**Checks:** app Netflix; launch only stop radio; acknowledge broad afplay implementation risk query BBC Radio 1; distinguish launch from verified playback action fullscreen; app Safari action play Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic installed Netflix app
- Synthetic prior radio session; no real audio processes
- Synthetic station result and intercepted player
- Synthetic Accessibility window bounds; variant-specific state must satisfy: action fullscreen; app Safari
- Synthetic Spotify playback state; variant-specific state must satisfy: action play

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0569 · Natural compound request

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

I have a few things to finish. Pause Spotify. Find and play BBC Radio 1. Quit the Route Scratch app normally. Bring Safari to the front and hide the other apps. Play brown noise for 20 minutes. Keep the results separate so I can tell what came from where.

**Required tools:** `spotify`, `play_radio`, `quit_app`, `switch_app`, `play_ambient`.
**Checks:** action pause; correct player query BBC Radio 1; distinguish launch from verified playback name Route Scratch; graceful quit only name Safari; hide_others true kind brown_noise; minutes 20 Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Spotify playback state
- Synthetic station result and intercepted player
- Disposable synthetic app with no unsaved work
- Safari already running in synthetic app state
- Synthetic playback process only

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0570 · Natural compound request

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

I have a few things to finish. Bring Safari forward without hiding the other apps. Open Books so I can choose my audiobook. Pause Apple Music. Switch one desktop to the left. Skip to the next Spotify track. Keep the results separate so I can tell what came from where.

**Required tools:** `switch_app`, `play_audiobook`, `music`, `manage_spaces`, `spotify`.
**Checks:** name Safari; hide_others false app handoff only; no claim of resuming playback action pause direction left action next Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Safari already running in synthetic app state; variant-specific state must satisfy: name Safari; hide_others false
- Synthetic Books launch
- Synthetic named Music playlist; variant-specific state must satisfy: action pause
- Synthetic Mission Control shortcuts enabled; variant-specific state must satisfy: direction left
- Synthetic Spotify playback state; variant-specific state must satisfy: action next

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0571 · Scoped execution

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

For these tasks, use only the named sources and targets: list the apps currently open and identify the frontmost one; then stop the internet radio you started earlier; then open Calculator; then open the Netflix app so I can pick a show; then open podcast search for Route Science so I can choose an episode. Leave everything else unchanged.

**Required tools:** `list_running_apps`, `stop_radio`, `open_app`, `play_streaming`, `play_podcast`.
**Ordering constraints:** `list_running_apps` before `stop_radio`; `stop_radio` before `open_app`; `open_app` before `play_streaming`; `play_streaming` before `play_podcast`.
**Checks:** read process list; no launch/quit stop radio; acknowledge broad afplay implementation risk name Calculator; launch only app Netflix; launch only query Route Science; manual play handoff Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Calculator, Safari and Music processes
- Synthetic prior radio session; no real audio processes
- Synthetic app launch
- Synthetic installed Netflix app
- Synthetic Podcasts search URI

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0572 · Scoped execution

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

For these tasks, use only the named sources and targets: switch one desktop to the left; then stop the internet radio you started earlier; then play white noise for ten minutes; then open Books so I can choose my audiobook; then open Calculator. Leave everything else unchanged.

**Required tools:** `manage_spaces`, `stop_radio`, `play_ambient`, `play_audiobook`, `open_app`.
**Ordering constraints:** `manage_spaces` before `stop_radio`; `stop_radio` before `play_ambient`; `play_ambient` before `play_audiobook`; `play_audiobook` before `open_app`.
**Checks:** direction left stop radio; acknowledge broad afplay implementation risk kind white_noise; minutes 10 app handoff only; no claim of resuming playback name Calculator; launch only Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Mission Control shortcuts enabled; variant-specific state must satisfy: direction left
- Synthetic prior radio session; no real audio processes
- Synthetic playback process only; variant-specific state must satisfy: kind white_noise; minutes 10
- Synthetic Books launch
- Synthetic app launch

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0573 · Scoped execution

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

For these tasks, use only the named sources and targets: pause Apple Music; then switch one desktop to the left; then quit the Route Scratch app normally; then tile Safari's front window to the right; then open podcast search for Route Science so I can choose an episode. Leave everything else unchanged.

**Required tools:** `music`, `manage_spaces`, `quit_app`, `window_control`, `play_podcast`.
**Ordering constraints:** `music` before `manage_spaces`; `manage_spaces` before `quit_app`; `quit_app` before `window_control`; `window_control` before `play_podcast`.
**Checks:** action pause direction left name Route Scratch; graceful quit only action right; app Safari query Route Science; manual play handoff Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic named Music playlist; variant-specific state must satisfy: action pause
- Synthetic Mission Control shortcuts enabled; variant-specific state must satisfy: direction left
- Disposable synthetic app with no unsaved work
- Synthetic Accessibility window bounds; variant-specific state must satisfy: action right; app Safari
- Synthetic Podcasts search URI

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0574 · Scoped execution

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

For these tasks, use only the named sources and targets: open Calculator; then open the Netflix app so I can pick a show; then find and play BBC Radio 1; then list the apps currently open and identify the frontmost one; then switch to desktop number two. Leave everything else unchanged.

**Required tools:** `open_app`, `play_streaming`, `play_radio`, `list_running_apps`, `manage_spaces`.
**Ordering constraints:** `open_app` before `play_streaming`; `play_streaming` before `play_radio`; `play_radio` before `list_running_apps`; `list_running_apps` before `manage_spaces`.
**Checks:** name Calculator; launch only app Netflix; launch only query BBC Radio 1; distinguish launch from verified playback read process list; no launch/quit direction number; number 2 Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic app launch
- Synthetic installed Netflix app
- Synthetic station result and intercepted player
- Synthetic Calculator, Safari and Music processes
- Synthetic Mission Control shortcuts enabled

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0575 · Scoped execution

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

For these tasks, use only the named sources and targets: stop the ambient sound you started earlier; then open Calculator; then bring Safari forward without hiding the other apps; then find and play BBC Radio 1; then open the Netflix app so I can pick a show. Leave everything else unchanged.

**Required tools:** `play_ambient`, `open_app`, `switch_app`, `play_radio`, `play_streaming`.
**Ordering constraints:** `play_ambient` before `open_app`; `open_app` before `switch_app`; `switch_app` before `play_radio`; `play_radio` before `play_streaming`.
**Checks:** kind stop; no invented stop_ambient tool name Calculator; launch only name Safari; hide_others false query BBC Radio 1; distinguish launch from verified playback app Netflix; launch only Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic playback process only; variant-specific state must satisfy: kind stop; no invented stop_ambient tool
- Synthetic app launch
- Safari already running in synthetic app state; variant-specific state must satisfy: name Safari; hide_others false
- Synthetic station result and intercepted player
- Synthetic installed Netflix app

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0576 · Scoped execution

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

For these tasks, use only the named sources and targets: play white noise for ten minutes; then bring Safari forward without hiding the other apps; then stop the internet radio you started earlier; then quit the Route Scratch app normally; then open Books so I can choose my audiobook. Leave everything else unchanged.

**Required tools:** `play_ambient`, `switch_app`, `stop_radio`, `quit_app`, `play_audiobook`.
**Ordering constraints:** `play_ambient` before `switch_app`; `switch_app` before `stop_radio`; `stop_radio` before `quit_app`; `quit_app` before `play_audiobook`.
**Checks:** kind white_noise; minutes 10 name Safari; hide_others false stop radio; acknowledge broad afplay implementation risk name Route Scratch; graceful quit only app handoff only; no claim of resuming playback Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic playback process only; variant-specific state must satisfy: kind white_noise; minutes 10
- Safari already running in synthetic app state; variant-specific state must satisfy: name Safari; hide_others false
- Synthetic prior radio session; no real audio processes
- Disposable synthetic app with no unsaved work
- Synthetic Books launch

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0577 · Scoped execution

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

For these tasks, use only the named sources and targets: open Books so I can choose my audiobook; then return to the previous Spotify track; then bring Safari forward without hiding the other apps; then stop the internet radio you started earlier; then maximize Safari's front window. Leave everything else unchanged.

**Required tools:** `play_audiobook`, `spotify`, `switch_app`, `stop_radio`, `window_control`.
**Ordering constraints:** `play_audiobook` before `spotify`; `spotify` before `switch_app`; `switch_app` before `stop_radio`; `stop_radio` before `window_control`.
**Checks:** app handoff only; no claim of resuming playback action previous name Safari; hide_others false stop radio; acknowledge broad afplay implementation risk action zoom; app Safari Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Books launch
- Synthetic Spotify playback state; variant-specific state must satisfy: action previous
- Safari already running in synthetic app state; variant-specific state must satisfy: name Safari; hide_others false
- Synthetic prior radio session; no real audio processes
- Synthetic Accessibility window bounds; variant-specific state must satisfy: action zoom; app Safari

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0578 · Scoped execution

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

For these tasks, use only the named sources and targets: open podcast search for Route Science so I can choose an episode; then skip to the next Apple Music track; then minimize Safari's front window; then open the Netflix app so I can pick a show; then bring Safari forward without hiding the other apps. Leave everything else unchanged.

**Required tools:** `play_podcast`, `music`, `window_control`, `play_streaming`, `switch_app`.
**Ordering constraints:** `play_podcast` before `music`; `music` before `window_control`; `window_control` before `play_streaming`; `play_streaming` before `switch_app`.
**Checks:** query Route Science; manual play handoff action next action minimize; app Safari app Netflix; launch only name Safari; hide_others false Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Podcasts search URI
- Synthetic named Music playlist; variant-specific state must satisfy: action next
- Synthetic Accessibility window bounds; variant-specific state must satisfy: action minimize; app Safari
- Synthetic installed Netflix app
- Safari already running in synthetic app state; variant-specific state must satisfy: name Safari; hide_others false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0579 · Scoped execution

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

For these tasks, use only the named sources and targets: find and play BBC Radio 1; then open Calculator; then stop the internet radio you started earlier; then open the Netflix app so I can pick a show; then resume Apple Music. Leave everything else unchanged.

**Required tools:** `play_radio`, `open_app`, `stop_radio`, `play_streaming`, `music`.
**Ordering constraints:** `play_radio` before `open_app`; `open_app` before `stop_radio`; `stop_radio` before `play_streaming`; `play_streaming` before `music`.
**Checks:** query BBC Radio 1; distinguish launch from verified playback name Calculator; launch only stop radio; acknowledge broad afplay implementation risk app Netflix; launch only action play Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic station result and intercepted player
- Synthetic app launch
- Synthetic prior radio session; no real audio processes
- Synthetic installed Netflix app
- Synthetic named Music playlist; variant-specific state must satisfy: action play

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0580 · Scoped execution

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

For these tasks, use only the named sources and targets: quit the Route Scratch app normally; then stop the ambient sound you started earlier; then return to the previous Spotify track; then open podcast search for Route Science so I can choose an episode; then switch one desktop to the right. Leave everything else unchanged.

**Required tools:** `quit_app`, `play_ambient`, `spotify`, `play_podcast`, `manage_spaces`.
**Ordering constraints:** `quit_app` before `play_ambient`; `play_ambient` before `spotify`; `spotify` before `play_podcast`; `play_podcast` before `manage_spaces`.
**Checks:** name Route Scratch; graceful quit only kind stop; no invented stop_ambient tool action previous query Route Science; manual play handoff direction right Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Disposable synthetic app with no unsaved work
- Synthetic playback process only; variant-specific state must satisfy: kind stop; no invented stop_ambient tool
- Synthetic Spotify playback state; variant-specific state must satisfy: action previous
- Synthetic Podcasts search URI
- Synthetic Mission Control shortcuts enabled; variant-specific state must satisfy: direction right

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0581 · Late constraints

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Switch to desktop number two. Bring Safari to the front and hide the other apps. Stop the internet radio you started earlier. Open podcast search for Route Science so I can choose an episode. Quit the Route Scratch app normally. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `manage_spaces`, `switch_app`, `stop_radio`, `play_podcast`, `quit_app`.
**Checks:** direction number; number 2 name Safari; hide_others true stop radio; acknowledge broad afplay implementation risk query Route Science; manual play handoff name Route Scratch; graceful quit only Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Mission Control shortcuts enabled
- Safari already running in synthetic app state
- Synthetic prior radio session; no real audio processes
- Synthetic Podcasts search URI
- Disposable synthetic app with no unsaved work

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0582 · Late constraints

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Return to the previous Apple Music track. Open the Netflix app so I can pick a show. Stop the internet radio you started earlier. Open Books so I can choose my audiobook. Switch one desktop to the right. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `music`, `play_streaming`, `stop_radio`, `play_audiobook`, `manage_spaces`.
**Checks:** action previous app Netflix; launch only stop radio; acknowledge broad afplay implementation risk app handoff only; no claim of resuming playback direction right Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic named Music playlist; variant-specific state must satisfy: action previous
- Synthetic installed Netflix app
- Synthetic prior radio session; no real audio processes
- Synthetic Books launch
- Synthetic Mission Control shortcuts enabled; variant-specific state must satisfy: direction right

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0583 · Late constraints

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Play white noise for ten minutes. Quit the Route Scratch app normally. Pause Apple Music. Resume Spotify playback. Find and play BBC Radio 1. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `play_ambient`, `quit_app`, `music`, `spotify`, `play_radio`.
**Checks:** kind white_noise; minutes 10 name Route Scratch; graceful quit only action pause action play query BBC Radio 1; distinguish launch from verified playback Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic playback process only; variant-specific state must satisfy: kind white_noise; minutes 10
- Disposable synthetic app with no unsaved work
- Synthetic named Music playlist; variant-specific state must satisfy: action pause
- Synthetic Spotify playback state; variant-specific state must satisfy: action play
- Synthetic station result and intercepted player

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0584 · Late constraints

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Open Books so I can choose my audiobook. Bring Safari forward without hiding the other apps. Resume Spotify playback. Open podcast search for Route Science so I can choose an episode. Close only Safari's front window. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `play_audiobook`, `switch_app`, `spotify`, `play_podcast`, `window_control`.
**Checks:** app handoff only; no claim of resuming playback name Safari; hide_others false action play query Route Science; manual play handoff action close; app Safari; do not quit app Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Books launch
- Safari already running in synthetic app state; variant-specific state must satisfy: name Safari; hide_others false
- Synthetic Spotify playback state; variant-specific state must satisfy: action play
- Synthetic Podcasts search URI
- Synthetic Accessibility window bounds; variant-specific state must satisfy: action close; app Safari; do not quit app

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0585 · Late constraints

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Open podcast search for Route Science so I can choose an episode. Switch one desktop to the right. Quit the Route Scratch app normally. Open the Netflix app so I can pick a show. List the apps currently open and identify the frontmost one. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `play_podcast`, `manage_spaces`, `quit_app`, `play_streaming`, `list_running_apps`.
**Checks:** query Route Science; manual play handoff direction right name Route Scratch; graceful quit only app Netflix; launch only read process list; no launch/quit Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Podcasts search URI
- Synthetic Mission Control shortcuts enabled; variant-specific state must satisfy: direction right
- Disposable synthetic app with no unsaved work
- Synthetic installed Netflix app
- Synthetic Calculator, Safari and Music processes

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0586 · Late constraints

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Open podcast search for Route Science so I can choose an episode. Play white noise for ten minutes. Open the Netflix app so I can pick a show. Stop the internet radio you started earlier. Quit the Route Scratch app normally. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `play_podcast`, `play_ambient`, `play_streaming`, `stop_radio`, `quit_app`.
**Checks:** query Route Science; manual play handoff kind white_noise; minutes 10 app Netflix; launch only stop radio; acknowledge broad afplay implementation risk name Route Scratch; graceful quit only Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Podcasts search URI
- Synthetic playback process only; variant-specific state must satisfy: kind white_noise; minutes 10
- Synthetic installed Netflix app
- Synthetic prior radio session; no real audio processes
- Disposable synthetic app with no unsaved work

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0587 · Late constraints

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Find and play BBC Radio 1. Open Calculator. Stop the internet radio you started earlier. Maximize Safari's front window. Open Books so I can choose my audiobook. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `play_radio`, `open_app`, `stop_radio`, `window_control`, `play_audiobook`.
**Checks:** query BBC Radio 1; distinguish launch from verified playback name Calculator; launch only stop radio; acknowledge broad afplay implementation risk action zoom; app Safari app handoff only; no claim of resuming playback Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic station result and intercepted player
- Synthetic app launch
- Synthetic prior radio session; no real audio processes
- Synthetic Accessibility window bounds; variant-specific state must satisfy: action zoom; app Safari
- Synthetic Books launch

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0588 · Late constraints

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Find and play BBC Radio 1. Skip to the next Spotify track. Switch one desktop to the left. Quit the Route Scratch app normally. List the apps currently open and identify the frontmost one. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `play_radio`, `spotify`, `manage_spaces`, `quit_app`, `list_running_apps`.
**Checks:** query BBC Radio 1; distinguish launch from verified playback action next direction left name Route Scratch; graceful quit only read process list; no launch/quit Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic station result and intercepted player
- Synthetic Spotify playback state; variant-specific state must satisfy: action next
- Synthetic Mission Control shortcuts enabled; variant-specific state must satisfy: direction left
- Disposable synthetic app with no unsaved work
- Synthetic Calculator, Safari and Music processes

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0589 · Late constraints

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Return to the previous Spotify track. Open Books so I can choose my audiobook. Play white noise for ten minutes. List the apps currently open and identify the frontmost one. Quit the Route Scratch app normally. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `spotify`, `play_audiobook`, `play_ambient`, `list_running_apps`, `quit_app`.
**Checks:** action previous app handoff only; no claim of resuming playback kind white_noise; minutes 10 read process list; no launch/quit name Route Scratch; graceful quit only Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Spotify playback state; variant-specific state must satisfy: action previous
- Synthetic Books launch
- Synthetic playback process only; variant-specific state must satisfy: kind white_noise; minutes 10
- Synthetic Calculator, Safari and Music processes
- Disposable synthetic app with no unsaved work

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0590 · Late constraints

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Resume Spotify playback. Open the Netflix app so I can pick a show. Open Calculator. Find and play BBC Radio 1. Skip to the next Apple Music track. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `spotify`, `play_streaming`, `open_app`, `play_radio`, `music`.
**Checks:** action play app Netflix; launch only name Calculator; launch only query BBC Radio 1; distinguish launch from verified playback action next Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Spotify playback state; variant-specific state must satisfy: action play
- Synthetic installed Netflix app
- Synthetic app launch
- Synthetic station result and intercepted player
- Synthetic named Music playlist; variant-specific state must satisfy: action next

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0591 · Colloquial with interruptions

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Could you list the apps currently open and identify the frontmost one; then find and play BBC Radio 1; then open podcast search for Route Science so I can choose an episode; then resume Apple Music; then maximize Safari's front window? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `list_running_apps`, `play_radio`, `play_podcast`, `music`, `window_control`.
**Ordering constraints:** `list_running_apps` before `play_radio`; `play_radio` before `play_podcast`; `play_podcast` before `music`; `music` before `window_control`.
**Checks:** read process list; no launch/quit query BBC Radio 1; distinguish launch from verified playback query Route Science; manual play handoff action play action zoom; app Safari Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Calculator, Safari and Music processes
- Synthetic station result and intercepted player
- Synthetic Podcasts search URI
- Synthetic named Music playlist; variant-specific state must satisfy: action play
- Synthetic Accessibility window bounds; variant-specific state must satisfy: action zoom; app Safari

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0592 · Colloquial with interruptions

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Could you open Calculator; then play brown noise for 20 minutes; then tile Safari's front window to the left half of the screen; then list the apps currently open and identify the frontmost one; then stop the internet radio you started earlier? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `open_app`, `play_ambient`, `window_control`, `list_running_apps`, `stop_radio`.
**Ordering constraints:** `open_app` before `play_ambient`; `play_ambient` before `window_control`; `window_control` before `list_running_apps`; `list_running_apps` before `stop_radio`.
**Checks:** name Calculator; launch only kind brown_noise; minutes 20 action left; app Safari; only target window read process list; no launch/quit stop radio; acknowledge broad afplay implementation risk Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic app launch
- Synthetic playback process only
- Synthetic Accessibility window bounds
- Synthetic Calculator, Safari and Music processes
- Synthetic prior radio session; no real audio processes

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0593 · Colloquial with interruptions

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Could you play white noise for ten minutes; then open the Netflix app so I can pick a show; then find and play BBC Radio 1; then open podcast search for Route Science so I can choose an episode; then open Books so I can choose my audiobook? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `play_ambient`, `play_streaming`, `play_radio`, `play_podcast`, `play_audiobook`.
**Ordering constraints:** `play_ambient` before `play_streaming`; `play_streaming` before `play_radio`; `play_radio` before `play_podcast`; `play_podcast` before `play_audiobook`.
**Checks:** kind white_noise; minutes 10 app Netflix; launch only query BBC Radio 1; distinguish launch from verified playback query Route Science; manual play handoff app handoff only; no claim of resuming playback Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic playback process only; variant-specific state must satisfy: kind white_noise; minutes 10
- Synthetic installed Netflix app
- Synthetic station result and intercepted player
- Synthetic Podcasts search URI
- Synthetic Books launch

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0594 · Colloquial with interruptions

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Could you quit the Route Scratch app normally; then list the apps currently open and identify the frontmost one; then switch one desktop to the left; then stop the internet radio you started earlier; then bring Safari forward without hiding the other apps? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `quit_app`, `list_running_apps`, `manage_spaces`, `stop_radio`, `switch_app`.
**Ordering constraints:** `quit_app` before `list_running_apps`; `list_running_apps` before `manage_spaces`; `manage_spaces` before `stop_radio`; `stop_radio` before `switch_app`.
**Checks:** name Route Scratch; graceful quit only read process list; no launch/quit direction left stop radio; acknowledge broad afplay implementation risk name Safari; hide_others false Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Disposable synthetic app with no unsaved work
- Synthetic Calculator, Safari and Music processes
- Synthetic Mission Control shortcuts enabled; variant-specific state must satisfy: direction left
- Synthetic prior radio session; no real audio processes
- Safari already running in synthetic app state; variant-specific state must satisfy: name Safari; hide_others false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0595 · Colloquial with interruptions

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Could you quit the Route Scratch app normally; then resume Apple Music; then list the apps currently open and identify the frontmost one; then find and play BBC Radio 1; then stop the internet radio you started earlier? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `quit_app`, `music`, `list_running_apps`, `play_radio`, `stop_radio`.
**Ordering constraints:** `quit_app` before `music`; `music` before `list_running_apps`; `list_running_apps` before `play_radio`; `play_radio` before `stop_radio`.
**Checks:** name Route Scratch; graceful quit only action play read process list; no launch/quit query BBC Radio 1; distinguish launch from verified playback stop radio; acknowledge broad afplay implementation risk Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Disposable synthetic app with no unsaved work
- Synthetic named Music playlist; variant-specific state must satisfy: action play
- Synthetic Calculator, Safari and Music processes
- Synthetic station result and intercepted player
- Synthetic prior radio session; no real audio processes

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0596 · Colloquial with interruptions

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Could you quit the Route Scratch app normally; then stop the ambient sound you started earlier; then bring Safari forward without hiding the other apps; then exit full screen for Safari's front window; then open podcast search for Route Science so I can choose an episode? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `quit_app`, `play_ambient`, `switch_app`, `window_control`, `play_podcast`.
**Ordering constraints:** `quit_app` before `play_ambient`; `play_ambient` before `switch_app`; `switch_app` before `window_control`; `window_control` before `play_podcast`.
**Checks:** name Route Scratch; graceful quit only kind stop; no invented stop_ambient tool name Safari; hide_others false action exit_fullscreen; app Safari query Route Science; manual play handoff Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Disposable synthetic app with no unsaved work
- Synthetic playback process only; variant-specific state must satisfy: kind stop; no invented stop_ambient tool
- Safari already running in synthetic app state; variant-specific state must satisfy: name Safari; hide_others false
- Synthetic Accessibility window bounds; variant-specific state must satisfy: action exit_fullscreen; app Safari
- Synthetic Podcasts search URI

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0597 · Colloquial with interruptions

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Could you resume Spotify playback; then open the Netflix app so I can pick a show; then bring Safari forward without hiding the other apps; then open podcast search for Route Science so I can choose an episode; then switch one desktop to the left? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `spotify`, `play_streaming`, `switch_app`, `play_podcast`, `manage_spaces`.
**Ordering constraints:** `spotify` before `play_streaming`; `play_streaming` before `switch_app`; `switch_app` before `play_podcast`; `play_podcast` before `manage_spaces`.
**Checks:** action play app Netflix; launch only name Safari; hide_others false query Route Science; manual play handoff direction left Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Spotify playback state; variant-specific state must satisfy: action play
- Synthetic installed Netflix app
- Safari already running in synthetic app state; variant-specific state must satisfy: name Safari; hide_others false
- Synthetic Podcasts search URI
- Synthetic Mission Control shortcuts enabled; variant-specific state must satisfy: direction left

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0598 · Colloquial with interruptions

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Could you bring Safari forward without hiding the other apps; then find and play BBC Radio 1; then list the apps currently open and identify the frontmost one; then stop the ambient sound you started earlier; then open Books so I can choose my audiobook? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `switch_app`, `play_radio`, `list_running_apps`, `play_ambient`, `play_audiobook`.
**Ordering constraints:** `switch_app` before `play_radio`; `play_radio` before `list_running_apps`; `list_running_apps` before `play_ambient`; `play_ambient` before `play_audiobook`.
**Checks:** name Safari; hide_others false query BBC Radio 1; distinguish launch from verified playback read process list; no launch/quit kind stop; no invented stop_ambient tool app handoff only; no claim of resuming playback Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Safari already running in synthetic app state; variant-specific state must satisfy: name Safari; hide_others false
- Synthetic station result and intercepted player
- Synthetic Calculator, Safari and Music processes
- Synthetic playback process only; variant-specific state must satisfy: kind stop; no invented stop_ambient tool
- Synthetic Books launch

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0599 · Colloquial with interruptions

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Could you bring Safari forward without hiding the other apps; then skip to the next Spotify track; then open Books so I can choose my audiobook; then list the apps currently open and identify the frontmost one; then quit the Route Scratch app normally? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `switch_app`, `spotify`, `play_audiobook`, `list_running_apps`, `quit_app`.
**Ordering constraints:** `switch_app` before `spotify`; `spotify` before `play_audiobook`; `play_audiobook` before `list_running_apps`; `list_running_apps` before `quit_app`.
**Checks:** name Safari; hide_others false action next app handoff only; no claim of resuming playback read process list; no launch/quit name Route Scratch; graceful quit only Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Safari already running in synthetic app state; variant-specific state must satisfy: name Safari; hide_others false
- Synthetic Spotify playback state; variant-specific state must satisfy: action next
- Synthetic Books launch
- Synthetic Calculator, Safari and Music processes
- Disposable synthetic app with no unsaved work

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0600 · Colloquial with interruptions

**Focus:** Disambiguate app opening, transport controls, window actions, and manual playback handoffs.

**Prompt:**

Could you enter full screen for Safari's front window; then find and play BBC Radio 1; then open Books so I can choose my audiobook; then open podcast search for Route Science so I can choose an episode; then play white noise for ten minutes? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `window_control`, `play_radio`, `play_audiobook`, `play_podcast`, `play_ambient`.
**Ordering constraints:** `window_control` before `play_radio`; `play_radio` before `play_audiobook`; `play_audiobook` before `play_podcast`; `play_podcast` before `play_ambient`.
**Checks:** action fullscreen; app Safari query BBC Radio 1; distinguish launch from verified playback app handoff only; no claim of resuming playback query Route Science; manual play handoff kind white_noise; minutes 10 Disambiguate app opening, transport controls, window actions, and manual playback handoffs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic Accessibility window bounds; variant-specific state must satisfy: action fullscreen; app Safari
- Synthetic station result and intercepted player
- Synthetic Books launch
- Synthetic Podcasts search URI
- Synthetic playback process only; variant-specific state must satisfy: kind white_noise; minutes 10

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.
