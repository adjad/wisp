"""Check voice rewrites against the hard rules in REWRITE_GUIDE.md.

    python3 check_rewrite.py <rewrites.jsonl> [more.jsonl ...]   # each line: {"id", "orig_sha", "text"}

Reads rewrite_queue.jsonl for the originals and their context. Prints one line per problem; exit 1 if any. Also used
by validate_v4.py, so the same rules gate the final build.
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

NUM = re.compile(r"\d+(?:[:.,/]\d+)*")
QUOTED = re.compile(r"“([^”]{4,})”|\"([^\"]{4,})\"")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
URL = re.compile(r"https?://\S+")
CODE = re.compile(r"```.*?```", re.S)
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⭐⭕✅❌]")
CAP = re.compile(r"\b[A-Z][a-zA-Z'’&-]+(?:\s+[A-Z][a-zA-Z'’&-]+)*")
BAD_NEWS = re.compile(r"\b(denied|blocked|failed|fail|didn't work|did not work|couldn't|could not|can't|cannot|unable|"
                      r"not allowed|no tool|isn't allowed|nothing matched|not found|no results|error)\b", re.I)
BAD_TRACE = re.compile(r"\b(denied|blocked|failed|didn't work|report that rather than invent|honestly)\b", re.I)
SKELETON = ("Suggest how to use", "Suggest how to use it.")
COMMON_CAPS = {"I", "I'm", "I'll", "I've", "I'd", "Wisp", "You", "Your", "Here", "Hey", "Hi", "Yes", "No", "Nope", "Yep", "Sure",
               "Done", "Got", "Okay", "OK", "Ok", "Good", "Great", "Nice", "Want", "Would", "Should", "Shall", "Let", "Let's",
               "Just", "That", "That's", "This", "There", "There's", "It", "It's", "And", "But", "So", "Also", "Then", "If",
               "When", "What", "Which", "Who", "How", "Today", "Tomorrow", "Tonight", "Morning", "Afternoon", "Evening",
               "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "AM", "PM", "Plus", "All",
               "Nothing", "Looks", "Heads", "Oh", "Ah", "Aw", "Woo", "Whoa", "Wow", "Yay", "Congrats", "Congratulations",
               "Thanks", "Thank", "Welcome", "Happy", "Enjoy", "Sounds", "Perfect", "Awesome", "Amazing", "Love", "Ready",
               "One", "Two", "Three", "First", "Next", "After", "Before", "Once", "Quick", "Big", "Light", "Busy", "Free",
               "Lots", "Not", "Unfortunately", "Sorry", "Hmm", "Alright", "All right", "Absolutely", "Totally", "Maybe",
               "Or", "Either", "Both", "Since", "Because", "While", "Until", "Note", "Tip", "Pro", "Bonus", "Option",
               "Do", "Don't", "Did", "Can", "Could", "Ask", "Tell", "Give", "Make", "Take", "Try", "Keep", "Check", "See",
               "Need", "Is", "Are", "Was", "Were", "Have", "Has", "Had", "Will", "My", "Me", "We", "We're", "Our", "Its",
               "Anything", "Everything", "Something", "Someone", "Calendar", "Reminders", "Mail", "Messages", "Notes",
               "Settings", "Privacy", "Security", "Automation", "Files", "Finder", "Mac", "Safari", "Music", "Spotify"}


def strip_md(text):
    return text.replace("**", "").replace("__", "").replace("`", "")


def facts(original):
    """Things the rewrite must keep, verbatim (after markdown is stripped)."""
    o = strip_md(CODE.sub(" ", original))
    keep = set(NUM.findall(o)) | set(EMAIL.findall(o)) | set(URL.findall(o))
    keep |= {a or b for a, b in QUOTED.findall(original)}
    return keep


def problems(item, text):
    out = []
    orig = item["original"]
    if not text or not text.strip():
        return ["empty rewrite"]
    plain = strip_md(text)
    for f in sorted(facts(orig)):
        if strip_md(f) not in plain:
            out.append(f"dropped fact {f!r}")
    for block in CODE.findall(orig):
        if block not in text:
            out.append("code block changed")
    context = " ".join([orig, item.get("user", ""), item.get("now", ""), json.dumps(item.get("tool_results", []), ensure_ascii=False),
                        json.dumps(item.get("history", []), ensure_ascii=False)])
    context_plain = strip_md(context)
    for n in set(NUM.findall(plain)) - set(NUM.findall(context_plain)):
        out.append(f"new number {n!r}")
    for e in set(EMAIL.findall(plain)) - set(EMAIL.findall(context_plain)):
        out.append(f"new email address {e!r}")
    skeleton = any(s in orig for s in SKELETON) or item.get("family") == "chat_warm"
    if not skeleton:
        body = CODE.sub(" ", plain)
        # A single capitalised word that only opens a sentence or list item ("Sent", "Here's") is not a name.
        openers = {m.group(1) for m in re.finditer(r"(?:^|[.!?:]\s+|\n\s*(?:[-*]\s+)?)([A-Z][a-zA-Z'’]+)\b", body)}
        for phrase in set(CAP.findall(body)):
            if phrase in openers and " " not in phrase and body.count(phrase) == 1:
                continue
            words = [w for w in phrase.split() if w not in COMMON_CAPS]
            if words and not all(w in context_plain for w in words):
                out.append(f"new name or title {phrase!r}")
    pron = re.compile(r"\b(he|she|him|her|his|hers|himself|herself)\b", re.I)
    if len(pron.findall(plain)) > len(pron.findall(strip_md(orig))) + len(pron.findall(item.get("user", ""))):
        out.append("adds a gendered pronoun (never guess pronouns from a name)")
    emoji = EMOJI.findall(text)
    if len(emoji) > 2:
        out.append(f"{len(emoji)} emoji (max 2)")
    if emoji and (BAD_NEWS.search(orig) or BAD_TRACE.search(item.get("trace", ""))):
        out.append("emoji on a failure, denial or refusal")
    asks = orig.rstrip().endswith("?")
    if asks and text.count("?") > orig.count("?") + 1:
        out.append("added questions to a clarifying answer")
    if not asks and not skeleton and text.count("?") > 1:
        out.append("more than one question in an answer that asked none")
    if "Suggest how to use" in text:
        out.append("skeleton instruction left in the answer")
    if len(plain.split()) > max(160, 3 * len(orig.split())) and "```" not in orig:
        out.append(f"too long ({len(plain.split())} words)")
    return out


def load_queue():
    return {json.loads(line)["id"]: json.loads(line) for line in (HERE / "rewrite_queue.jsonl").read_text().splitlines()}


def main(paths):
    queue = load_queue()
    bad = 0
    seen = 0
    ratios, emoji, openers = [], 0, {}
    for p in paths:
        for line in Path(p).read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            seen += 1
            item = queue.get(r["id"])
            if item is None:
                print(f"{r['id']}: not in rewrite_queue.jsonl")
                bad += 1
                continue
            if r.get("orig_sha") != item["orig_sha"]:
                print(f"{r['id']}: orig_sha does not match the queue")
                bad += 1
            text = r.get("text", "")
            ratios.append(len(text.split()) / max(1, len(item["original"].split())))
            emoji += bool(EMOJI.search(text))
            key = " ".join(re.findall(r"[A-Za-z']+", text)[:2])
            openers[key] = openers.get(key, 0) + 1
            for prob in problems(item, text):
                print(f"{r['id']}: {prob}")
                bad += 1
    if ratios:
        ratios.sort()
        top = sorted(openers.items(), key=lambda kv: -kv[1])[:3]
        print(f"voice: median length x{ratios[len(ratios) // 2]:.2f}, emoji in {100 * emoji // len(ratios)}%, "
              f"most common openers {top}")
    print(f"{seen} rewrites checked, {bad} problems")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main(sys.argv[1:])
