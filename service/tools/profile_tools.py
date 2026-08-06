"""Profile tools — Wisp's own picture of who the user is, built from what it
has already synced (Mail, Messages, Notes, Calendar). These don't read any new
data or talk to any new source; they're a local-model pass over the caches the
sync tools already maintain (email_tools.py/imessage_tools.py/notes_tools.py/
assistant/store.py). See service/memory/profile.py for the actual build.
"""
from __future__ import annotations

from service.memory import profile as profile_mod
from service.tools.registry import register


@register(
    "build_profile",
    "Scan everything Wisp has already synced from the user's Mail, Messages, "
    "Notes, and Calendar, and build/update Wisp's own persistent profile of "
    "who the user is — people in their life, work/school, interests, "
    "routines, preferences. Use whenever the user asks Wisp to learn about "
    "them, get to know them better, or build/update a profile. Runs entirely "
    "on the local model (nothing leaves the machine) and can take a little "
    "while since it reads through everything synced so far; it's safe to "
    "re-run any time — each run merges into the existing profile instead of "
    "starting over.",
    {"type": "object", "properties": {}},
    category="assistant_write",
)
async def build_profile() -> str:
    result = await profile_mod.build_profile()
    if not result.get("ok"):
        return f"(couldn't build a profile yet: {result.get('reason')})"
    parts = [f"{src} ({c['chars']:,} chars in {c['batches']} pass"
             f"{'es' if c['batches'] != 1 else ''})"
             for src, c in result["sources"].items()]
    out = "Profile updated from " + ", ".join(parts) + "."
    # Surfacing what was EMPTY matters as much as what was read — a source
    # that silently contributed nothing is how a profile ends up describing
    # someone from a fraction of their data.
    if result.get("missing"):
        out += (" No data available yet from: " + ", ".join(result["missing"])
                + " (still syncing, or access not granted).")
    return out + " Ask \"what do you know about me?\" any time to see it."


@register(
    "show_profile",
    "Show what Wisp knows about the user — people in their life, work/school, "
    "interests, routines, preferences. Use when the user asks 'what do you "
    "know about me', 'show my profile', 'do you know who I am', or similar. "
    "With no arguments returns an OVERVIEW across all sections; pass `section` "
    "to get one section in full (sections: " + ", ".join(profile_mod.SECTIONS)
    + "). If the user asks about one topic (their friends, their classes, "
    "their routine), pass the matching section rather than fetching everything.",
    {"type": "object",
     "properties": {
         "section": {"type": "string",
                     "description": "one section name to return in full; omit for an overview"},
     }},
    category="assistant_read",
)
async def show_profile(section: str | None = None) -> str:
    text = profile_mod.get_profile_text()
    if not text:
        return ("(No profile built yet — call build_profile first, or tell the "
                "user to ask me to \"learn about me from my mail, messages, "
                "notes, and calendar\".)")
    return profile_mod.render_profile(text, section)
