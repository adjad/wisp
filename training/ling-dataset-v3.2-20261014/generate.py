#!/usr/bin/env python3
"""Ling V3.2 dataset generator: migrate every V3.1 training conversation to the CURRENT Wisp tool registry and render
~half of them with terse tool schemas. Deterministic, standard library only, seed 20261014.

V3.2 authors no new scenarios. It replays each source conversation (V3.1 targeted fixes, V3 replay sample; V2 replay
rows have no tools and pass through unchanged) through the host's model-led protocol as it exists on origin/main:

  * tool names/arguments/results are rewritten by the rules in MIGRATIONS (merged tool -> survivor + behaviour
    argument; retired tool -> an honest current tool, or the conversation is dropped and counted);
  * every round's offered tools, discovery receipts, the scoped system prompt (loop.build_system), the identity and
    installed-skills blocks, and the capability catalog are rebuilt from assets/ (captured from Wisp source by
    capture_assets.py), so no retired tool name survives anywhere in a row;
  * the offer order matches loop.model_led_offer: loaded real schemas, then name-only stubs (sorted), then
    get_tool_schemas LAST (V3/V3.1 put get_tool_schemas before the stubs);
  * ~50% of tool-using conversations per category (seeded, stratified) get schema_style "terse": every REAL schema in
    `tools` uses the one-line description from assets/short_descriptions.json and has parameter descriptions stripped
    exactly like registry._strip_parameter_descriptions (a property NAMED "description" is kept).

    python3 sources.py && python3 generate.py      # writes train/dev.jsonl, provenance, conversations, stats, card

Re-rendering after a new short-description export is one command (see README): re-run capture_assets.py (or replace
assets/short_descriptions.json) and then `python3 generate.py`.
"""
import argparse, copy, hashlib, json, random, re, statistics, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
CACHE = HERE / "source_cache"
V31 = HERE.parent / "ling-dataset-v3.1-20261013"
SEED = 20261014                 # V3.2 decisions (terse/full assignment). Source conversations keep their V3.1/V3 seeds.
VERSION = "v3.2-20261014"
DISCOVERY = "get_tool_schemas"
MAX_DISCOVERIES, MAX_SCHEMAS = 2, 24

TOOLS = json.loads((ASSETS / "tools.json").read_text())
SP = json.loads((ASSETS / "system_parts.json").read_text())
SHORT = json.loads((ASSETS / "short_descriptions.json").read_text())
REG = {s["function"]["name"]: s for s in TOOLS["full_registry_schemas"]}
FAMILY = TOOLS["tool_family"]
CATALOG = {"web": TOOLS["catalog_families"], "personal_no_web": TOOLS["catalog_families_noweb"]}
CATALOG_TOOLS = {v: sorted(t for ts in c.values() for t in ts) for v, c in CATALOG.items()}
DISC_SCHEMA = {"web": TOOLS["discovery_schema_step0"][0], "personal_no_web": TOOLS["discovery_schema_noweb"][0]}
GUARD_RE = re.compile(r"^\((?P<name>[a-z_]+) already ran with these exact arguments this turn")
OLD_REG = json.loads((V31 / "assets/tools.json").read_text())
OLD_NAMES = {s["function"]["name"] for s in OLD_REG["full_registry_schemas"]} | {DISCOVERY}
RETIRED_NAMES = sorted(OLD_NAMES - set(REG) - {DISCOVERY})


# ----------------------------------------------------------------------------- prompt builders (mirror Wisp source)

def catalog_prompt(variant):
    cat = CATALOG[variant]
    lines = list(SP["catalog"]["preamble_lines"]) + [SP["catalog"]["capabilities_header"]]
    for fam in SP["catalog"]["family_order"]:
        if cat.get(fam):
            lines.append(f"{fam}: {SP['catalog']['descriptions'][fam]}. Tools: {', '.join(cat[fam])}")
    return "\n".join(lines)


assert catalog_prompt("web") == SP["catalog_prompt_full"], "catalog builder drifted from the captured CapabilityCatalog.prompt()"


def build_system(offered):
    """loop.build_system(offered, planner=False): cross-cutting blocks always, tool blocks when one of their tools is offered."""
    return "\n".join(b["text"] for b in SP["system_blocks"] if not b["named"] or set(b["named"]) & set(offered))


def skills_block(user_text):
    """service.skills.skills_context_block(user_text, '', catalog=True) for the bundled skills (no user skills)."""
    sk = SP["skills_block"]
    low = user_text.lower()
    matched = [s for s in sk["skills"] if f"@{s['name'].lower()}" in low or f"${s['name'].lower()}" in low
               or any(t.lower().strip() in low for t in s["triggers"] if t.strip())]
    names = {s["name"] for s in matched}
    out = [sk["header"]]
    for s in sk["skills"]:
        out.append(f"- {s['name']}: {s['description'] or '(no description)'}" + ("  [loaded below]" if s["name"] in names else ""))
    for s in matched:
        out.append(f"\n--- Skill: {s['name']} ---\nThe user set up this procedure for requests like this one. Follow it:\n{s['body']}")
    return "\n".join(out)


def sys_messages(loaded, now_sentence, variant, user_text):
    return [{"role": "system", "content": build_system(set(loaded)) + SP["identity_block"] + skills_block(user_text)},
            {"role": "system", "content": "\n" + now_sentence + "\n" + catalog_prompt(variant)},
            {"role": "system", "content": SP["context_note"]}]


def strip_parameter_descriptions(node):
    """Exact mirror of service/tools/registry.py::_strip_parameter_descriptions (terse-tool-schemas branch)."""
    if isinstance(node, list):
        return [strip_parameter_descriptions(v) for v in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if key == "description" and isinstance(value, str):
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: strip_parameter_descriptions(sub) for name, sub in value.items()}
        else:
            out[key] = strip_parameter_descriptions(value)
    return out


def real_schema(name, style):
    full = REG[name]
    if style != "terse":
        return copy.deepcopy(full)
    f = full["function"]
    # registry.model_schema: short_description(name, full) -> SHORT.get(name, full); skill tools have no entry.
    return {"type": "function", "function": {"name": name, "description": SHORT["tools"].get(name, f["description"]),
                                             "parameters": strip_parameter_descriptions(f["parameters"])}}


def stub_schema(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def offered(loaded, attempts, variant):
    names = list(loaded)
    if attempts < MAX_DISCOVERIES:
        names += [t for t in CATALOG_TOOLS[variant] if t not in loaded] + [DISCOVERY]
    return names


def render_tools(loaded, attempts, variant, style):
    out = []
    for n in offered(loaded, attempts, variant):
        if n == DISCOVERY:
            out.append(copy.deepcopy(DISC_SCHEMA[variant]))
        elif n in loaded:
            out.append(real_schema(n, style))
        else:
            out.append(stub_schema(n))
    return out


def receipt(families, names):
    return json.dumps({"schema_discovery": "ready_next_step", "families": list(families), "available_tools": list(names),
                       "source_tools_executed": False})


def rejection(error):
    return f"Schema selection rejected: {error}. No source was read or action performed."


# ----------------------------------------------------------------------------- migration rules

class Drop(Exception):
    """The conversation cannot be migrated honestly; it is dropped and counted under this reason."""


def _web_rewrite(kind):
    """Retired lookups whose honest successor is web_search: query from the old arguments, result in Wisp's web_search
    listing shape (synthetic example.com domains, as V3.1's web rows use), old figures kept as the top snippet."""
    def conv(args, result=None):
        if kind == "currency":
            q = f"{args['amount']} {args['from_currency']} to {args['to_currency']}"
            dom, slug = "rates.example.com", f"{args['from_currency']}-{args['to_currency']}".lower()
            t1, t2, s2 = (f"{args['from_currency']} to {args['to_currency']} exchange rate", "Currency converter",
                          "Convert between 160 currencies.")
        elif kind == "sun":
            loc = args["location"]
            city = loc.split(",")[0]
            q, dom, slug = f"sunset time {city} today", "suntimes.example.com", city.lower().replace(" ", "-")
            t1, t2, s2 = f"Sunrise and sunset in {city}", "Moon phases", "Moonrise, moonset and phase calendar."
        else:
            dish = args["dish"]
            q, dom, slug = f"{dish} recipe", "recipes.example.com", dish.lower().replace(" ", "-")
            t1, t2, s2 = f"Easy {dish}", f"{dish.capitalize()} variations", "Ten twists on the classic."
        new_args = {"query": q}
        if result is None:
            return new_args, None, dom
        res = (f"1. {t1}\n   https://{dom}/{slug}\n   {result}\n2. {t2}\n   https://{dom}/more\n   {s2}")
        return new_args, res, dom
    return conv


def _final_cites(domain):
    def f(text):
        t = text.rstrip()
        first, dot, rest = t.partition(". ")
        return (first.rstrip(".") + f", per {domain}." + ((" " + rest) if rest else "")) if dot else t.rstrip(".") + f", per {domain}."
    return f


# rule: old -> (new tool, argument converter(args)->new args, rule id). A converter may raise Drop.
def _kw(**fixed):
    return lambda a: {**fixed, **a}


def _rename(mapping, **fixed):
    return lambda a: {**fixed, **{mapping.get(k, k): v for k, v in a.items()}}


def _mark_read(a):
    out = {"message_id": a["message_id"]} if "message_id" in a else {}
    out["action"] = "mark_read" if a.get("read", True) else "mark_unread"
    return out


def _flag(a):
    out = {"message_id": a["message_id"]} if "message_id" in a else {}
    out["action"] = "flag" if a.get("flagged", a.get("flag", True)) else "unflag"
    return out


def _weather(include):
    def f(a):
        out = {k: v for k, v in a.items() if k in ("location", "period")}
        out["include"] = [include]
        return out
    return f


def _media(a):
    act = {"resume": "play", "skip": "next", "back": "previous", "prev": "previous"}.get(a.get("action"), a.get("action"))
    if act not in ("play", "pause", "next", "previous"):
        raise Drop("mac_media cannot do this playback request")
    return {"action": act}


def _trash(a):
    path = a.get("path") or a.get("source")
    if not path:
        raise Drop("delete without a path")
    return {"source": path, "destination": "~/.Trash/" + path.rstrip("/").rsplit("/", 1)[-1]}


MIGRATIONS = {
    # PR #208 merges (service/safety/grants.py _MERGED_SCOPES/_RETIRED_INTO, tests/test_tool_consolidation.py)
    "set_alarm": ("set_timer", _kw(kind="alarm"), "merged:set_alarm->set_timer(kind=alarm)"),
    "set_sleep_timer": ("set_timer", _kw(kind="sleep"), "merged:set_sleep_timer->set_timer(kind=sleep)"),
    "list_timers": ("manage_timers", lambda a: {"action": "list"}, "merged:list_timers->manage_timers(action=list)"),
    "cancel_timer": ("manage_timers", lambda a: {"action": "cancel", **({"label": a.get("label") or a.get("id")} if (a.get("label") or a.get("id")) else {})},
                     "merged:cancel_timer->manage_timers(action=cancel)"),
    "stopwatch": ("manage_timers", lambda a: {"action": "stopwatch_" + a.get("action", "read")}, "merged:stopwatch->manage_timers(action=stopwatch_*)"),
    "get_battery_status": ("system_status", lambda a: {"section": "battery"}, "merged:get_battery_status->system_status(section=battery)"),
    "network_info": ("system_status", lambda a: {"section": "network"}, "merged:network_info->system_status(section=network)"),
    "list_bluetooth_devices": ("system_status", lambda a: {"section": "bluetooth"}, "merged:list_bluetooth_devices->system_status(section=bluetooth)"),
    "set_wifi": ("toggle_setting", lambda a: {"setting": "wifi", **({"on": a.get("on", a.get("enabled"))} if ("on" in a or "enabled" in a) else {})},
                 "merged:set_wifi->toggle_setting(setting=wifi)"),
    "archive_email": ("email_triage", _kw(action="archive"), "merged:archive_email->email_triage(action=archive)"),
    "mark_email_read": ("email_triage", _mark_read, "merged:mark_email_read->email_triage(action=mark_read or mark_unread)"),
    "flag_email": ("email_triage", _flag, "merged:flag_email->email_triage(action=flag or unflag)"),
    "weather_alerts": ("get_weather", _weather("alerts"), "merged:weather_alerts->get_weather(include=[alerts])"),
    "air_quality": ("get_weather", _weather("air_quality"), "merged:air_quality->get_weather(include=[air_quality])"),
    "rain_radar": ("get_weather", _weather("rain_next_hours"), "merged:rain_radar->get_weather(include=[rain_next_hours])"),
    "wisp_sync": ("wisp_status", lambda a: {"topic": "sync"}, "merged:wisp_sync->wisp_status(topic=sync)"),
    "switch_app": ("open_app", _rename({"app": "name"}), "merged:switch_app->open_app"),
    # PR #224 removals that moved to the mac-controls skill
    "get_volume": ("mac_get_volume", lambda a: {}, "skill:get_volume->mac_get_volume"),
    "set_volume": ("mac_set_volume", lambda a: {"level": a["level"]} if "level" in a else {}, "skill:set_volume->mac_set_volume"),
    "lock_screen": ("mac_lock_screen", lambda a: {}, "skill:lock_screen->mac_lock_screen"),
    "music": ("mac_media", _media, "skill:music->mac_media"),
    "spotify": ("mac_media", lambda a: {**_media(a), "app": "spotify"}, "skill:spotify->mac_media(app=spotify)"),
    # no delete tool any more: an explicit trash/delete of one named file becomes a move to the Trash
    "trash_file": ("move_path", _trash, "no_delete:trash_file->move_path(~/.Trash)"),
    "delete_path": ("move_path", _trash, "no_delete:delete_path->move_path(~/.Trash)"),
    "grocery_note_evidence": ("search_notes", lambda a: {"query": "grocery"}, "removed:grocery_note_evidence->search_notes"),
    "grocery_reminder_evidence": ("search_reminders", lambda a: {"query": "grocery"}, "removed:grocery_reminder_evidence->search_reminders"),
}
# retired outright, rewritten to web_search with a matching result (honest fit; needs the web catalog variant)
WEB_REWRITES = {"convert_currency": "currency", "astronomy": "sun", "recipe_lookup": "recipe"}
# retired with no honest successor: the conversation is dropped
NO_SUCCESSOR = {"airdrop_file": "AirDrop was retired with no replacement (no attachment-capable send tool)",
                "place_call": "phone calls were retired with no replacement"}
TRASH_CUE = re.compile(r"\b(trash|delete|throw (?:it |that |this )?away|get rid of|bin)\b", re.I)


def generic_map(old):
    """Name mapping for tools that only appear as loaded decoys/preloads (never called): merged -> survivor, else None."""
    if old in REG:
        return old
    if old in MIGRATIONS:
        return MIGRATIONS[old][0]
    return None


# ----------------------------------------------------------------------------- source loading

def read_jsonl(p):
    return [json.loads(x) for x in Path(p).read_text().splitlines() if x]


def jdump(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def group_rows(rows):
    convs = {}
    for r in rows:
        convs.setdefault(r["conversation_id"], []).append(r)
    return convs


def _strip_flag(m):
    return {k: v for k, v in m.items() if k != "training"}


# ----------------------------------------------------------------------------- one conversation

def migrate_conversation(rows, prov, style, source):
    """-> (new_rows_payload, new_conversation, info) or raises Drop."""
    full = rows[-1]["messages"]
    for r in rows:
        assert [_strip_flag(m) for m in r["messages"][1:-1]] == [_strip_flag(m) for m in full[1:len(r["messages"]) - 1]], r["id"]
    target_idx = {len(r["messages"]) - 1 for r in rows}
    variant = prov["catalog_variant"]
    now_sentence = full[1]["content"].split("\n")[1]
    assert now_sentence.startswith("The current date and time is "), now_sentence
    last_user = max(i for i, m in enumerate(full) if m["role"] == "user")
    prefix = [dict(m) for m in full[3:last_user + 1]]
    user_text = full[last_user]["content"]
    turn = full[last_user + 1:]
    cat_tools = set(CATALOG_TOOLS[variant])

    # conversation-specific name map for every tool that is CALLED
    called = []
    for m in turn:
        for tc in m.get("tool_calls") or []:
            if tc["function"]["name"] != DISCOVERY:
                called.append(tc["function"]["name"])
    name_map, rules = {}, {}
    for old in dict.fromkeys(called):
        if old in REG:
            name_map[old] = old
        elif old in NO_SUCCESSOR:
            raise Drop(f"{old}: {NO_SUCCESSOR[old]}")
        elif old in WEB_REWRITES:
            if variant != "web":
                raise Drop(f"{old}: retired; web_search is the only honest successor but this turn has no web access")
            name_map[old] = "web_search"
            rules[old] = f"retired:{old}->web_search"
        elif old in MIGRATIONS:
            name_map[old] = MIGRATIONS[old][0]
            rules[old] = MIGRATIONS[old][2]
            if old in ("trash_file", "delete_path") and not TRASH_CUE.search(user_text):
                raise Drop(f"{old}: no explicit trash/delete request for a named file")
        else:
            raise Drop(f"{old}: retired with no successor")

    def nmap(old):
        return name_map[old] if old in name_map else generic_map(old)

    def map_list(names):
        out = []
        for n in names:
            m = nmap(n)
            if m and m in cat_tools and m not in out:
                out.append(m)
        return out

    migrations = []
    web_domain = None

    def map_call(old, args, result=None, stub=False):
        """-> (new_name, new_args, new_result)."""
        nonlocal web_domain
        new = name_map[old]
        if old == new:
            return new, args, result
        if old in WEB_REWRITES:
            conv = _web_rewrite(WEB_REWRITES[old])
            if stub and not args:
                return new, {}, result
            if stub:
                return new, {}, result
            na, nr, dom = conv(args, result)
            web_domain = dom
            return new, na, nr
        if stub and not args:
            return new, {}, result
        na = MIGRATIONS[old][1](dict(args))
        return new, na, result

    preload_old = list(prov.get("preload") or [])
    loaded = map_list(preload_old)
    preload_new = list(loaded)
    old_loaded = list(preload_old)
    attempts = 0
    out_turn = []            # new messages
    steps = []               # (index in out_turn, loaded snapshot, attempts snapshot, trainable)
    gold, modes = [], []
    i = 0
    while i < len(turn):
        m = turn[i]
        abs_idx = last_user + 1 + i
        trainable = abs_idx in target_idx
        if m["role"] != "assistant":
            raise AssertionError(f"unexpected {m['role']} in turn")
        tcs = m.get("tool_calls") or []
        results = turn[i + 1:i + 1 + len(tcs)]
        assert all(x["role"] == "tool" for x in results) and [x["tool_call_id"] for x in results] == [t["id"] for t in tcs]
        i += 1 + len(tcs)
        if not tcs:
            steps.append((len(out_turn), list(loaded), attempts, trainable))
            text = m["content"]
            if web_domain:
                text = _final_cites(web_domain)(text)
            out_turn.append({"role": "assistant", "content": text, "training": False})
            continue
        names = [t["function"]["name"] for t in tcs]
        kinds = {("disc" if n == DISCOVERY else "stub" if n not in old_loaded else "real") for n in names}
        assert len(kinds) == 1, (names, kinds)
        kind = kinds.pop()
        snap = (list(loaded), attempts)
        if kind == "disc":
            assert len(tcs) == 1
            a = tcs[0]["function"]["arguments"]
            res_old = results[0]["content"]
            was_rejected = res_old.startswith("Schema selection rejected")
            fams = [f for f in a["families"] if f in CATALOG[variant]]
            if "tools" in a:
                tl = map_list(a["tools"]) if not was_rejected else [nmap(t) or t for t in a["tools"]]
                tl = list(dict.fromkeys(tl))
                if not tl:
                    raise Drop("explicit discovery requested only retired tools")
                if not was_rejected:
                    for t in tl:
                        if FAMILY[t] not in fams:
                            fams.append(FAMILY[t])
                new_args = {"families": fams, "tools": tl}
            else:
                new_args = {"families": fams}
            if not fams or len(fams) > 3:
                raise Drop("explicit discovery families cannot be mapped")
            if attempts >= MAX_DISCOVERIES:
                raise Drop("discovery budget exhausted after migration")
            attempts += 1
            fam_names = {t for f in fams for t in CATALOG[variant][f]}
            req = new_args.get("tools") or sorted(fam_names)
            if not set(req) <= fam_names:
                text = rejection("Requested tools must belong to the selected admitted families")
                if not was_rejected:
                    raise Drop("explicit discovery became invalid after migration")
                modes.append("explicit")
                gold.append({"name": DISCOVERY, "arguments": new_args, "rejected": True})
            else:
                if was_rejected:
                    raise Drop("rejected discovery became valid after migration")
                names_new = list(dict.fromkeys([*loaded, *req]))
                if len(names_new) > MAX_SCHEMAS:
                    raise Drop("discovery exceeds 24 schemas after migration")
                loaded = names_new
                text = receipt(fams, names_new)
                modes.append("explicit")
                gold.append({"name": DISCOVERY, "arguments": new_args})
                old_loaded = json.loads(res_old)["available_tools"]
            if new_args != a:
                migrations.append({"step": "discovery", "migrated_from": a, "migrated_to": new_args,
                                   "migration_rule": "discovery_args_remapped"})
            steps.append((len(out_turn), *snap, trainable and not was_rejected))
            out_turn.append({"role": "assistant", "content": "", "training": False,
                             "tool_calls": [{"id": tcs[0]["id"], "type": "function",
                                             "function": {"name": DISCOVERY, "arguments": new_args}}]})
            out_turn.append({"role": "tool", "tool_call_id": tcs[0]["id"], "content": text})
            continue
        if kind == "stub":
            old_loaded = json.loads(results[0]["content"])["available_tools"]
            keep = []
            for tc in tcs:
                old = tc["function"]["name"]
                new, na, _ = map_call(old, tc["function"]["arguments"], stub=True)
                if new not in cat_tools:
                    raise Drop(f"stub {new} is not admitted in this catalog variant")
                if new in loaded or any(k[1] == new for k in keep):
                    continue
                keep.append((tc, new, na))
                if new != old:
                    migrations.append({"step": "stub", "migrated_from": {"name": old, "arguments": tc["function"]["arguments"]},
                                       "migrated_to": {"name": new, "arguments": na}, "migration_rule": rules[old]})
            if not keep:
                # every stubbed tool is already loaded after migration: the loading step disappears
                migrations.append({"step": "stub", "migrated_from": names, "migrated_to": None,
                                   "migration_rule": "stub_step_removed:tool_already_loaded"})
                continue
            if attempts >= MAX_DISCOVERIES:
                raise Drop("discovery budget exhausted after migration")
            attempts += 1
            new_names = [n for _, n, _ in keep]
            fams = list(dict.fromkeys(FAMILY[n] for n in new_names))
            names_new = list(dict.fromkeys([*loaded, *new_names]))
            if len(names_new) > MAX_SCHEMAS or len(fams) > 3:
                raise Drop("implicit discovery exceeds limits after migration")
            loaded = names_new
            text = receipt(fams, names_new)
            modes.append("stub")
            msg = {"role": "assistant", "content": "", "training": False, "tool_calls": []}
            res_msgs = []
            for tc, new, na in keep:
                msg["tool_calls"].append({"id": tc["id"], "type": "function", "function": {"name": new, "arguments": na}})
                res_msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": text})
                gold.append({"name": new, "arguments": na, "stub": True})
            steps.append((len(out_turn), *snap, trainable))
            out_turn.append(msg)
            out_turn.extend(res_msgs)
            continue
        # real calls
        msg = {"role": "assistant", "content": "", "training": False, "tool_calls": []}
        res_msgs = []
        guard_step = False
        for tc, res in zip(tcs, results):
            old = tc["function"]["name"]
            new, na, nr = map_call(old, tc["function"]["arguments"], res["content"])
            if new not in loaded:
                raise Drop(f"{new} called before its schema is loaded after migration")
            content = nr if nr is not None else res["content"]
            gm = GUARD_RE.match(res["content"])
            entry = {"name": new, "arguments": na}
            if gm:
                guard_step = True
                content = res["content"].replace(f"({old} already ran", f"({new} already ran", 1)
                entry["guard_rejected"] = True
            if new != old:
                migrations.append({"step": "call", "migrated_from": {"name": old, "arguments": tc["function"]["arguments"]},
                                   "migrated_to": {"name": new, "arguments": na}, "migration_rule": rules[old],
                                   "result_rewritten": content != res["content"]})
            msg["tool_calls"].append({"id": tc["id"], "type": "function", "function": {"name": new, "arguments": na}})
            res_msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": content})
            gold.append(entry)
        steps.append((len(out_turn), *snap, trainable and not guard_step))
        out_turn.append(msg)
        out_turn.extend(res_msgs)

    new_rows, conv_msgs, info = emit(steps, out_turn, prefix, loaded, now_sentence, variant, user_text, style,
                                     preload_new, gold, modes)
    info["migrations"] = migrations
    info["removed_preload"] = [t for t in preload_old if not (nmap(t) and nmap(t) in cat_tools)]
    return new_rows, conv_msgs, info


def emit(steps, out_turn, prefix, loaded, now_sentence, variant, user_text, style, preload, gold, modes):
    """Rows (one per trainable assistant step), the full conversation, and per-conversation info."""
    def sysmsgs(ld):
        return sys_messages(ld, now_sentence, variant, user_text)

    new_rows = []
    k = 0
    for idx, ld, att, ok in steps:
        if not ok:
            continue
        msgs = sysmsgs(ld) + copy.deepcopy(prefix) + [copy.deepcopy(x) for x in out_turn[:idx]]
        for x in msgs:
            if x["role"] == "assistant":
                x["training"] = False
        tgt = copy.deepcopy(out_turn[idx])
        tgt["training"] = True
        new_rows.append({"k": k, "tools": render_tools(ld, att, variant, style), "messages": msgs + [tgt]})
        k += 1
    trainable_idx = {idx for idx, _, _, ok in steps if ok}
    conv_msgs = sysmsgs(loaded) + copy.deepcopy(prefix)
    for j, x in enumerate(out_turn):
        x = copy.deepcopy(x)
        if x["role"] == "assistant":
            x["training"] = j in trainable_idx
        conv_msgs.append(x)
    first_kind = None
    for idx, _, _, _ in steps:
        msg = out_turn[idx]
        if msg.get("tool_calls"):
            n0 = msg["tool_calls"][0]["function"]["name"]
            first_kind = "real" if n0 != DISCOVERY and n0 in preload else "load"
            break
    variant_sp = "S" if not preload else ("SP-hit" if first_kind == "real" else "SP-decoy")
    info = {"gold_calls": gold, "discovery_modes": modes, "preload": list(preload), "protocol_variant": variant_sp,
            "migrations": [], "n_steps": len(new_rows),
            "tools_per_round": [{"real_schemas": ld, "get_tool_schemas": att < MAX_DISCOVERIES,
                                 "name_only_stubs": len([t for t in CATALOG_TOOLS[variant] if t not in ld]) if att < MAX_DISCOVERIES else 0}
                                for _, ld, att, _ in steps]}
    return new_rows, conv_msgs, info


class Builder:
    """Records one NEW conversation (V3.2-authored categories) directly in the current host protocol: the same state
    machine migrate_conversation replays (loaded schemas, two loading steps, receipts, stub implicit discovery)."""

    def __init__(self, rng, variant, preload=()):
        self.r, self.variant = rng, variant
        self.loaded = [t for t in CATALOG_TOOLS[variant] if t in set(preload)]
        self.preload = list(self.loaded)
        self.attempts = 0
        self.out_turn, self.steps, self.gold, self.modes = [], [], [], []
        self.seen = set()

    def _id(self):
        return "call_" + "".join(self.r.choice("0123456789abcdef") for _ in range(8))

    def _step(self, msg, trainable=True):
        self.steps.append((len(self.out_turn), list(self.loaded), self.attempts, trainable))
        self.out_turn.append(msg)

    def offered(self):
        return offered(self.loaded, self.attempts, self.variant)

    def stub(self, specs):
        """specs: [(tool, guess_args)]: an implicit discovery through name-only stubs."""
        new = [t for t, _ in specs]
        assert self.attempts < MAX_DISCOVERIES and all(t in CATALOG_TOOLS[self.variant] and t not in self.loaded for t in new)
        fams = list(dict.fromkeys(FAMILY[t] for t in new))
        names = list(dict.fromkeys([*self.loaded, *new]))
        rec = receipt(fams, names)
        msg = {"role": "assistant", "content": "", "training": False, "tool_calls": []}
        res = []
        for t, a in specs:
            cid = self._id()
            msg["tool_calls"].append({"id": cid, "type": "function", "function": {"name": t, "arguments": dict(a)}})
            res.append({"role": "tool", "tool_call_id": cid, "content": rec})
            self.gold.append({"name": t, "arguments": dict(a), "stub": True})
        self._step(msg)
        self.out_turn.extend(res)
        self.attempts += 1
        self.loaded = names
        self.modes.append("stub")

    def explicit(self, families, tools):
        assert self.attempts < MAX_DISCOVERIES
        args = {"families": list(families), "tools": list(tools)}
        allowed = {t for f in families for t in CATALOG[self.variant][f]}
        assert set(tools) <= allowed
        names = list(dict.fromkeys([*self.loaded, *tools]))
        cid = self._id()
        self._step({"role": "assistant", "content": "", "training": False, "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": DISCOVERY, "arguments": args}}]})
        self.out_turn.append({"role": "tool", "tool_call_id": cid, "content": receipt(families, names)})
        self.attempts += 1
        self.loaded = names
        self.modes.append("explicit")
        self.gold.append({"name": DISCOVERY, "arguments": args})

    def call(self, specs, trainable=True):
        """specs: [(tool, args, result)]. trainable=False records a context-only step (e.g. a bad call the host refused)."""
        msg = {"role": "assistant", "content": "", "training": False, "tool_calls": []}
        res = []
        for t, a, out in specs:
            assert t in self.loaded, (t, self.loaded)
            key = (t, json.dumps(a, sort_keys=True))
            assert key not in self.seen, key
            self.seen.add(key)
            cid = self._id()
            msg["tool_calls"].append({"id": cid, "type": "function", "function": {"name": t, "arguments": dict(a)}})
            res.append({"role": "tool", "tool_call_id": cid, "content": out})
            self.gold.append({"name": t, "arguments": dict(a), **({} if trainable else {"context_only": True})})
        self._step(msg, trainable)
        self.out_turn.extend(res)

    def final(self, text):
        self._step({"role": "assistant", "content": text, "training": False})

    def finish(self, prefix, now_sentence, user_text, style):
        return emit(self.steps, self.out_turn, prefix, self.loaded, now_sentence, self.variant, user_text, style,
                    self.preload, self.gold, self.modes)


# ----------------------------------------------------------------------------- helpers for authored categories

def rng_v32(*parts):
    return random.Random(int(hashlib.sha256("|".join(str(x) for x in (SEED,) + parts).encode()).hexdigest()[:16], 16))


def _norm_tokens(text):
    return re.findall(r"[a-z0-9']+", text.lower())


def _hsh(t):
    return hashlib.sha1(" ".join(t).encode()).hexdigest()[:12]


def heldout_overlap(held, text):
    """Max Jaccard of `text`'s n-gram shingles against the hashed eval prompts (same rule as V3.1)."""
    t = _norm_tokens(text)
    if not t:
        return 0.0
    full = _hsh(t)
    best = 0.0
    for it in held:
        if it["full"] == full:
            return 1.0
        m = min(len(t), it["ntok"])
        if m < 3:
            continue
        n = min(6, m)
        a = {_hsh(t[i:i + n]) for i in range(len(t) - n + 1)}
        b = set(it["sh"].get(str(n), []))
        if a and b:
            best = max(best, len(a & b) / len(a | b))
    return best


OVERLAP_LIMIT = 0.4
NEW_CATEGORIES = ["v32:v32_mac_controls"]


# ----------------------------------------------------------------------------- style assignment

def assign_styles(entries):
    """entries: [(split, category, conversation_id, uses_tools)] -> {cid: style}. ~50% terse per (split, category)."""
    by = {}
    for split, cat, cid, uses in entries:
        by.setdefault((split, cat), []).append(cid)
    styles = {}
    for (split, cat), cids in sorted(by.items()):
        cids = sorted(cids)
        rng = random.Random(f"{SEED}|terse|{split}|{cat}")
        rng.shuffle(cids)
        odd_up = int(hashlib.sha256(f"{SEED}|{split}|{cat}".encode()).hexdigest(), 16) & 1
        n_terse = (len(cids) + odd_up) // 2
        for j, cid in enumerate(cids):
            styles[cid] = "terse" if j < n_terse else "full"
    return styles


# ----------------------------------------------------------------------------- main

def new_cid(source, cid):
    return ("v32-" + cid[len("v31-"):]) if source == "v31_new" else ("v32r-" + cid)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", default=str(HERE))
    a = ap.parse_args()
    out = Path(a.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    if not (CACHE / "VERIFIED").exists():
        sys.exit("source_cache/ missing: run `python3 sources.py` first")
    v3r_prov = {p["source_conversation_id"]: p for p in read_jsonl(CACHE / "v3_replay.provenance.jsonl")}
    sources = []      # (split, source, rows_by_conv, prov_by_cid)
    for split in ("train", "dev"):
        new_rows = read_jsonl(CACHE / f"v31_new/{split}.jsonl")
        new_prov = {p["conversation_id"]: p for p in read_jsonl(CACHE / f"v31_new/{split}.provenance.jsonl")}
        sources.append((split, "v31_new", group_rows(new_rows), new_prov))
        v3_rows = read_jsonl(CACHE / f"v3_replay.{split}.jsonl")
        sources.append((split, "v3_replay", group_rows(v3_rows), {c: v3r_prov[c] for c in group_rows(v3_rows)}))
    entries = []
    for split, source, convs, prov in sources:
        for cid in convs:
            uses = any(r["tools"] for r in convs[cid])
            entries.append((split, ("v31:" if source == "v31_new" else "v3r:") + prov[cid]["category"], cid, uses))
    sys.path.insert(0, str(HERE))
    import ext_v32_mac as MAC
    held = json.loads((ASSETS / "heldout_shingles.json").read_text())["items"]
    mac = MAC.build(sys.modules[__name__], held, OVERLAP_LIMIT)
    counters = {"train": 0, "dev": 0}
    for m in mac:
        counters[m["split"]] += 1
        m["cid"] = f"v32-mac-{m['split']}-{counters[m['split']]:04d}"
        entries.append((m["split"], "v32:" + MAC.CATEGORY, m["cid"], True))
    styles = assign_styles(entries)
    rows_out = {"train": [], "dev": []}
    prov_out = {"train": [], "dev": []}
    convs_out = []
    dropped = []
    for split, source, convs, prov in sources:
        for cid, rows in convs.items():
            p = prov[cid]
            style = styles[cid]
            try:
                nrows, cmsgs, info = migrate_conversation(rows, p, style, source)
            except Drop as e:
                dropped.append({"split": split, "source": source, "source_conversation_id": cid, "category": p["category"],
                                "reason": str(e), "source_rows": len(rows)})
                continue
            ncid = new_cid(source, cid)
            for r in nrows:
                rows_out[split].append({"id": f"{ncid}-s{r['k']}", "conversation_id": ncid, "tools": r["tools"],
                                        "messages": r["messages"]})
            keep = {k: v for k, v in p.items() if k not in ("id", "conversation_id", "split", "source_conversation_id",
                                                            "gold_calls", "discovery_modes", "preload", "protocol_variant",
                                                            "n_steps", "preload_contains_needed")}
            pv = {"id": ncid, "conversation_id": ncid, "split": split, "source": source, "source_conversation_id": cid,
                  "source_category": p["category"], **keep, "category": ("v31:" if source == "v31_new" else "v3r:") + p["category"],
                  "schema_style": style, "gold_calls": info["gold_calls"], "discovery_modes": info["discovery_modes"],
                  "preload": info["preload"], "protocol_variant": info["protocol_variant"], "n_steps": info["n_steps"],
                  "source_steps": len(rows), "migrated": bool(info["migrations"]), "migrations": info["migrations"],
                  "removed_preload": info["removed_preload"]}
            prov_out[split].append(pv)
            convs_out.append({"conversation_id": ncid, "id": ncid, "split": split, "category": pv["category"],
                              "schema_style": style, "now": p["reference_now"], "messages": cmsgs,
                              "tools_per_round": info["tools_per_round"]})
    # authored V3.2 category (after the migrated rows, before the V2 replay block)
    for m in mac:
        split, ncid, style = m["split"], m["cid"], styles[m["cid"]]
        prefix = [({"role": role, "content": t, "training": False} if role == "assistant" else {"role": role, "content": t})
                  for role, t in m["prefix"]] + [{"role": "user", "content": m["user_text"]}]
        nrows, cmsgs, info = m["builder"].finish(prefix, m["now"], m["user_text"], style)
        for r in nrows:
            rows_out[split].append({"id": f"{ncid}-s{r['k']}", "conversation_id": ncid, "tools": r["tools"], "messages": r["messages"]})
        pv = {"id": ncid, "conversation_id": ncid, "split": split, "source": "v32_new", "source_conversation_id": None,
              "source_category": MAC.CATEGORY, "category_key": "mac", "family_id": m["fid"],
              "template_id": f"{m['fid']}-{m['j']}", "targeted_failure_class": ["mac_controls_skill"],
              "reference_now": m["now"], "user_prompt": m["user_text"], "user_prompt_clean": m["user_text"],
              "history_user_prompts": [t for role, t in m["prefix"] if role == "user"], "history_turns": len(m["prefix"]),
              "slots": m["meta"], "catalog_variant": m["variant"], "heldout_max_jaccard": round(m["overlap"], 3),
              "generation_attempt": m["attempt"], "category": "v32:" + MAC.CATEGORY, "schema_style": style,
              "gold_calls": info["gold_calls"], "discovery_modes": info["discovery_modes"], "preload": info["preload"],
              "protocol_variant": info["protocol_variant"], "n_steps": info["n_steps"], "source_steps": None,
              "migrated": False, "migrations": [], "removed_preload": []}
        prov_out[split].append(pv)
        convs_out.append({"conversation_id": ncid, "id": ncid, "split": split, "category": pv["category"], "schema_style": style,
                          "now": m["now"], "messages": cmsgs, "tools_per_round": info["tools_per_round"]})
    # V2 replay: no tools, no tool names -> unchanged bytes, appended last (as in V3.1)
    v2 = {s: (V31 / f"v2_replay.{s}.jsonl").read_text() for s in ("train", "dev")}
    for split in ("train", "dev"):
        (out / f"{split}.jsonl").write_text("".join(jdump(x) + "\n" for x in rows_out[split]) + v2[split])
        (out / f"{split}.provenance.jsonl").write_text("".join(jdump(x) + "\n" for x in prov_out[split]))
    (out / "conversations.jsonl").write_text("".join(jdump(x) + "\n" for x in convs_out))
    report = {"version": VERSION, "seed": SEED, "dropped": dropped}
    (out / "migration_report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
    write_stats(out, rows_out, prov_out, dropped, v2)
    print(json.dumps({s: {"conversations": len(prov_out[s]), "rows": len(rows_out[s])} for s in prov_out} | {"dropped": len(dropped)}))


# ----------------------------------------------------------------------------- stats + data card

def est_tokens(obj):
    return len(json.dumps(obj, ensure_ascii=False)) / 4.0


def v31_counts():
    """Conversations per category in the V3.1 sources (for the V3.1 vs V3.2 table)."""
    out = {"train": {}, "dev": {}}
    v3r = read_jsonl(CACHE / "v3_replay.provenance.jsonl")
    for split in ("train", "dev"):
        for p in read_jsonl(CACHE / f"v31_new/{split}.provenance.jsonl"):
            k = "v31:" + p["category"]
            out[split][k] = out[split].get(k, 0) + 1
        for p in v3r:
            if p["split"] == split:
                k = "v3r:" + p["category"]
                out[split][k] = out[split].get(k, 0) + 1
    return out


def write_stats(out, rows, prov, dropped, v2):
    stats = {"version": VERSION, "seed": SEED, "short_descriptions": {"source_sha": SHORT.get("source_sha"),
             "placeholder": bool(SHORT.get("placeholder"))}, "wisp_main_sha": TOOLS["source"]["wisp_main_sha"],
             "splits": {}, "v31_conversations_by_category": v31_counts(), "dropped": {}, "migration_rules": {}}
    for d in dropped:
        stats["dropped"][d["reason"]] = stats["dropped"].get(d["reason"], 0) + 1
    for split in ("train", "dev"):
        rs, ps = rows[split], prov[split]
        toks = sorted(est_tokens(x) for x in rs)
        by_cat, by_style, by_var = {}, {}, {}
        for p in ps:
            by_cat[p["category"]] = by_cat.get(p["category"], 0) + 1
            by_style[p["schema_style"]] = by_style.get(p["schema_style"], 0) + 1
            by_var[p["protocol_variant"]] = by_var.get(p["protocol_variant"], 0) + 1
            for mg in p["migrations"]:
                stats["migration_rules"][mg["migration_rule"]] = stats["migration_rules"].get(mg["migration_rule"], 0) + 1
        rows_style = {}
        style_of = {p["conversation_id"]: p["schema_style"] for p in ps}
        for x in rs:
            s_ = style_of[x["conversation_id"]]
            rows_style[s_] = rows_style.get(s_, 0) + 1
        stats["splits"][split] = {
            "conversations": len(ps), "rows": len(rs), "v2_replay_rows": len([x for x in v2[split].splitlines() if x]),
            "by_category": dict(sorted(by_cat.items())), "conversations_by_schema_style": by_style, "rows_by_schema_style": rows_style,
            "protocol_variants": by_var, "migrated_conversations": sum(1 for p in ps if p["migrated"]),
            "row_tokens_est_chars_div4": {"mean": round(statistics.mean(toks)), "p50": round(toks[len(toks) // 2]),
                                          "p95": round(toks[int(len(toks) * 0.95)]), "max": round(toks[-1])}}
    (out / "stats.json").write_text(json.dumps(stats, indent=1, sort_keys=True) + "\n")
    write_card(out, stats, prov, dropped)


def write_card(out, stats, prov, dropped):
    S = stats["splits"]
    v31 = stats["v31_conversations_by_category"]
    L = ["# Ling dataset V3.2 data card (generated by generate.py)", "",
         f"Version `{VERSION}`, seed {SEED} (terse/full assignment; source conversations keep their V3.1 seed 20261013 / V3 seed "
         f"20261012). Synthetic; no real data. Migrates every V3.1 training conversation to the Wisp tool registry at "
         f"`{stats['wisp_main_sha'][:12]}` (91 offered built-ins + `use_skill` + the five mac-controls skill tools) and renders "
         f"about half of them with terse schemas.", "",
         f"Short descriptions: `assets/short_descriptions.json`, source_sha `{stats['short_descriptions']['source_sha']}`, "
         f"placeholder: {stats['short_descriptions']['placeholder']}.", "",
         "## Counts", "",
         "| split | conversations (tool protocol, incl. v32_mac_controls) | rows | V2 replay rows | migrated conversations | terse conversations | full conversations | terse rows | full rows |",
         "|---|---|---|---|---|---|---|---|---|"]
    for s in ("train", "dev"):
        d = S[s]
        cs, rs = d["conversations_by_schema_style"], d["rows_by_schema_style"]
        L.append(f"| {s} | {d['conversations']} | {d['rows']} | {d['v2_replay_rows']} | {d['migrated_conversations']} | "
                 f"{cs.get('terse', 0)} | {cs.get('full', 0)} | {rs.get('terse', 0)} | {rs.get('full', 0)} |")
    L += ["", "## Conversations by category: V3.1 source vs V3.2", "",
          "`v31:` = V3.1 targeted-fix conversations, `v3r:` = the V3 replay sample V3.1 trained on, `v32:` = authored in V3.2 "
          "(ext_v32_mac.py). V2 replay rows (600 train / 60 dev) are unchanged.", "",
          "| category | V3.1 train | V3.2 train | V3.1 dev | V3.2 dev |", "|---|---|---|---|---|"]
    cats = sorted(set(v31["train"]) | set(v31["dev"]) | set(S["train"]["by_category"]) | set(S["dev"]["by_category"]))
    for c in cats:
        L.append(f"| {c} | {v31['train'].get(c, 0)} | {S['train']['by_category'].get(c, 0)} | {v31['dev'].get(c, 0)} | {S['dev']['by_category'].get(c, 0)} |")
    L += ["", "## Dropped conversations", "", "| source conversation | category | reason |", "|---|---|---|"]
    for d in dropped:
        L.append(f"| {d['source_conversation_id']} | {d['category']} | {d['reason']} |")
    L += ["", "## Migration table", "",
          "Every rewritten call, stub or discovery argument is recorded per conversation in provenance (`migrations`: `migrated_from`, "
          "`migrated_to`, `migration_rule`). Counts below are migration records (one conversation can carry several).", "",
          "| rule | records |", "|---|---|"]
    for r, n in sorted(stats["migration_rules"].items()):
        L.append(f"| `{r}` | {n} |")
    L += ["", "Rules defined but unused by this source set (kept for re-runs): every other entry of `MIGRATIONS` in generate.py "
          "(merged timers/alarms/stopwatch, bluetooth, wifi, flag, weather alerts, wisp_sync, switch_app, the volume/lock/music -> "
          "mac_* skill rules, trash/delete -> `move_path` to `~/.Trash`, grocery evidence -> `search_notes`/`search_reminders`).",
          "", "Retired tools that appeared only as loaded decoys (preloaded SP schemas or explicit-discovery lists) are mapped to their "
          "survivor when merged and removed otherwise (`removed_preload` in provenance); they were never called.",
          "", "## Protocol variants after migration", "", "| split | S | SP-hit | SP-decoy |", "|---|---|---|---|"]
    for s in ("train", "dev"):
        v = S[s]["protocol_variants"]
        L.append(f"| {s} | {v.get('S', 0)} | {v.get('SP-hit', 0)} | {v.get('SP-decoy', 0)} |")
    L += ["", "## Length estimates (characters / 4)", "", "| split | row mean | p50 | p95 | max |", "|---|---|---|---|---|"]
    for s in ("train", "dev"):
        t = S[s]["row_tokens_est_chars_div4"]
        L.append(f"| {s} | {t['mean']} | {t['p50']} | {t['p95']} | {t['max']} |")
    L += ["", "## What changed from V3.1", "",
          "- Registry: `assets/tools.json` is captured from Wisp source (`capture_assets.py`), not from an eval run: 91 offered built-ins "
          "(`update_event` is registered but unavailable, so never offered; `run_applescript` is blocked by the private-shell rule that the "
          "always-present identity block triggers), plus `use_skill` and `mac_get_volume`, `mac_set_volume`, `mac_mute`, `mac_lock_screen`, "
          "`mac_media` registered by the bundled mac-controls skill (category `skill_tool`, family `skills`, always confirmation-gated).",
          "- System prompt: message 0 is `build_system(loaded)` over loop.SYSTEM's blocks (gating names taken from each block's first line, "
          "as Wisp does) + the identity block + the installed-skills catalog (idea-refine, interview-me, mac-controls; a skill's body is "
          "appended when its triggers match the user's text). Message 1 is the now line + `CapabilityCatalog.prompt()`. Message 2 is the "
          "model-led context note from service/main.py.",
          "- Offer order: loaded real schemas, name-only stubs, then `get_tool_schemas` last (as `model_led_offer` sends them).",
          "- Terse rows: description = SHORT[name] (full text for tools without an entry: use_skill, mac_*), parameter `description` keys "
          "stripped recursively; names, types, enums, required, minimum/maximum unchanged. `get_tool_schemas` is identical in both styles "
          "(Wisp builds it outside model_schema). Name-only stubs are identical in both styles.",
          "- Every assistant target, tool result and final answer is byte-identical to its source except where a migration rule rewrote it.",
          "", "## Limitations", "",
          "- Inherits every V3.1 / V3 limitation (synthetic, template phrasing, condensed tool results).",
          "- `v32_mac_controls` (60 train / 8 dev, ext_v32_mac.py) is the only authored category: explicit and relative volume "
          "(read first, about +/-20, clamped), volume reads, mute/unmute, lock success and failure (the answer says NOT locked), "
          "Apple Music / Spotify playback, specific-music requests (no call; open_app only after the user accepts), declined "
          "confirmations (host text `The user denied this action.`, no retry) and refused arguments (the bad call is context only, the "
          "target is the correction). Results are the skill's real output strings; template phrasing is limited (12 train families).",
          "- Merged-tool results keep the old single-purpose shape (e.g. `get_weather(include=[air_quality])` returns only the AQI line).",
          "- The installed-skills block assumes the mac-controls skill ships bundled and enabled and no user skills are installed.",
          "- Quality is unmeasured until Ling is retrained and evaluated (see README gate, including terse-vs-full parity).", ""]
    (out / "DATA_CARD.md").write_text("\n".join(L))


if __name__ == "__main__":
    main()
