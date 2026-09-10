"""Tool-subset scoping — regression tests.

Two facts drive this file.

1. A long tool list measurably degrades tool SELECTION. The project's own
   measurement, same prompt and decoding: 5-7 domain tools -> 3/3 correct calls,
   all 44 -> 2/3, and it reached for an unrelated tool. Scoping is a reliability
   measure first and a token saving second.

2. Before this, 24 of 55 registered tools — 44% of the registry — were reachable
   ONLY by falling through to the undifferentiated full-toolset route. Device
   control, apps and media, and live-web lookups were the three big clusters
   with no home of their own, so every "what's my battery", "play some music"
   and "what's the weather" paid ~13,300 tokens of fixed overhead and got the
   worst selection accuracy available.

What must keep holding:
  * every registered tool is reachable through SOME scoped route, except
    delete_path, which is deliberately left to the unscoped route because it is
    destructive and must not ride along in a broad subset;
  * the ambiguous fallback offers the core set, not the whole registry, and
    still includes installed SKILL tools — those are directly callable, so a
    static core silently breaks skill invocation;
  * a request naming the user's own data keeps the full toolset when the intent
    is genuinely two-way ("open my calendar" could be open_app or get_upcoming);
  * the warm read-out voice (_LIGHT_READ_STYLE) is applied to personal-data
    reads ONLY — never to device control, web lookups, or the fallback.

    .venv/bin/python tests/test_router_scoping.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.skills import load as load_skills  # noqa: E402

load_skills()  # register use_skill + the user's installed skill tools

import service.router.router as R  # noqa: E402
from service.router.router import route  # noqa: E402
from service.router.tool_aliases import apply as apply_aliases  # noqa: E402
from service.tools import tool_schemas  # noqa: E402
from service.tools.registry import REGISTRY  # noqa: E402

# The real runtime always attaches tool_aliases.py's table before retrieval
# ever runs (ToolIndex.build() calls this first thing — see semantic.py) — a
# retrofitted tool's aliases exist in that table from the moment it's added,
# but REGISTRY[name].aliases stays empty until something actually calls
# apply(). Without this, "does it have aliases" below would test the
# freshly-imported REGISTRY (always aliases=[]) instead of what a real turn
# ever sees, and wrongly flag every alias-only tool as unreachable.
apply_aliases()

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def dec(prompt: str):
    return asyncio.run(route(prompt))


def subset(prompt: str) -> list[str]:
    return dec(prompt).tool_subset or []


# --------------------------------------------------------------------------

def test_every_tool_has_a_scoped_home() -> None:
    print("\nevery tool is reachable through some scoped route")
    reachable: set[str] = set()
    # _DOCUMENT_TOOLS / _SYSTEM_CONTROL_TOOLS / _APPS_MEDIA_TOOLS are gone
    # (2026-08-21) — those three routes still claim their phrasings (the
    # regexes are unchanged), but now fill tool_subset by the SAME semantic
    # retrieval this test's own "unreachable" check already trusts for every
    # OTHER orphaned tool below, rather than a hand-written list. Nothing to
    # union in here for them any more; their tools just need aliases, like
    # anything else retrieval covers.
    for group in (R._ALL_SOURCES, R._MEMORY_TOOLS,
                  R._SCHEDULED_SEND_TOOLS, R._WEB_TOOLS, R._core_tools(),
                  ["get_recent_activity", *R._ALL_SOURCES],   # recency sweep route
                  ["view_messages", "summarize_messages"],
                  ["view_emails", "summarize_emails"],
                  ["get_upcoming"], ["get_past_events"], ["search_notes"],
                  ["add_reminder", "add_calendar_event", "get_upcoming"],
                  ["create_tool", "write_file"]):
        reachable |= set(group)
    for group in R._DOMAIN_WRITE_TOOLS.values():
        reachable |= set(group)
    # Missing aliases do not prove a tool is unreachable: retrieval also
    # indexes its name/description. Count only targets actually demonstrated
    # by a final route, not every incidental tool in the returned menu.
    for prompt, target in (
            ("search my chat history for an old project", "search_conversations"),
            ("clear my saved memories", "clear_memory")):
        offered = subset(prompt)
        check(f"{target} has a demonstrated final route", target in offered,
              f"{prompt!r} -> {offered}")
        if target in offered:
            reachable.add(target)
    read_tools = subset("show my saved memories")
    check("a memory read does not offer filesystem deletion",
          not any(REGISTRY[n].category == "fs_delete" for n in read_tools),
          f"-> {read_tools}")
    registered = {s["function"]["name"] for s in tool_schemas(None)}
    orphans = registered - reachable

    # A tool outside every regex subset is no longer stranded: since semantic
    # retrieval replaced the unscoped fallback, ANY registered tool can be
    # offered. So the invariant this test protects has changed shape — the
    # question is no longer "is it in a hand-written subset" but "can retrieval
    # actually find it". Aliases provide retrieval evidence for the remaining
    # orphans; the aliasless tools above instead require actual route proofs.
    #
    # Tools are added to the registry WITHOUT regex routes on purpose now (the
    # Capability Atlas rollout order): they surface through retrieval, and only
    # the ones measured to be hot or to miss get promoted to a hand-tuned route.
    # Asserting the old invariant would forbid exactly that.
    unreachable = {n for n in orphans
                   if n != "delete_path" and not REGISTRY[n].aliases}
    check("every tool has a scoped route, demonstrated route, or retrieval aliases",
          not unreachable, f"no demonstrated route or aliases: {sorted(unreachable)}")
    check("delete_path stays out of the regex subsets (destructive, deliberate)",
          "delete_path" in orphans)


def test_device_apps_and_web_are_scoped() -> None:
    print("\nthe three clusters that used to fall through are now scoped")
    for prompt, tool in (("what's the current volume?", "get_volume"),
                         ("how much battery do I have", "get_battery_status"),
                         ("turn off my wifi", "set_wifi"),
                         ("lock my screen", "lock_screen"),
                         ("what's on my clipboard", "clipboard_read"),
                         ("open safari", "open_app"),
                         ("open Safari", "open_app"),
                         ("quit spotify", "quit_app"),
                         ("play some music", "music"),
                         ("what's the weather in Tokyo", "web_fetch"),
                         ("what's the price of bitcoin", "web_fetch")):
        s = subset(prompt)
        # PROPORTIONAL, not a fixed number — a fixed ceiling needed manual
        # bumps (13 -> 20) every time the Capability Atlas rollout legitimately
        # grew _SYSTEM_CONTROL_TOOLS/_APPS_MEDIA_TOOLS (their own broad regexes
        # already claim these tools' phrasings, see router.py's comments on
        # both), which is exactly the kind of test-maintenance tax that gets
        # silently skipped under time pressure. What this actually asserts —
        # "still meaningfully less than the full registry, never silently
        # unscoped" — scales with the registry on its own if stated as a
        # fraction instead of a constant.
        ceiling = max(15, len(REGISTRY) // 4)
        check(f"{prompt!r} -> scoped, offers {tool}",
              bool(s) and tool in s and len(s) <= ceiling,
              f"-> {len(s)} tools (ceiling {ceiling}): {s or 'UNSCOPED'}")


def test_ambiguous_gets_core_not_everything() -> None:
    print("\nthe ambiguous fallback gets the core set, not the registry")
    registered = {s["function"]["name"] for s in tool_schemas(None)}
    for prompt in ("tell me a joke", "what?", "can you help me with something",
                   "anything from earlier today?"):
        s = subset(prompt)
        check(f"{prompt!r} is scoped", bool(s), "-> UNSCOPED")
        check(f"{prompt!r} is much smaller than the registry",
              len(s) < len(registered) / 2, f"{len(s)} of {len(registered)}")
    # run_shell is the deliberate escape hatch; destructive tools are not here.
    core = subset("tell me a joke")
    check("run_shell is offered as the escape hatch", "run_shell" in core)
    for banned in ("delete_path", "send_email", "send_message", "create_tool"):
        check(f"{banned} is NOT in the core set", banned not in core)


def test_core_includes_installed_skill_tools() -> None:
    print("\ninstalled skill tools stay callable from the fallback")
    from service.tools.registry import REGISTRY
    skill_tools = {n for n, t in REGISTRY.items() if t.category == "skill_tool"}
    if not skill_tools:
        check("(no skills installed — nothing to check)", True)
        return
    core = set(R._core_tools())
    check(f"all {len(skill_tools)} skill tools are in the core set",
          skill_tools <= core, f"missing={sorted(skill_tools - core)}")
    check("use_skill is offered too", "use_skill" in core)


def test_data_nouns_keep_the_full_toolset() -> None:
    print("\na genuinely two-way request keeps the whole toolset")
    # "open my calendar" could mean open_app OR get_upcoming; the model should
    # still get to choose, so the device/apps rules must decline it.
    for prompt in ("open my calendar", "open my email"):
        d = dec(prompt)
        check(f"{prompt!r} is not narrowed to apps/media",
              d.tool_subset is None or "get_upcoming" in (d.tool_subset or [])
              or "summarize_emails" in (d.tool_subset or []),
              f"-> {d.tool_subset}")
    # and a FILE gets the document tools rather than only app control.
    #
    # This assertion used to pass VACUOUSLY: the route was unscoped, so
    # `subset()` returned [] and "open_app" not in [] was trivially true — the
    # thing it claims to check was never actually exercised. Now that the route
    # retrieves a real subset, check the property that was always meant: the
    # tools that can act on a FILE are present. open_app may also be retrieved
    # ("open" is genuinely ambiguous) and that is fine, as long as it is not the
    # only option on the table.
    s = subset("open the pdf in my downloads")
    check("'open the pdf…' can reach the file tools",
          bool({"read_file", "find_files", "list_dir"} & set(s)),
          f"-> {s or 'UNSCOPED'}")


def test_light_read_voice_is_only_for_personal_data() -> None:
    print("\nthe warm read-out voice is only used for personal-data reads")
    for prompt in ("what's on my calendar", "summarize my messages",
                   "what's in my notes"):
        check(f"{prompt!r} keeps the warm voice", dec(prompt).light_read,
              "light_read=False")
    for prompt in ("what's the current volume?", "how much battery do I have",
                   "play some music", "what's the weather in Tokyo",
                   "tell me a joke", "what?"):
        check(f"{prompt!r} does NOT get the warm voice",
              not dec(prompt).light_read, "light_read=True")



def test_confirmation_inherits_the_previous_domain() -> None:
    print("\na bare 'yes' inherits the previous turn's domain, not the registry")
    # Verified failure 2026-08-09: "yes archive them", right after Wisp listed
    # emails, was routed with NO subset — all 55 tools, the configuration this
    # project measured as worst at selection. It called view_emails again and
    # never archive_email, on the one turn where the user had already said
    # "do it".
    d = asyncio.run(route("yes archive them",
                          last_assistant="Would you like me to archive these emails to keep your inbox clean?",
                          last_tools="view_emails,summarize_emails"))
    sub = d.tool_subset or []
    check("scoped rather than the full registry", bool(sub) and len(sub) < 20,
          f"-> {len(sub) if sub else 'UNSCOPED'}")
    check("archive_email is actually offered", "archive_email" in sub, f"-> {sub}")
    check("it does not force a tool call", d.expect_tool_first is False)

    d = asyncio.run(route("go ahead", last_assistant="Want me to send that text to Mom?",
                          last_tools="view_messages,lookup_contact"))
    check("a messages confirmation offers send_message",
          "send_message" in (d.tool_subset or []), f"-> {d.tool_subset}")

    d = asyncio.run(route("yes please", last_assistant="Should I add that to your calendar?",
                          last_tools="get_upcoming"))
    check("a calendar confirmation offers add_calendar_event",
          "add_calendar_event" in (d.tool_subset or []), f"-> {d.tool_subset}")

    # Nothing to INHERIT — but "the full toolset" stopped being the honest
    # answer once the registry grew: it is ~15,000 tokens at 78 tools and the
    # configuration measured worst at selection. The confirmation now retrieves
    # against the ASSISTANT'S OFFER, which describes the action in full where
    # the user's "do it" describes nothing. So the assertion is no longer "is it
    # unscoped" but "did it find the tool the offer implies".
    d = asyncio.run(route("do it", last_assistant="Shall I run it?\n```bash\nls\n```",
                          last_tools=None))
    check("no previous tools -> retrieves against the offer",
          "run_shell" in (d.tool_subset or []),
          f"-> {d.tool_subset}")



def test_update_someone_is_a_send() -> None:
    print("\n'give my mom an update' is a SEND, not a lookup")
    # Verified failure 2026-08-09: "give an update to my mom via messages about
    # what I did this month" contained none of send/reply/draft, so it routed
    # read-only and send_message was never offered. The model was asked to send
    # and structurally could not.
    # Split by whether the user NAMED a channel, because the two now have
    # different (and both correct) outcomes. The 2026-08-09 bug being guarded
    # here was that these weren't recognized as SENDS AT ALL — they routed
    # read-only with no write intent. That still must not happen. What changed
    # on 2026-08-10 is that an unnamed channel no longer picks one silently
    # (see test_ambiguous_channel_withholds_every_outbound_tool).
    print("  channel NAMED -> can send straight away")
    for prompt in ("give an update to my mom via messages about what I did this month",
                   "give my mom an update via messages"):
        s = subset(prompt)
        check(f"{prompt[:44]!r} can send",
              "send_message" in s or "send_email" in s, f"-> {s or 'UNSCOPED'}")

    print("  channel NOT named -> recognized as a send, but asks which one first")
    for prompt in ("let dad know I'll be late",
                   "tell Mom that I'm on my way",
                   "update my mom on my week",
                   "give Trishe a heads up about tomorrow"):
        d = dec(prompt)
        offered = set(d.tool_subset or [])
        check(f"{prompt[:44]!r} is recognized as a send", d.clarify_channel,
              f"reason={d.reason}")
        check(f"{prompt[:44]!r} can resolve the recipient",
              "lookup_contact" in offered, f"-> {sorted(offered)}")
        check(f"{prompt[:44]!r} cannot commit to a channel yet",
              not (offered & R._CHANNEL_OUTBOUND_TOOLS),
              f"-> {sorted(offered & R._CHANNEL_OUTBOUND_TOOLS)}")

    print("  …but talking to WISP about the user is still a read")
    for prompt in ("tell me what's on my calendar", "give me an update on my inbox",
                   "tell me about myself", "summarize my messages"):
        s = subset(prompt)
        check(f"{prompt[:44]!r} does not arm a send",
              "send_message" not in s and "send_email" not in s, f"-> {s}")


def test_multi_round_flags_the_sequential_routes() -> None:
    print("\nmulti_round is set on routes whose tools are sequential, not alternatives")
    # These four MUST carry multi_round=True — it's what stops the agent loop's
    # "broad" narration mode from treating one source answering as "done" on a
    # route that genuinely needs several. See router.RouteDecision.multi_round
    # and agent/loop.run_agent's multi_round parameter.
    for prompt in ("what do I need to do", "help me plan my day"):
        d = dec(prompt)
        check(f"{prompt!r} (aggregate to-do) is multi_round", d.multi_round,
              f"subset={d.tool_subset}")
        check(f"{prompt!r} offers all 4 sources",
              set(d.tool_subset or []) >= set(R._ALL_SOURCES), f"-> {d.tool_subset}")
    d = dec("summarize the PDF in my downloads")
    check("document route (list_dir THEN read_file) is multi_round", d.multi_round,
          f"subset={d.tool_subset}")
    d = dec("what did I have last week and what's next")
    check("a calendar+past-tense question is multi_round", d.multi_round,
          f"subset={d.tool_subset}")
    d = dec("tell me a joke")
    check("the ambiguous core fallback is multi_round (heterogeneous tools)",
          d.multi_round, f"subset={d.tool_subset}")


def test_narration_after_names_each_multi_round_route_requirement() -> None:
    print("\nnarration_after names what makes each multi_round route done")
    # multi_round alone is a whole-TURN veto on the narration gate, which is
    # stronger than the risk it guards. narration_after says which tools, once
    # answered, mean the requirement is met. See RouteDecision.narration_after.
    ALL4 = set(R._ALL_SOURCES)
    for prompt in ("what do I need to do", "help me plan my day"):
        d = dec(prompt)
        check(f"{prompt!r} releases only after all 4 sources",
              set(d.narration_after) == ALL4, f"-> {sorted(d.narration_after)}")
    d = dec("summarize the PDF in my downloads")
    check("document route releases after read_file, not list_dir",
          set(d.narration_after) == {"read_file"}, f"-> {sorted(d.narration_after)}")
    d = dec("what did I have on my calendar last week")
    check("calendar+past releases only after BOTH directions",
          set(d.narration_after) == {"get_upcoming", "get_past_events"},
          f"-> {sorted(d.narration_after)}")
    # The one multi_round route with no knowable required set must stay
    # conservative: empty means "never narrate", i.e. exactly today's behavior.
    d = dec("tell me a joke")
    check("the heterogeneous core fallback names NO required set (stays conservative)",
          not d.narration_after, f"-> {sorted(d.narration_after)}")

    print("  …and a non-multi_round route never needs one")
    for prompt in ("what's on my calendar", "summarize my messages",
                   "how much battery do I have"):
        d = dec(prompt)
        check(f"{prompt!r} has no narration_after", not d.narration_after,
              f"-> {sorted(d.narration_after)}")

    print("  …and every narration_after tool is actually IN its route's subset")
    # A required tool the route never offers could never be answered, which
    # would silently make narration_after unsatisfiable — the same bug class as
    # multi_round's blanket veto, just harder to see.
    for prompt in ("what do I need to do", "help me plan my day",
                   "summarize the PDF in my downloads",
                   "what did I have on my calendar last week"):
        d = dec(prompt)
        check(f"{prompt[:38]!r} requires only tools it offers",
              set(d.narration_after) <= set(d.tool_subset or []),
              f"required={sorted(d.narration_after)} offered={d.tool_subset}")

    print("  …but ordinary alternative-tool routes are NOT")
    for prompt in ("what's on my calendar", "summarize my messages",
                   "what's in my inbox", "how much battery do I have",
                   "what do you remember about me", "look up dan's contact",
                   "play some music", "what's the weather in tokyo"):
        d = dec(prompt)
        check(f"{prompt!r} is NOT multi_round", not d.multi_round,
              f"subset={d.tool_subset}")


def test_compose_widening_is_not_blocked_by_an_incidental_domain() -> None:
    print("\na send naming a recipient still gets send tools even when another "
          "domain noun (schedule/notes/etc.) matches too")
    # VERIFIED FAILURE 2026-08-10: "tell my mom about my schedule for this
    # week" matches _COMPOSE_RE (a send naming a recipient, no channel) AND
    # "schedule" trips the calendar domain. The calendar branch filled `subset`
    # first, so the old `if not subset and writing and _COMPOSE_RE...` gate —
    # keyed on subset being EMPTY — never fired, and send_message/send_email
    # were silently never offered. Real debug export: the model's own
    # reasoning said "I don't have a messaging tool", which was true given
    # what it was offered — it was not forgetting a capability, the router
    # never granted it.
    s = subset("I need you to tell my mom about my schedule for this week "
              "since we plan to go on a vacation")
    check("get_upcoming is still offered (the calendar half)",
          "get_upcoming" in s, f"-> {s}")
    # The send half is now expressed as "it reached the send ROUTE and can
    # resolve the recipient", not "send_message is on the table" — an unnamed
    # channel withholds the outbound tools until the user picks one. The bug
    # this guards is the route never gaining send intent at all, which is
    # exactly what clarify_channel records.
    d = dec("I need you to tell my mom about my schedule for this week "
           "since we plan to go on a vacation")
    check("it is recognized as a send (was: calendar-only, no send intent)",
          d.clarify_channel, f"reason={d.reason}")
    check("and can resolve the recipient",
          "lookup_contact" in (d.tool_subset or []), f"-> {d.tool_subset}")

    print("  …the same shape holds for other incidental-domain sends")
    for prompt in ("tell dad about my notes on the project",
                   "let mom know what's on my calendar today"):
        d = dec(prompt)
        check(f"{prompt!r} reaches the send route",
              d.clarify_channel, f"reason={d.reason}")
        check(f"{prompt!r} can resolve the recipient",
              "lookup_contact" in (d.tool_subset or []), f"-> {d.tool_subset}")


def test_clarify_channel_fires_only_when_genuinely_ambiguous() -> None:
    print("\nclarify_channel: ask which app, but only when neither was named")
    d = dec("I need you to tell my mom about my schedule for this week "
           "since we plan to go on a vacation")
    check("no channel named -> clarify_channel True", d.clarify_channel,
          f"reason={d.reason}")
    d = dec("send this to mom tonight")
    check("a send naming no channel -> clarify_channel True (pre-existing route)",
          d.clarify_channel, f"reason={d.reason}")

    print("  …but not when a channel WAS named")
    for prompt in ("email mom about my schedule this week",
                   "text mom about my schedule this week",
                   "email and text mom about my schedule"):
        d = dec(prompt)
        check(f"{prompt!r} -> clarify_channel False (channel is explicit)",
              not d.clarify_channel, f"reason={d.reason}")

    print("  …and not on routes with no send intent at all")
    for prompt in ("what is on my calendar this week",
                   "add a dentist appointment tomorrow at 3pm",
                   "summarize my messages", "what's in my inbox"):
        d = dec(prompt)
        check(f"{prompt!r} -> clarify_channel False (no send)",
              not d.clarify_channel, f"reason={d.reason}")


def test_ambiguous_channel_withholds_every_outbound_tool() -> None:
    print("\nchannel unknown -> the send/draft tools are not on the table at all")
    # VERIFIED FAILURE 2026-08-10, the most serious bug found so far. Asked to
    # "send an update to my mom" with no channel named, clarify_channel was set
    # and the system prompt told the model to ask first — and it called
    # draft_message anyway, without asking. draft_message then typed the whole
    # message into an UNRELATED GROUP CHAT the user's mother is not even in
    # (the Messages targeting had silently failed, see OutboundSender.swift),
    # and reported success naming a recipient it never reached.
    #
    # A prompt is not a constraint on a 4B model; the offered toolset is. So
    # the channel-committing tools are withheld until the user names a channel.
    for prompt in ("send an update to my mom with my schedule for this week",
                   "I need you to tell my mom about my schedule for this week",
                   "send this to mom tonight"):
        d = dec(prompt)
        offered = set(d.tool_subset or [])
        check(f"{prompt[:42]!r} offers NO outbound tool",
              not (offered & R._CHANNEL_OUTBOUND_TOOLS),
              f"-> {sorted(offered & R._CHANNEL_OUTBOUND_TOOLS)}")
        check(f"{prompt[:42]!r} still keeps its source and contact lookup",
              ("lookup_contact" in offered
               and ("get_upcoming" in offered or len(offered) >= 4)),
              f"-> {sorted(offered)}")

    print("  …but naming a channel outright still arms that channel immediately")
    s_txt = subset("text mom my schedule this week")
    check("'text mom …' can send_message", "send_message" in s_txt, f"-> {s_txt}")
    s_eml = subset("email mom my schedule this week")
    check("'email mom …' can send_email", "send_email" in s_eml, f"-> {s_eml}")


def test_answering_the_channel_question_restores_the_right_tools() -> None:
    print("\nanswering 'text or email?' arms exactly that channel")
    # Without this the fix above would be a dead end: a bare "text"/"email"
    # routes to a read-only 2-tool lookup, so answering the question Wisp had
    # just asked left it unable to act on the answer. Verified on all of these.
    ASKED = ("Would you like me to send this to your mom via text message "
             "or email? Which would you prefer?")
    LT = "get_upcoming,view_messages,summarize_messages"
    for prompt in ("text", "text her", "imessage", "send it as a text", "message her"):
        d = asyncio.run(route(prompt, last_assistant=ASKED, last_tools=LT))
        offered = set(d.tool_subset or [])
        check(f"{prompt!r} -> send_message armed", "send_message" in offered, f"-> {sorted(offered)}")
        check(f"{prompt!r} -> does NOT arm email", "send_email" not in offered, f"-> {sorted(offered)}")
    for prompt in ("email", "via email", "mail"):
        d = asyncio.run(route(prompt, last_assistant=ASKED, last_tools=LT))
        offered = set(d.tool_subset or [])
        check(f"{prompt!r} -> send_email armed", "send_email" in offered, f"-> {sorted(offered)}")
        check(f"{prompt!r} -> does NOT arm messages", "send_message" not in offered, f"-> {sorted(offered)}")

    print("  …but only when Wisp actually ASKED — otherwise it's a plain read")
    for prompt in ("email", "text"):
        d = dec(prompt)
        offered = set(d.tool_subset or [])
        check(f"bare {prompt!r} with no question before it stays read-only",
              not (offered & R._CHANNEL_OUTBOUND_TOOLS), f"-> {sorted(offered)}")
    # A previous assistant turn that mentions only ONE channel isn't the
    # question, so its answer must not be treated as one.
    d = asyncio.run(route("email", last_assistant="Want me to check your inbox?",
                          last_tools="summarize_emails"))
    check("a one-channel previous turn doesn't count as the question",
          not (set(d.tool_subset or []) & R._CHANNEL_OUTBOUND_TOOLS), f"-> {d.reason}")


def test_topic_lookup_forces_get_upcoming_before_a_compose_guesses() -> None:
    print("\nunresolved topic in a compose sentence -> get_upcoming forced first")
    # VERIFIED FAILURE 2026-08-24 (two of the user's debug exports, same day).
    # "send mom a message reminder her about my move in date" got get_upcoming
    # into its subset ONLY because "reminder" (a typo for "remind") happens to
    # match _CALENDAR_NOUN_RE — an accident, not real domain detection. The
    # retry, phrased naturally with no typo ("send a message to mom with my
    # college move in date"), matched no calendar/notes noun at all and never
    # offered get_upcoming — the model settled for "I don't have that date"
    # and asked, even AFTER a system-prompt rule told it to search first
    # (tool_digest still showed only lookup_contact — a prompt can't make the
    # model call a tool that was never offered).
    for prompt in ("send a message to mom with my college move in date",
                   "I need you to send mom a message reminder her about my move in date"):
        d = dec(prompt)
        offered = set(d.tool_subset or [])
        check(f"{prompt[:46]!r} offers get_upcoming", "get_upcoming" in offered,
              f"-> {sorted(offered)}")
        check(f"{prompt[:46]!r} offers search_notes", "search_notes" in offered,
              f"-> {sorted(offered)}")
        check(f"{prompt[:46]!r} forces get_upcoming first",
              d.force_first_tool == "get_upcoming", f"-> {d.force_first_tool}")
        check(f"{prompt[:46]!r} keeps its send tool reachable",
              bool(offered & {"send_message", "send_email"}), f"-> {sorted(offered)}")

    # A generic topic should be grounded too, but a project is not a calendar
    # payload.  Search personal notes/conversations rather than inventing a
    # reason to query the calendar.
    prompt = "email my boss about the project I have been working on"
    d = dec(prompt)
    offered = set(d.tool_subset or [])
    check("generic project topic offers search_notes", "search_notes" in offered,
          f"-> {sorted(offered)}")
    check("generic project topic offers conversations",
          "search_conversations" in offered, f"-> {sorted(offered)}")
    check("generic project topic forces a grounding source first",
          d.force_first_tool == "search_notes", f"-> {d.force_first_tool}")
    check("generic project topic keeps email reachable",
          "send_email" in offered, f"-> {sorted(offered)}")

    print("  …but a message with nothing to look up is untouched")
    for prompt in ("tell her I will be late", "text mom happy birthday",
                   "send Dan a thumbs up"):
        d = dec(prompt)
        check(f"{prompt!r} does not force get_upcoming",
              d.force_first_tool != "get_upcoming", f"-> {d.force_first_tool}")


if __name__ == "__main__":
    test_every_tool_has_a_scoped_home()
    test_device_apps_and_web_are_scoped()
    test_ambiguous_gets_core_not_everything()
    test_core_includes_installed_skill_tools()
    test_data_nouns_keep_the_full_toolset()
    test_light_read_voice_is_only_for_personal_data()
    test_confirmation_inherits_the_previous_domain()
    test_update_someone_is_a_send()
    test_multi_round_flags_the_sequential_routes()
    test_narration_after_names_each_multi_round_route_requirement()
    test_compose_widening_is_not_blocked_by_an_incidental_domain()
    test_clarify_channel_fires_only_when_genuinely_ambiguous()
    test_ambiguous_channel_withholds_every_outbound_tool()
    test_answering_the_channel_question_restores_the_right_tools()
    test_topic_lookup_forces_get_upcoming_before_a_compose_guesses()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
