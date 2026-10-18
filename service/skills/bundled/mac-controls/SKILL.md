---
name: mac-controls
description: Mac volume, mute, screen lock and music playback (Apple Music or Spotify) through fixed, validated commands. Use for "turn it down", "mute", "lock my screen", "pause the music", "next song", "skip".
metadata:
  wisp:
    triggers:
      - my volume
      - volume up
      - volume down
      - volume to
      - louder
      - quieter
      - turn it up
      - turn it down
      - unmute
      - mute my
      - mute the
      - mute sound
      - lock my screen
      - lock the screen
      - lock my mac
      - lock my computer
      - pause the music
      - pause music
      - play music
      - resume music
      - next song
      - skip
      - skip song
      - skip this song
      - previous song
      - next track
      - previous track
      - on spotify
      - in spotify
      - pause spotify
      - play spotify
    enabled: true
    tools:
      - name: mac_get_volume
        description: Read the Mac's output volume and whether it is muted.
        command: bash controls.sh get-volume
      - name: mac_set_volume
        description: Set the Mac's output volume to a level from 0 to 100.
        command: bash controls.sh set-volume {level}
        parameters:
          level:
            type: integer
            minimum: 0
            maximum: 100
            description: Output volume percent, 0-100.
            required: true
      - name: mac_mute
        description: Mute (state on) or unmute (state off) the Mac's sound output.
        command: bash controls.sh mute {state}
        parameters:
          state:
            type: string
            enum: ["on", "off"]
            required: true
      - name: mac_lock_screen
        description: Lock the Mac's screen now and report whether macOS confirmed the lock.
        command: bash controls.sh lock
      - name: mac_media
        description: Play, pause, skip or go back in Apple Music (default) or Spotify.
        command: bash controls.sh media {action} {app}
        parameters:
          action:
            type: string
            enum: ["play", "pause", "next", "previous"]
            required: true
          app:
            type: string
            enum: ["music", "spotify"]
            description: Which player. Use spotify only when the user names Spotify.
---

# Mac controls

- For volume, mute, screen lock or music playback, call the `mac_*` tools. Do not use `run_shell` or `run_applescript` for these.
- "Turn it down/up" with no number: call `mac_get_volume` first, then `mac_set_volume` about 20 lower or higher (clamped to 0-100).
- "Mute" is `mac_mute` with state `on`; "unmute" is state `off`. Setting volume 0 is not the same as muting.
- `mac_media` cannot pick a song, album or playlist. If the user asks for specific music, say so and offer to play or open the app instead.
- `mac_lock_screen` reports "screen locked" only when macOS confirms it. If it returns an error, tell the user the screen is NOT locked and why.
- Depending on the user's permission settings these may ask for confirmation first. If the user declines, say so plainly and do not retry another way.
