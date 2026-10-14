"""V3.2 category `v32_mac_controls`: the mac-controls skill tools (volume, mute, lock, playback).

New, authored conversations (V3.1 had none to migrate). Deterministic (seed via generate.SEED), stdlib only, built with
generate.Builder so they follow exactly the host protocol the migrated rows follow: name-only stubs or get_tool_schemas
(the mac_* tools live in the `skills` family: model_led.family_for maps category skill_tool there), SP preloads (hit and
decoy), receipts, two loading steps. Results are the real strings the skill produces: controls.sh output through the
skill runner (non-zero exit -> "(exit code N)\\n<stderr>"), the host's argument-validation error, and the host's
confirmation-denial text "The user denied this action." (service/agent/loop.py; every skill tool is confirm-tier).

Gold policy (mirrors the skill body at 55a2b5b1):
  * explicit level -> mac_set_volume(level=<int 0-100>) ("50%" -> 50, never 0.5);
  * "turn it up/down" with no number -> mac_get_volume, then mac_set_volume about 20 higher/lower, clamped to 0-100;
  * mute/unmute -> mac_mute(state=on/off); volume 0 is not mute; "what's my volume"/"am I muted" -> mac_get_volume only;
  * lock -> mac_lock_screen; on an error result the answer says plainly the screen is NOT locked, and why; no retry;
  * play/pause/next/previous -> mac_media(action), app omitted (Apple Music) unless the user names Spotify;
  * a specific song/album/playlist -> no call; say mac_media cannot pick music and offer to play or open the app;
    accepting that offer in a follow-up -> open_app;
  * a declined confirmation -> say nothing changed; no retry, never run_shell / run_applescript;
  * a refused argument -> correct once (the bad call is context only, never a training target).
"""
import datetime as dt

CATEGORY = "v32_mac_controls"
N_TRAIN, N_DEV = 60, 8
SKILL = ["mac_get_volume", "mac_set_volume", "mac_mute", "mac_lock_screen", "mac_media"]
DENIED = "The user denied this action."
LOCK_UNCONFIRMED = "(exit code 2)\nsent the Lock Screen shortcut, but macOS did not report the screen as locked"
LOCK_ERRORS = [
    "(exit code 2)\ncould not lock the screen: 36:84: execution error: System Events got an error: osascript is not allowed to send keystrokes. (1002)",
    "(exit code 2)\ncould not lock the screen: execution error: Not authorized to send Apple events to System Events. (-1743)",
]
RANGE_ERROR = "(exit code 2)\nlevel must be a whole number from 0 to 100"
DECOY_POOL = ["clipboard_read", "clipboard_write", "list_running_apps", "open_app", "quit_app", "screen_capture",
              "system_status", "text_to_speech", "toggle_setting", "window_control", "wisp_mcp", "use_skill", "wisp_skills"]

# ------------------------------------------------------------------ phrasing (train registers vs dev registers)
SET_T = ["set the volume to {n}", "put my volume at {n} percent", "volume to {n}% please", "can you make the sound {n} percent",
         "change my mac volume to {n}", "set sound level to {n}%", "i want the volume at {n}"]
SET_D = ["could the output level sit at {n}% for now", "dial the speakers to {n} out of 100"]
REL_T = {"down": ["turn it down a bit", "it's too loud, turn it down", "volume down a little please", "make it quieter",
                  "can you bring the sound down some", "a bit quieter please, the music is blasting"],
         "up": ["turn it up a bit", "louder please", "volume up a notch", "i can barely hear this, turn it up",
                "make it louder", "bump the sound up some"]}
REL_D = {"down": ["ease the speakers back a touch"], "up": ["give the speakers a little more juice"]}
READ_T = [("vol", "what's my volume at"), ("vol", "how loud is my mac set right now"), ("vol", "what is my volume level"),
          ("mute", "am i muted right now"), ("mute", "is my mac's sound muted")]
MUTE_T = {"on": ["mute my mac", "mute the sound", "mute sound please", "silence my computer for a bit", "mute my speakers"],
          "off": ["unmute my mac", "unmute the sound", "unmute please, i need to hear this", "turn the sound back on, unmute it"]}
LOCK_T = ["lock the screen please", "lock up my screen now", "please lock my laptop screen", "lock my mac before anyone sees",
          "time to lock the computer, do it", "quick, lock my screen for me", "lock my mac, i'm stepping away", "lock my computer real quick",
          "can you lock my mac", "lock the screen before I go to lunch"]
LOCK_D = ["secure the display while I grab coffee", "I'm walking off, get the screen locked",
          "nobody should see my desktop while I'm out, lock it", "screen lock now, heading into a meeting"]
MEDIA_T = {"pause": ["pause the music for a minute", "pause music for a sec", "stop the song for a moment, pause it"],
           "play": ["play music", "resume music", "start the music back up"],
           "next": ["next song", "skip this song", "skip song please", "next track"],
           "previous": ["previous song", "go back a track", "previous track please", "play the last song again, go back one"]}
SPOT_T = {"pause": ["pause spotify", "pause the song on spotify"], "play": ["play spotify", "resume my music in spotify"],
          "next": ["skip to the next track on spotify", "next song in spotify"],
          "previous": ["previous track on spotify", "go back a song in spotify"]}
MEDIA_D = {"pause": ["hold the tunes in Music"], "next": ["move Music along to the following track"]}
SPECIFIC_T = [("Music", "play my workout playlist"), ("Music", "put on some Taylor Swift"), ("Music", "play the album Rumours"),
              ("Spotify", "play my discover weekly on spotify"), ("Music", "play bohemian rhapsody"),
              ("Spotify", "put my chill playlist on in spotify")]
DECLINE_T = [("set", "set the volume to {n}"), ("mute", "mute my mac"), ("lock", "lock the screen please"),
             ("media", "pause the music for a minute"), ("set", "volume to {n}% please"), ("media", "skip this song")]
DECLINE_D = [("lock", "get the display locked up, I'm leaving the desk"), ("set", "dial the speakers to {n} out of 100")]


def _now(r):
    d = dt.date(2026, 10, 14) + dt.timedelta(days=r.randint(0, 60))
    h, m = r.choice([7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]), r.choice([2, 9, 14, 21, 33, 40, 47, 56])
    t = dt.datetime(d.year, d.month, d.day, h, m)
    return (f"The current date and time is {t.strftime('%A, %B')} {t.day}, {t.year} at {(t.hour - 1) % 12 + 1}:{t.minute:02d} "
            f"{'AM' if t.hour < 12 else 'PM'}. Resolve relative dates/times against this.")


def _noise(r, text):
    if r.random() < 0.25:
        text = text.replace("please", "pls")
    if r.random() < 0.3:
        text = text[0].upper() + text[1:]
    return text


def _load(G, b, r, tools, guesses=None):
    """Load the needed tool schemas the way protocol S/SP does: nothing if preloaded, else stubs (~72%) or
    get_tool_schemas for the skills family (or several families)."""
    need = [t for t in tools if t not in b.loaded]
    if not need:
        return
    if r.random() < 0.72:
        b.stub([(t, (guesses or {}).get(t, {}) if r.random() < 0.4 else {}) for t in need])
    else:
        fams = list(dict.fromkeys(G.FAMILY[t] for t in need))
        b.explicit(fams, need)


def _preload(G, r, variant, need):
    if r.random() >= 0.30:
        return []
    k = r.randint(6, 10)
    pool = [t for t in DECOY_POOL if t in G.CATALOG_TOOLS[variant] and t not in need]
    if r.random() < 0.5:
        return list(dict.fromkeys(need + r.sample(pool, k - len(need))))
    return r.sample(pool, k)


def _set_answer(n):
    return f"Volume set to {n}%." if n else "Volume set to 0%. (It's at zero, not muted.)"


# ------------------------------------------------------------------ families: fn(G, r, b, dev) -> (prefix, user_text, meta)

def f_set(G, r, b, dev):
    n = r.choice([0, 15, 20, 25, 30, 35, 40, 45, 50, 60, 65, 70, 75, 80, 90, 100]) if not dev else r.choice([30, 55, 85])
    tpl = r.choice(SET_D if dev else SET_T)
    text = tpl.format(n=n)
    _load(G, b, r, ["mac_set_volume"])
    b.call([("mac_set_volume", {"level": n}, f"volume set to {n}%")])
    b.final(_set_answer(n))
    return [], text, {"level": n}


def f_relative(G, r, b, dev):
    way = r.choice(["up", "down"])
    cur = r.choice([5, 10, 15, 30, 40, 45, 50, 60, 70, 85, 90, 95])
    new = max(0, min(100, cur + (20 if way == "up" else -20)))
    if new == cur:
        cur, new = 50, 70 if way == "up" else 30
    text = r.choice((REL_D if dev else REL_T)[way])
    _load(G, b, r, ["mac_get_volume", "mac_set_volume"])
    b.call([("mac_get_volume", {}, f"volume {cur}%, muted: false")])
    b.call([("mac_set_volume", {"level": new}, f"volume set to {new}%")])
    clamp = (way == "up" and new == 100 and cur > 80) or (way == "down" and new == 0 and cur < 20)
    b.final(f"Turned it {way} from {cur}% to {new}%" + (" (that's as far as it goes)." if clamp else "."))
    return [], text, {"from": cur, "to": new, "direction": way}


def f_read(G, r, b, dev):
    kind, text = r.choice(READ_T)
    v, muted = r.choice([20, 35, 50, 65, 80]), r.random() < 0.4
    _load(G, b, r, ["mac_get_volume"])
    b.call([("mac_get_volume", {}, f"volume {v}%, muted: {'true' if muted else 'false'}")])
    if kind == "mute":
        b.final(f"Yes, your sound is muted (the volume underneath is {v}%)." if muted else f"No, you're not muted. Volume is at {v}%.")
    else:
        b.final(f"Your volume is at {v}%, but the sound is muted." if muted else f"Your volume is at {v}%.")
    return [], text, {"volume": v, "muted": muted}


def f_mute(G, r, b, dev):
    state = r.choice(["on", "off"])
    text = r.choice(MUTE_T[state])
    _load(G, b, r, ["mac_mute"])
    b.call([("mac_mute", {"state": state}, "muted" if state == "on" else "unmuted")])
    b.final("Your Mac is muted." if state == "on" else "Sound is back on, your Mac is unmuted.")
    return [], text, {"state": state}


def f_lock(G, r, b, dev):
    text = r.choice(LOCK_D if dev else LOCK_T)
    _load(G, b, r, ["mac_lock_screen"])
    b.call([("mac_lock_screen", {}, "screen locked")])
    b.final("Your screen is locked.")
    return [], text, {"outcome": "locked"}


def f_lock_fail(G, r, b, dev):
    text = r.choice(LOCK_D if dev else LOCK_T)
    _load(G, b, r, ["mac_lock_screen"])
    if r.random() < 0.5:
        b.call([("mac_lock_screen", {}, LOCK_UNCONFIRMED)])
        b.final("Your screen is NOT locked: I sent the Lock Screen shortcut, but macOS never confirmed the lock. "
                "Press Control-Command-Q to lock it yourself.")
        return [], text, {"outcome": "unconfirmed"}
    err = r.choice(LOCK_ERRORS)
    b.call([("mac_lock_screen", {}, err)])
    why = ("Wisp isn't allowed to send keystrokes" if "keystrokes" in err else "Wisp isn't authorized to control System Events")
    b.final(f"I couldn't lock your screen, so it is NOT locked: {why}. You can allow it under System Settings > Privacy & "
            "Security, or press Control-Command-Q to lock it now.")
    return [], text, {"outcome": "error"}


def f_media(G, r, b, dev):
    action = r.choice(list(MEDIA_D) if dev else list(MEDIA_T))
    text = r.choice((MEDIA_D if dev else MEDIA_T)[action])
    _load(G, b, r, ["mac_media"])
    b.call([("mac_media", {"action": action}, f"Music: {action}")])
    b.final({"play": "Music is playing.", "pause": "Paused Music.", "next": "Skipped to the next song in Music.",
             "previous": "Went back to the previous track in Music."}[action])
    return [], text, {"action": action, "app": "music"}


def f_spotify(G, r, b, dev):
    action = r.choice(list(SPOT_T))
    text = r.choice(SPOT_T[action])
    _load(G, b, r, ["mac_media"], guesses={"mac_media": {"app": "spotify"}})
    b.call([("mac_media", {"action": action, "app": "spotify"}, f"Spotify: {action}")])
    b.final({"play": "Spotify is playing.", "pause": "Paused Spotify.", "next": "Skipped to the next track in Spotify.",
             "previous": "Went back a track in Spotify."}[action])
    return [], text, {"action": action, "app": "spotify"}


def f_specific(G, r, b, dev):
    app, text = r.choice(SPECIFIC_T)
    b.final(f"I can't pick a specific song, album or playlist: I can only play, pause or skip in {app}. "
            f"Want me to resume whatever's queued in {app}, or open {app} so you can choose it?")
    return [], text, {"app": app}


def f_specific_followup(G, r, b, dev):
    app, ask = r.choice(SPECIFIC_T)
    offer = (f"I can't pick a specific song, album or playlist: I can only play, pause or skip in {app}. "
             f"Want me to open {app} so you can choose it?")
    text = r.choice(["yes open it", "sure, open the app", "ok do that, open it", "yeah go ahead and open it"])
    _load(G, b, r, ["open_app"])
    b.call([("open_app", {"name": app}, f"opened {app}")])
    b.final(f"{app} is open, pick what you want to hear.")
    return [("user", ask), ("assistant", offer)], text, {"app": app, "history_ask": ask}


def f_declined(G, r, b, dev):
    kind, tpl = r.choice(DECLINE_D if dev else DECLINE_T)
    n = r.choice([20, 40, 60, 80])
    text = tpl.format(n=n)
    if kind == "set":
        tool, args, fin = "mac_set_volume", {"level": n}, "Okay, I didn't change the volume."
    elif kind == "mute":
        tool, args, fin = "mac_mute", {"state": "on"}, "Okay, I left the sound on."
    elif kind == "lock":
        tool, args, fin = "mac_lock_screen", {}, "Okay, I didn't lock the screen, so it's still unlocked."
    else:
        act = "pause" if "pause" in text else "next"
        tool, args, fin = "mac_media", {"action": act}, ("Okay, the music keeps playing." if act == "pause" else "Okay, I didn't skip the song.")
    _load(G, b, r, [tool])
    b.call([(tool, args, DENIED)])
    b.final(fin)
    return [], text, {"declined": tool}


def f_bad_arg(G, r, b, dev):
    if r.random() < 0.5:
        n = r.choice([30, 50, 70, 80])
        text = r.choice([f"set the volume to {n}%", f"volume at {n} percent please", f"make it {n}% volume"])
        _load(G, b, r, ["mac_set_volume"])
        bad = {"level": f"{n}%"}
        b.call([("mac_set_volume", bad, f"(error calling mac_set_volume({bad!r}): level must be integer. Expected arguments "
                                       f"— level: integer (required). Call it again with corrected arguments.)")], trainable=False)
        b.call([("mac_set_volume", {"level": n}, f"volume set to {n}%")])
        b.final(f"Volume set to {n}%.")
        return [], text, {"error": "type", "level": n}
    over = r.choice([105, 110, 120, 150])
    text = r.choice([f"max the volume out, like {over}%", f"crank it all the way, {over} percent", f"volume {over}, as loud as it goes"])
    _load(G, b, r, ["mac_set_volume"])
    b.call([("mac_set_volume", {"level": over}, RANGE_ERROR)], trainable=False)
    b.call([("mac_set_volume", {"level": 100}, "volume set to 100%")])
    b.final("Volume is at 100%, the maximum (it can't go past 100).")
    return [], text, {"error": "range", "asked": over}


FAMILIES = {   # fid: (fn, weight, split, needs)
    "mc_set": (f_set, 9, "train"), "mc_relative": (f_relative, 8, "train"), "mc_read": (f_read, 5, "train"),
    "mc_mute": (f_mute, 6, "train"), "mc_lock": (f_lock, 4, "train"), "mc_lock_fail": (f_lock_fail, 6, "train"),
    "mc_media": (f_media, 6, "train"), "mc_spotify": (f_spotify, 4, "train"), "mc_specific": (f_specific, 4, "train"),
    "mc_specific_followup": (f_specific_followup, 3, "train"), "mc_declined": (f_declined, 6, "train"),
    "mc_bad_arg": (f_bad_arg, 4, "train"),
    "mc_dev_volume": (lambda G, r, b, dev: (f_set if r.random() < 0.5 else f_relative)(G, r, b, True), 3, "dev"),
    "mc_dev_lock": (lambda G, r, b, dev: (f_lock if r.random() < 0.4 else f_lock_fail)(G, r, b, True), 2, "dev"),
    "mc_dev_media": (lambda G, r, b, dev: f_media(G, r, b, True), 2, "dev"),
    "mc_dev_declined": (lambda G, r, b, dev: f_declined(G, r, b, True), 1, "dev"),
}
NEEDS = {"mc_set": ["mac_set_volume"], "mc_relative": ["mac_get_volume", "mac_set_volume"], "mc_read": ["mac_get_volume"],
         "mc_mute": ["mac_mute"], "mc_lock": ["mac_lock_screen"], "mc_lock_fail": ["mac_lock_screen"], "mc_media": ["mac_media"],
         "mc_spotify": ["mac_media"], "mc_specific": [], "mc_specific_followup": ["open_app"], "mc_declined": [],
         "mc_bad_arg": ["mac_set_volume"], "mc_dev_volume": ["mac_set_volume"], "mc_dev_lock": ["mac_lock_screen"],
         "mc_dev_media": ["mac_media"], "mc_dev_declined": []}


def allocate(weights, total):
    s = sum(weights.values())
    raw = {k: total * w / s for k, w in weights.items()}
    out = {k: int(v) for k, v in raw.items()}
    for k in sorted(weights, key=lambda k: (-(raw[k] - out[k]), k))[:total - sum(out.values())]:
        out[k] += 1
    return out


def build(G, held, overlap_limit):
    """-> list of conversation dicts (split, fid, j, prefix, user_text, now, variant, builder, meta, overlap)."""
    out = []
    for split, total in (("train", N_TRAIN), ("dev", N_DEV)):
        fams = {f: w for f, (fn, w, s) in FAMILIES.items() if s == split}
        alloc = allocate(fams, total)
        taken = set()
        for fid in sorted(alloc):
            fn = FAMILIES[fid][0]
            for j in range(alloc[fid]):
                for attempt in range(80):
                    r = G.rng_v32("mac", fid, j, attempt)
                    variant = "web" if r.random() < 0.5 else "personal_no_web"
                    pre = _preload(G, r, variant, NEEDS[fid]) if NEEDS[fid] else []
                    b = G.Builder(r, variant, pre)
                    prefix, text, meta = fn(G, r, b, split == "dev")
                    text = _noise(r, text)
                    key = text.lower()
                    ov = max([G.heldout_overlap(held, text)] + [G.heldout_overlap(held, t) for role, t in prefix if role == "user"])
                    if key in taken or ov >= overlap_limit:
                        continue
                    taken.add(key)
                    out.append({"split": split, "fid": fid, "j": j, "prefix": prefix, "user_text": text, "now": _now(r),
                                "variant": variant, "builder": b, "meta": meta, "overlap": ov, "attempt": attempt})
                    break
                else:
                    raise RuntimeError(f"could not build {fid} #{j}")
    return out
