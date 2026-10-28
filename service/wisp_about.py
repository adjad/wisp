"""What Wisp says about itself: "What can you do?", "Who are you?", "Is my
data private?" and "How do I use Wisp?".

These are a new user's first questions, and they are often asked before the
model has finished loading. So the answers are written here, deterministic and
true, and the router hands them straight to the user (wisp_capabilities is a
presynthesized tool, see service/main.py): no model runs, nothing is guessed,
and a removed feature cannot creep back into the answer.

Rules for this text:
- Every feature named here exists in this build. A line about a tool is shown
  only when that tool is registered and usable now (routable_tool_names).
- Live state is read, never assumed: which sources Wisp can read right now,
  whether a Google account is connected, and whether a cloud model is on.
- Plain words for a first-time user. No tool names, counts or internal terms.
"""
from __future__ import annotations

import re

TOPICS = ("overview", "identity", "privacy", "howto")

# One try-it prompt per connected source, in this order, then prompts that need
# no source. Each one routes to a real, scoped tool today
# (tests/test_capability_welcome.py checks the routes).
TRY_IT = (
    ("calendar", "What's on my calendar tomorrow?"),
    ("email", "Any emails I need to reply to?"),
    ("reminders", "Remind me at 5pm to call Mom."),
    ("messages", "Any texts I need to respond to?"),
    (None, "Set a timer for 10 minutes."),
    (None, "What time is it in Tokyo?"),
    (None, "What's the weather in Paris this weekend?"),
)


def _usable_tools() -> set[str]:
    try:
        from service.tools.registry import routable_tool_names
        return routable_tool_names()
    except Exception:  # noqa: BLE001 - the answer must never fail on this
        return set()


def _sources() -> list[dict] | None:
    """Every source's live status (service/assistant/sync_status), or None when
    the readers can't be asked at all. One unreadable source never hides the rest."""
    try:
        from service.assistant.sync_status import SOURCE_IDS, source_status
    except Exception:  # noqa: BLE001
        return None
    out = []
    for source_id in SOURCE_IDS:
        try:
            out.append(source_status(source_id))
        except Exception:  # noqa: BLE001
            continue
    return out


def _google_connected() -> bool:
    try:
        from service.gmail_source import connected_accounts
        return bool(connected_accounts())
    except Exception:  # noqa: BLE001
        return False


def _google_sign_in() -> bool:
    """Whether this build can sign in to Google at all (gmail_source.can_connect),
    so "connect a Google account" is offered only where Settings → Google can do it."""
    try:
        from service.gmail_source import can_connect
        return can_connect()
    except Exception:  # noqa: BLE001 - offer nothing rather than a dead end
        return False


def _rhythm() -> str:
    """What Wisp does on its own and when (brief.rhythm_line), or nothing when
    the settings behind it can't be read."""
    try:
        from service.assistant.brief import rhythm_line
        return rhythm_line()
    except Exception:  # noqa: BLE001
        return ""


def _host(url: object) -> str:
    from urllib.parse import urlsplit
    try:
        return urlsplit(str(url or "")).hostname or "your cloud provider"
    except ValueError:
        return "your cloud provider"


_GENERIC_HOST = "your cloud provider"
# What each cloud-assignable role answers, in the user's words.
_ROLE_LABELS = {"reasoning": "hard reasoning questions", "coding": "code questions",
                "research": "research"}


_UNKNOWN = object()


def _read(read):
    """A setting's value, or _UNKNOWN when it can't be read. Each one is read
    on its own, so one broken setting can't hide another."""
    try:
        return read()
    except Exception:  # noqa: BLE001
        return _UNKNOWN


def cloud_detail() -> dict:
    """Everything the answers need to say where Wisp's AI runs:

    mode     "everything" (a remote-only build, or Cloud "Everything" turned
             on), "super" (an optional cloud model answers some questions),
             or "" (all on this Mac)
    host     the provider's host, or "your cloud provider" when the config
             can't name it
    pending  Cloud "Everything" is turned on but takes over only after Wisp
             restarts; until then this Mac answers, except roles still bound
             to the optional cloud model (super_model/roles/cloud_host say
             which: turning Everything on doesn't unbind them)
    super_model, roles   which optional cloud model is on: the Super Model,
             and/or roles bound to the cloud endpoint
    known    False when a setting couldn't be read. Unknown is never read as
             local: an unreadable Everything setting reports "everything", an
             unreadable optional model "super", each with the generic host.
             (profile.may_send_to treats unknown as "send nothing".)"""
    def result(mode="", host="", *, pending=False, super_model=False, roles=(), known=True,
               cloud_host=""):
        return {"mode": mode, "host": host, "pending": pending, "super_model": super_model,
                "roles": list(roles), "known": known, "cloud_host": cloud_host or host}

    try:
        from service import config
        from service.inference import remote_policy
    except Exception:  # noqa: BLE001
        return result("everything", _GENERIC_HOST, known=False)
    profile = _read(remote_policy.remote_only_profile)
    if profile is not None and profile is not _UNKNOWN:
        return result("everything", _host(getattr(profile, "base_url", "")))
    everything = _read(config.cloud_everything_enabled)
    if profile is _UNKNOWN or everything is not False:
        remote = _read(lambda: (config.models_config().get("inference") or {}).get("remote_only"))
        host = _host(remote.get("base_url")) if isinstance(remote, dict) else _GENERIC_HOST
        if everything is True and profile is None:
            # Turned on but not active: remote_only_profile() is None until a
            # restart, and until then the role bindings still apply.
            super_model, roles, cloud_host = _optional_cloud_now(config)
            return result("everything", host, pending=True, super_model=super_model,
                          roles=roles, cloud_host=cloud_host)
        return result("everything", host if everything is True else _GENERIC_HOST, known=False)
    settings = _read(config.cloud_provider_settings)
    super_model = _read(config.cloud_super_model_enabled)
    if isinstance(settings, dict):
        roles = [r for r in settings.get("roles") or () if r in _ROLE_LABELS] \
            if settings.get("enabled") else []
        cloud_host = _host(settings.get("base_url"))
    else:
        roles, cloud_host = [], _GENERIC_HOST
    known = isinstance(settings, dict) and super_model is not _UNKNOWN
    if (super_model is True or roles) and _cloud_is_own_node():
        # The "cloud" endpoint is the user's own verified node: named as
        # theirs (own_nodes), not as a cloud model.
        super_model, roles = False, []
    if super_model is True or roles:
        return result("super", cloud_host, super_model=super_model is True, roles=roles, known=known)
    if not known:
        return result("super", _GENERIC_HOST, known=False)
    return result()


# The user's own Mac (a mini or an Air) on their tailnet. The user decided that
# their data may cross the tailnet to it (docs: mini = proactive node). It is
# recognised only by its provisioning record, never by its hostname or URL:
# the endpoint must use the mini inference credential, and
# credentials.verify_binding must accept its exact origin (an owner-only
# record naming a *.ts.net host).
_NODE_CREDENTIAL = "env:WISP_MINI_INFERENCE_KEY"


def own_node(endpoint) -> str:
    """A short name for `endpoint` ("mini") when it is the user's own verified
    tailnet node, else ""."""
    if endpoint is None or getattr(endpoint, "credential_ref", None) != _NODE_CREDENTIAL:
        return ""
    url = str(getattr(endpoint, "base_url", "") or "")
    try:
        from service.config.credentials import verify_binding
        verify_binding(_NODE_CREDENTIAL.removeprefix("env:"), url, purpose="inference")
    except Exception:  # noqa: BLE001 - unverified is not the user's node
        return ""
    from urllib.parse import urlsplit
    host = urlsplit(url).hostname or ""
    return host.split(".")[0] or "node"


def own_nodes() -> list[str]:
    """Short names of the user's verified tailnet nodes that a role answers on now."""
    try:
        from service.config import models_config
        from service.config.endpoints import endpoint
        bindings = (models_config().get("inference") or {}).get("bindings") or {}
    except Exception:  # noqa: BLE001
        return []
    # "cloud" is included: what makes an endpoint the user's node is its
    # verified provisioning record, never its name.
    names = sorted({str(b.get("endpoint")) for b in bindings.values()
                    if isinstance(b, dict) and b.get("endpoint")} - {"local", "local_provider"})
    labels: list[str] = []
    for name in names:
        try:
            label = own_node(endpoint(name))
        except Exception:  # noqa: BLE001
            continue
        if label and label not in labels:
            labels.append(label)
    return labels


def nodes_phrase(nodes: list[str]) -> str:
    """"your mini on your tailnet" for the lock line and the answers."""
    return f"your {_join(nodes)} on your tailnet" if nodes else ""


def _cloud_is_own_node() -> bool:
    """Whether the endpoint named "cloud" is really the user's own verified
    node: then nothing bound to it is an optional cloud model."""
    try:
        from service.config.endpoints import endpoint
        return bool(own_node(endpoint("cloud")))
    except Exception:  # noqa: BLE001
        return False


def _optional_cloud_now(config) -> tuple[bool, list[str], str]:
    """(Super Model on, roles bound to the cloud endpoint, its host), read
    from the bindings themselves: cloud_provider_settings() reports only
    "Everything" once that is turned on."""
    super_model = _read(config.cloud_super_model_enabled) is True
    cfg = _read(config.models_config)
    inference = (cfg.get("inference") or {}) if isinstance(cfg, dict) else {}
    endpoint = (inference.get("endpoints") or {}).get("cloud")
    enabled = isinstance(endpoint, dict) and endpoint.get("enabled", True) is True
    bindings = inference.get("bindings") or {}
    roles = [role for role in _ROLE_LABELS if enabled and isinstance(bindings.get(role), dict)
             and bindings[role].get("endpoint") == "cloud"]
    host = _host(endpoint.get("base_url")) if isinstance(endpoint, dict) else _GENERIC_HOST
    if (super_model or roles) and _cloud_is_own_node():
        return False, [], ""
    return super_model, roles, host


def cloud_state() -> tuple[str, str]:
    """(mode, host) from cloud_detail(), for every answer that says where Wisp's
    AI runs (this module, the profile's "Stays on this Mac" and its prompt
    predicate, wisp_status's models report): one helper, so they can't drift."""
    detail = cloud_detail()
    return detail["mode"], detail["host"]


def _auto_add_on() -> bool:
    """Whether the Brief may add a time-sensitive event or deadline to Calendar
    or Reminders on its own (service/proactive/autoadd.py). Off by default."""
    try:
        from service.config import proactive_auto_add_enabled
        return proactive_auto_add_enabled()
    except Exception:  # noqa: BLE001 - say less rather than claim more
        return True


def _write_mode() -> str:
    """How Wisp's own reminder and hold writes stand right now, by the same
    check behavior_runtime.policy_block makes (safety.decide on
    assistant_write): "view_only" (confirmed View Only), "allow" (they run
    without asking, as under Full access), "suggest" (the policy refuses them,
    so routine actions are only offered), or "unknown"."""
    try:
        from service.safety import Tier, decide, full_access, read_only
        if read_only() and not full_access():
            return "view_only"
        decision = decide("assistant_write", {"title": "", "kind": "reminder"}, tool="add_reminder")
    except Exception:  # noqa: BLE001 - say the cautious version
        return "unknown"
    return "allow" if decision.tier is Tier.ALLOW else "suggest"


def _routine_state() -> str:
    """Routine actions (service/assistant/behavior_runtime.py): "off" when
    behavior learning is off, "held" during the first-days hold (only offered),
    "acting" after it, "unknown" when the runtime can't be asked. Never
    starts the runtime just to answer."""
    try:
        import time
        # The switch first: importing behavior_runtime opens its stores.
        from service.behavior_switch import enabled
        if not enabled():
            return "off"
        from service.assistant import behavior_runtime
        runtime = getattr(behavior_runtime, "_runtime", None)
        if runtime is None:
            return "unknown"
        from service.assistant.store import assistant_store
        return "held" if runtime.auto_actions_held(assistant_store, time.time()) else "acting"
    except Exception:  # noqa: BLE001
        return "unknown"


def _attention_state() -> tuple[str, bool] | None:
    """Caught for you (service/assistant/attention_runner.py): (mode, capture_events)
    from attention/settings.json, read purely. A missing file is the default
    (shadow, no events); a file that can't be read or parsed is None, so the
    answer can say the cautious version. (The runner itself reads such a file
    as "off".)"""
    try:
        import json
        from service.attention.live import Settings, settings_path
        from service.paths import MOE_DIR
        try:
            raw = settings_path(MOE_DIR).read_text(encoding="utf-8")
        except FileNotFoundError:
            return Settings().mode, Settings().capture_events
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        settings = Settings(**{k: v for k, v in data.items() if k in Settings.__dataclass_fields__}).validate()
    except Exception:  # noqa: BLE001
        return None
    return settings.mode, settings.capture_events


_SEND_OK = "Before anything is sent, I show it to you and wait for your OK."
_CALENDAR_OK = "Before anything is sent, or your calendar changes, I show it to you and wait for your OK."
_AUTO_ADD = ("Because auto-add is on, I may add a time-sensitive event or deadline to your Calendar "
             "or Reminders on my own, then tell you, with Undo.")
_ROUTINE = {
    "held": ("For your first few days I only suggest reminders from your routine. After that I may "
             "add a reminder or a Tentative hold in Wisp's own schedule on my own and tell you, with Undo."),
    "acting": ("I may add a reminder or a Tentative hold in Wisp's own schedule from your routine on "
               "my own, and tell you, "
               "with Undo."),
    "unknown": ("Depending on your settings, I may add a reminder or a Tentative hold in Wisp's own "
                "schedule from your "
                "routine on my own, and tell you, with Undo."),
}
_CAUGHT = "what Caught for you finds in your mail and messages"


def _automatic_adds() -> tuple[str, list[tuple[str, str, bool]]]:
    """(write mode, the automatic Calendar/Reminders writers that may act now)
    as (full sentence, fragment for a combined sentence, cautious). Every
    automatic writer Wisp has is here: proactive auto-add, routine actions
    (behavior_runtime) and Caught for you (attention_runner live reminders and
    capture_events). The last two pass the same assistant_write gate as
    _write_mode, so a refusal there means they don't write."""
    mode = _write_mode()
    adds: list[tuple[str, str, bool]] = []
    if _auto_add_on():
        adds.append((_AUTO_ADD, "a time-sensitive event or deadline to your Calendar or Reminders "
                                "(auto-add is on)", False))
    routine = _routine_state()
    if mode == "unknown" and routine != "off":
        routine = "unknown"
    if mode != "suggest" and routine in _ROUTINE:
        # Routine actions stay in Wisp's own schedule, never Apple Reminders
        # or Calendar (behavior_runtime.py's "Acting").
        fragment = ("after your first few days, reminders or Tentative holds in Wisp's own schedule, "
                    "from your routine" if routine == "held" else
                    "reminders or Tentative holds in Wisp's own schedule, from your routine")
        adds.append((_ROUTINE[routine], fragment, routine == "unknown"))
    attention = _attention_state()
    if attention is None or (mode == "unknown" and attention[0] == "live"):
        adds.append(("Depending on your Caught for you settings, I may add reminders or Calendar "
                     "events from your mail and messages on my own, and tell you.",
                     f"reminders or Calendar events from {_CAUGHT}", True))
    elif mode == "allow" and attention[0] == "live":
        what = "reminders and Calendar events" if attention[1] else "reminders"
        adds.append((f"From {_CAUGHT}, I may add {what} on my own and tell you.",
                     f"{what} from {_CAUGHT}", False))
    return mode, adds


def _confirm_line() -> str:
    """What Wisp asks before it acts, true in each state. Sending always waits
    for an OK. "Your calendar changes only with your OK" is said only when every
    automatic writer is confirmed off or blocked: View Only, or none of them
    can add on its own. Anything that can't be read gets the cautious wording."""
    mode, adds = _automatic_adds()
    if mode == "view_only":
        # View Only blocks every automatic add: assistant_write is refused, and
        # autoadd.py checks view-only and the calendar policy itself.
        return _SEND_OK + " In View Only, nothing is added to your calendar or reminders without your OK."
    suggest_only = mode == "suggest" and _routine_state() in ("held", "acting")
    tail = (" Right now I only suggest reminders from your routine; you add them with a tap."
            if suggest_only else "")
    if not adds:
        return (_SEND_OK if mode == "unknown" else _CALENDAR_OK) + tail
    if len(adds) == 1:
        return _SEND_OK + " " + adds[0][0] + tail
    fragments = [fragment for _, fragment, _ in adds]
    joined = "; ".join(fragments[:-1]) + "; and " + fragments[-1]
    cautious = any(c for _, _, c in adds)
    lead = "Depending on your settings, on my own" if cautious else "On my own"
    return f"{_SEND_OK} {lead} I may add {joined}, and tell you each time." + tail


def _user_name() -> str:
    try:
        from service.memory.profile import load
        return load().get("name") or ""
    except Exception:  # noqa: BLE001
        return ""


def _join(labels: list[str]) -> str:
    if len(labels) <= 1:
        return "".join(labels)
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def _status_lines(sources: list[dict] | None) -> list[str]:
    if not sources:
        return ["I couldn't check which of your apps are connected just now."]
    lines = []
    ready = [s["label"] for s in sources if s.get("state") == "ready"]
    syncing = [s["label"] for s in sources if s.get("state") == "syncing"]
    if ready:
        lines.append(f"Connected: {', '.join(ready)}.")
    if syncing:
        lines.append(f"Still reading for the first time: {', '.join(syncing)}.")
    for source in sources:
        if source.get("state") in ("ready", "syncing"):
            continue
        reason = str(source.get("reason") or "").strip()
        lines.append(f"Not connected: {source['label']}" + (f". {reason}" if reason else "."))
    return lines


# The tool each source-free try-it needs, so a prompt is never offered for a
# feature this build can't run.
_TRY_TOOL = {"Set a timer for 10 minutes.": "set_timer", "What time is it in Tokyo?": "world_time",
             "What's the weather in Paris this weekend?": "get_weather"}


def _try_its(sources: list[dict] | None, tools: set[str]) -> list[str]:
    connected = {s.get("id") for s in sources or () if s.get("state") == "ready"}
    def usable(prompt: str) -> bool:
        needed = _TRY_TOOL.get(prompt)
        return not tools or needed is None or needed in tools
    return [prompt for source, prompt in TRY_IT
            if (source is None or source in connected) and usable(prompt)][:3]


def _can_lines(tools: set[str]) -> list[str]:
    lines = []
    reads = [label for label, needed in (
        ("calendar", {"get_upcoming"}), ("reminders", {"get_upcoming"}),
        ("mail", {"summarize_emails"}), ("messages", {"summarize_messages"}),
        ("notes", {"search_notes"})) if needed <= tools]
    if reads:
        lines.append(f"Read and sum up your {_join(reads)}, and tell you what needs a reply.")
    acts = [label for label, needed in (
        ("draft and send emails and texts", {"send_email", "send_message"}),
        ("add reminders and calendar events", {"add_reminder", "add_calendar_event"}),
        ("move events on your calendar", {"update_event"})) if needed <= tools]
    if acts:
        joined = "; ".join(acts) if len(acts) > 2 else " and ".join(acts)
        lines.append(joined[0].upper() + joined[1:] + ". " + _confirm_line())
    looks = [label for label, needed in (
        ("search the web", {"web_search"}), ("check the weather", {"get_weather"}),
        ("do quick math", {"calculate"})) if needed <= tools]
    if looks:
        joined = _join(looks)
        lines.append(joined[0].upper() + joined[1:] + ".")
    if "toggle_setting" in tools or "open_app" in tools:
        lines.append("Open apps and change Mac settings like Do Not Disturb or dark mode.")
    if "remember" in tools:
        lines.append("Remember what you tell me (\"remember that my locker is 112\").")
    return lines


def until_restart(detail: dict) -> str:
    """Who answers before Cloud "Everything" takes over: this Mac, except any
    role still bound to the optional cloud model."""
    nodes = own_nodes()
    here = f"this Mac and {nodes_phrase(nodes)} answer" if nodes else "this Mac answers"
    still = [_ROLE_LABELS[r] for r in detail.get("roles") or ()]
    if detail.get("super_model"):
        still.append("questions the Super Model takes")
    if not still:
        return f"Until then, {here}."
    return (f"Until then, {here}, except {_join(still)}, which still go to "
            f"{detail.get('cloud_host') or detail['host']}.")


def _pending_line(detail: dict) -> str:
    return (f"Cloud \"Everything\" is turned on and takes over after Wisp restarts. From then on, "
            f"what you ask, and the data a reply needs, is sent to {detail['host']}. "
            + until_restart(detail))


_UNKNOWN_LINE = ("I couldn't read the cloud settings just now, so I can't promise everything stays "
                 "on this Mac. Check the cloud model in **Settings**.")


def _greeting() -> str:
    """The overview's first line. "Runs on this Mac" is said only when it is true."""
    detail = cloud_detail()
    host = detail["host"]
    if not detail["known"]:
        return "**Hi, I'm Wisp.** " + _UNKNOWN_LINE
    if detail["mode"] == "everything" and detail["pending"]:
        return "**Hi, I'm Wisp.** " + _pending_line(detail)
    if detail["mode"] == "everything":
        return (f"**Hi, I'm Wisp.** Right now I'm answering with a cloud model at {host}, not on "
                "this Mac. Turn it off in **Settings** to keep everything here.")
    if detail["mode"] == "super":
        return (f"**Hi, I'm Wisp.** My AI runs right here on this Mac. You've also turned on a "
                f"cloud model, so questions it answers go to {host}.")
    nodes = own_nodes()
    if nodes:
        return f"**Hi, I'm Wisp.** My AI runs right here on this Mac and {nodes_phrase(nodes)}."
    return "**Hi, I'm Wisp.** My AI runs right here on this Mac."


def overview() -> str:
    """"What can you do?": what Wisp does (only usable features), which sources
    it can read right now, three try-its that fit them, and where things are."""
    tools = _usable_tools()
    sources = _sources()
    out = [_greeting(), "", "**What I can do**"]
    out += ["- " + line for line in _can_lines(tools)]
    if _google_connected():
        out.append("- Google: your account is connected, so I read its Gmail and Calendar too.")
    elif _google_sign_in():
        out.append("- Google: connect a Google account in **Settings → Google** and I'll read its "
                   "Gmail and Calendar too.")
    rhythm = _rhythm()
    if rhythm:
        out.append("- " + rhythm)
    out += ["", "**Right now**"] + _status_lines(sources)
    out += ["", "**Try asking**"] + [f"- \"{prompt}\"" for prompt in _try_its(sources, tools)]
    out += ["",
            "**Where things are**",
            "- Press ⌥ Space to ask me from anywhere, or ⌥⇧Space to ask out loud.",
            "- Type / in Ask to see shortcuts, like /today and /report.",
            "- Brief and Day are the tabs next to Ask: a summary of what's new, and your plan for today.",
            "- Getting to know you (type /about) tells me your name and what matters to you, "
            "so my answers fit you better.",
            "",
            "Ask \"is my data private?\" or \"how do I use Wisp?\" any time."]
    return "\n".join(out)


def identity() -> str:
    name = _user_name()
    detail = cloud_detail()
    cloud, host = detail["mode"], detail["host"]
    if not detail["known"]:
        intro = ("I'm **Wisp**, your assistant in the Mac's notch. I help with your calendar, "
                 "reminders, mail, messages and notes. " + _UNKNOWN_LINE)
    elif cloud == "everything" and detail["pending"]:
        intro = ("I'm **Wisp**, your assistant in the Mac's notch. I help with your calendar, "
                 "reminders, mail, messages and notes. " + _pending_line(detail))
    elif cloud == "everything":
        intro = ("I'm **Wisp**, your assistant in the Mac's notch. I help with your calendar, "
                 "reminders, mail, messages and notes. Right now a cloud model is on, so what you "
                 f"ask, and the data a reply needs, is sent to {host} to be answered.")
    elif cloud == "super":
        intro = ("I'm **Wisp**, your assistant in the Mac's notch. My AI runs on this Mac and helps "
                 "with your calendar, reminders, mail, messages and notes. You've also turned on a "
                 f"cloud model, so questions it answers go to {host}.")
    elif own_nodes():
        intro = ("I'm **Wisp**, your assistant in the Mac's notch. My AI runs on this Mac and "
                 f"{nodes_phrase(own_nodes())}, so I can help with your calendar, reminders, mail, "
                 "messages and notes while keeping them on your own Macs.")
    else:
        intro = ("I'm **Wisp**, your assistant in the Mac's notch. My AI runs on this Mac, so I can "
                 "help with your calendar, reminders, mail, messages and notes without sending them "
                 "to a server.")
    out = [intro,
           "",
           "Open me any time with **⌥Space** or by moving the pointer to the notch. Ask me anything "
           "(press **⌥⇧Space** to ask out loud), check **Brief** for your day at a glance, or **Day** "
           "for today's timeline."]
    if name:
        out += ["", f"And you're {name}: you told me in Getting to know you. Type **/about** to change it."]
    else:
        out += ["", "I don't know your name yet. Type **/about** and I'll ask three quick questions."]
    out += ["", "Ask \"what can you do?\" for the full tour."]
    return "\n".join(out)


def _optional_cloud_lines(detail: dict) -> list[str]:
    """What an optional cloud model is sent, exactly (service/main.py's direct
    chat branch and super-model path; profile.may_send_to keeps About you off
    both)."""
    host = detail["host"]
    lines = []
    if detail["super_model"]:
        lines.append(f"- The Super Model you turned on answers some general questions at {host}. "
                     "It gets only the question itself: no conversation, memory or personal data.")
    roles = [_ROLE_LABELS[r] for r in detail["roles"]]
    if roles:
        lines.append(f"- You've set a cloud model at {host} to answer {_join(roles)}. When it "
                     "answers, the question goes there with the recent conversation, things you "
                     "asked me to remember that relate to it, patterns I've noticed in your "
                     "routines, and the instructions of any skill in use (like a writing-style "
                     "skill). "
                     + ("Research also sends the web pages it read, and a"
                        if "research" in detail["roles"] else "A")
                     + " document search you upgrade sends the passages it answers from.")
    if not lines:
        lines.append(f"- A cloud model may answer some questions, at {host}. Check **Settings** "
                     "to see which.")
    lines.append("- What you told me in Getting to know you is never sent to it.")
    return lines


def privacy() -> str:
    detail = cloud_detail()
    cloud, host = detail["mode"], detail["host"]
    if not detail["known"]:
        out = ["**I couldn't check whether a cloud model is on.**", "",
               "- " + _UNKNOWN_LINE,
               "- Your mail, messages, calendar, notes and what you tell me are stored on this Mac.",
               "- What you told me in Getting to know you goes only to Wisp's own model on this Mac "
               + (f"and {nodes_phrase(own_nodes())} " if own_nodes() else "")
               + "while I can't check."]
    elif cloud == "everything" and detail["pending"]:
        out = ["**Cloud \"Everything\" is turned on.** It takes over after Wisp restarts. "
               + until_restart(detail), "",
               f"- After the restart, what you ask, and the data a reply needs (your mail, "
               f"messages, calendar and what you told me about yourself), is sent to {host}.",
               ("- Until the restart, what you told me in Getting to know you stays on your own "
                f"Macs: this one and {nodes_phrase(own_nodes())}, over your tailnet."
                if own_nodes() else
                "- Until the restart, what you told me in Getting to know you stays on this Mac."),
               "- To keep everything on this Mac, turn it off in **Settings** before restarting."]
        if detail["roles"] or detail["super_model"]:
            out += _optional_cloud_lines({**detail, "host": detail["cloud_host"]})[:-1]
    elif cloud == "everything":
        out = ["**Right now a cloud model is on**, so this is different from usual.", "",
               f"- What you ask, and the data a reply needs (your mail, messages, calendar and what "
               f"you told me about yourself), is sent to {host} to be answered.",
               "- To keep everything on this Mac again, turn the cloud model off in **Settings**."]
    elif cloud == "super":
        out = ["**Your data is stored on this Mac**, and you've turned on a cloud model for some "
               "questions.", "",
               "- My main AI model runs here, on your Mac. Your mail, messages, calendar, notes, "
               "and what you tell me are read and stored on this Mac."]
        out += _optional_cloud_lines(detail)
    elif own_nodes():
        out = ["**Your data stays on your own Macs.**", "",
               "- My AI model runs here, on your Mac. Your mail, messages, calendar, notes, "
               "and what you tell me are read and kept on your own Macs, not on anyone else's server.",
               "- No cloud AI is used unless you turn one on in **Settings**."]
    else:
        out = ["**Your data stays on this Mac.**", "",
               "- My AI model runs here, on your Mac. Your mail, messages, calendar, notes, "
               "and what you tell me are read and kept on this Mac, not on a server.",
               "- No cloud AI is used unless you turn one on in **Settings**."]
    # Everything (answering or pending) says where the profile goes itself.
    nodes = own_nodes() if cloud != "everything" else []
    if nodes:
        out.append(f"- Some questions are answered by {nodes_phrase(nodes)}, your own Mac. What a "
                   "reply needs, including what you told me in Getting to know you, goes there "
                   "over your tailnet and nowhere else.")
    online = "- Some things have to go online: web searches, weather and stocks."
    if _google_connected():
        online += " I fetch your Google account's mail and calendar from Google."
    elif _google_sign_in():
        online += " If you connect Google, I fetch that account's mail and calendar from Google."
    out += [online,
            "- " + _confirm_line(),
            "- You decide what I can read, in System Settings → Privacy & Security.",
            "- To see or erase what I know about you: **/about** for Getting to know you, and "
            "**/memory** for things I've remembered."]
    return "\n".join(out)


# How-to entries: (keywords that pick it, the step). A specific question
# ("how do I connect Google?") gets only its matching entries; anything else
# gets the whole guide.
_HOWTO = (
    (r"open|start|launch|find|summon|shortcut|hotkey|notch",
     "**Open Wisp**: press **⌥Space** from any app, or move the pointer to the notch."),
    (r"voice|speak|talk|dictat|microphone|\bmic\b|siri|out loud|aloud|hands.free|say it",
     "**Ask by voice**: press **⌥⇧Space** from any app, or click the mic in Ask (or in Brief and "
     "Day to ask about your day). Wisp sends your question when you pause and reads the answer "
     "aloud; choose when in Settings (the gear) → **Read answers aloud**. Your voice is turned "
     "into text by my model on this Mac; I keep no recording and it never leaves your Mac. It needs a model "
     "that can hear, like Gemma 4 E4B."),
    (r"ask|talk|chat|use|question|type",
     "**Ask**: type your question and press Return. Type **/** to see shortcuts. "
     "**⌘N** starts a new chat; **⌘O** continues it in the Wisp window."),
    (r"brief|day|summary|plan|schedule|today",
     "**Brief and Day**: the tabs next to Ask. Brief is your day at a glance; Day is today's "
     "timeline of events and reminders."),
    (r"about me|know me|my name|profile|getting to know|tone|goals?|call me",
     "**Tell Wisp about you**: type **/about**. Change or erase it any time in the Wisp "
     "window's Memory → About you."),
    (r"mail suggestion|suggestions? from (?:my )?(?:mail|email)|found in your mail|old(?:er)? (?:mail|email)|mail memory",
     "**Mail suggestions**: when nobody is asking me anything, I read your Sent mail and Mail's "
     "Primary, Transactions and Updates from the last 90 days, on this Mac, and find dates you "
     "might forget. The Brief shows only the important ones (yours to keep, from a person, about "
     "money, an account or an appointment) due within 30 days: **Add** puts one in Reminders. "
     "I never read spam, trash, archive or Mail's Promotions. Turn it off or to Record only in "
     "Settings → Privacy & Access → **Mail suggestions**."),
    (r"remember|memory|forget",
     "**Memory**: say \"remember that…\" and I'll keep it. Type **/memory** to see or delete "
     "what I've saved."),
    (r"google|gmail|workspace|drive",
     "**Connect Google**: open **Settings → Google**, then sign in. I'll read that account's "
     "Gmail and Calendar too."),
    (r"permission|access|allow|grant|privacy|calendar access|full disk",
     "**Let Wisp read more**: open **Settings → Privacy & Access**, choose **Permissions…**, "
     "and click **Allow** next to what you want. Screen Recording and Accessibility are under "
     "**More Access…** next to it."),
    (r"problem|bug|broken|wrong|report|feedback",
     "**Something wrong?** Type **/report**, or choose **Report a Problem…** from the menu "
     "bar icon."),
    (r"log ?in|sign ?in|login|signin|password",
     "**Signing in**: Wisp has no account of its own, so there's nothing to sign in to. "
     "To read a Google account's Gmail and Calendar, connect it in **Settings → Google**."),
    (r"log ?out|sign ?out|logout|signout",
     "**Signing out**: Wisp has no account to sign out of. To stop reading a Google account, "
     "choose **Disconnect & erase** in **Settings → Google**. That also removes the mail, "
     "events and to-dos Wisp kept from it."),
    (r"update (?:wisp|the app|this app|you)|upgrade|new version|latest version",
     "**Updating Wisp**: Wisp doesn't update itself. To move to a new version, quit Wisp from "
     "the menu bar icon, then replace the Wisp app in your Applications folder with the new one."),
    (r"quit|close|stop|turn off",
     "**Close or quit**: × closes the panel and Wisp stays in the menu bar. To quit, choose "
     "**Quit Wisp** from the menu bar icon."),
)

_GENERIC_HOWTO = re.compile(
    r"\b(?:use|get(?:ting)? started|work|works|set ?up|begin|help)\b", re.I)


# The steps that point at Settings → Google, for a build that can't sign in
# to Google and has no account connected: (answer to a direct question, step
# in the whole guide, or None to leave it out).
_HOWTO_WITHOUT_GOOGLE = {
    r"google|gmail|workspace|drive":
        ("**Google**: this version of Wisp can't sign in to Google. If your Gmail is in Apple's "
         "Mail app, or your Google calendar is in Calendar, I read it from there.", None),
    r"log ?in|sign ?in|login|signin|password":
        ("**Signing in**: Wisp has no account of its own, so there's nothing to sign in to.",
         "**Signing in**: Wisp has no account of its own, so there's nothing to sign in to."),
    r"log ?out|sign ?out|logout|signout":
        ("**Signing out**: Wisp has no account to sign out of.",
         "**Signing out**: Wisp has no account to sign out of."),
}


def _howto_steps(guide: bool) -> list[tuple[str, str]]:
    """The how-to entries this build can follow: without Google sign-in or a
    connected account, the Google steps change (or drop out of the guide)."""
    if _google_connected() or _google_sign_in():
        return list(_HOWTO)
    steps = []
    for keys, step in _HOWTO:
        if keys in _HOWTO_WITHOUT_GOOGLE:
            step = _HOWTO_WITHOUT_GOOGLE[keys][1 if guide else 0]
        if step:
            steps.append((keys, step))
    return steps


def howto(query: str = "") -> str:
    text = (query or "").lower()
    specific = [step for keys, step in _howto_steps(guide=False)
                if re.search(rf"\b(?:{keys})", text) and not _GENERIC_HOWTO.search(text)]
    if specific:
        return "\n".join(["- " + step for step in specific]
                         + ["", "Ask \"how do I use Wisp?\" for the whole guide."])
    return "\n".join(["**Getting started with Wisp**", ""]
                     + ["- " + step for _, step in _howto_steps(guide=True)])


def answer(topic: str = "overview", query: str = "") -> str:
    topic = (topic or "overview").strip().lower()
    if topic == "identity":
        return identity()
    if topic == "privacy":
        return privacy()
    if topic == "howto":
        return howto(query)
    return overview()


# A general "what can you do" (not "can you send texts?", which keeps the
# precise Yes/No capability report).
_GENERAL_INVENTORY = re.compile(
    r"\W*(?:(?:please|hey|hi|ok|okay|so|wisp)[,\s]+)*(?:"
    r"what\s+(?:else\s+)?(?:can|could)\s+you\s+(?:do|help\s+(?:me\s+)?with)|"
    r"what\s+(?:tools|capabilities)\s+(?:are\s+available|do\s+you\s+have)(?:\s+to\s+you)?|"
    r"which\s+(?:assistant\s+)?capabilities\s+do\s+you\s+have|"
    r"what\s+are\s+you\s+able\s+to\s+do)"
    r"(?:\s+(?:for\s+me|here|now|right\s+now|exactly))?[?.!\s]*", re.I)


def is_general_inventory(query: str) -> bool:
    return not (query or "").strip() or bool(_GENERAL_INVENTORY.fullmatch(query or ""))
