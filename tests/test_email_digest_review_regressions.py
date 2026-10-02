"""Review regressions for the sectioned inbox digest.

1. An account-security message from a social network (a LinkedIn sign-in alert)
   was filed under Social and rolled up to just "LinkedIn", hiding the subject.
2. Per-sender clipping ran before the section counts, so four urgent messages from
   one Canvas sender were headlined as "3 need attention".

Synthetic headers only; no mail, model, network or subprocess.
"""
import re
import time

import pytest

from service.tools import email_tools as E

NOW = time.time()
ATTENTION = "🔴 Needs your attention"


def row(name, address, subject, unread=False, account="Personal", age_h=1.0):
    return {"sender": name, "sender_address": address, "subject": subject, "unread": unread,
            "account": account, "account_id": account, "ts": NOW - age_h * 3600,
            "message_id": f"<{address}-{subject}-{age_h}>", "native_id": ""}


def sections(output):
    """{title: (header count, [body lines])} for every section block."""
    found, title = {}, None
    for line in output.splitlines():
        match = re.match(r"\*\*(.+?)\*\* \((\d+)\)$", line)
        if match:
            title = match.group(1)
            found[title] = (int(match.group(2)), [])
        elif not line.strip() or line.startswith("**Coverage:**"):
            title = None
        elif title:
            found[title][1].append(line)
    return found


def headline(output):
    return output.splitlines()[1]


def headline_total(output):
    return int(re.match(r"(\d+) emails?", headline(output)).group(1))


def messages_accounted_for(lines):
    """Messages a full section's lines account for: one per bullet, plus each
    '+N more from this sender' note, plus the '…and N more' remainder."""
    total = 0
    for line in lines:
        remainder = re.match(r"- …and (\d+) more", line)
        if remainder:
            total += int(remainder.group(1))
            continue
        total += 1
        more = re.search(r"\(\+(\d+) more from this sender\)", line)
        if more:
            total += int(more.group(1))
    return total


# ------------------------------------------------- 1. security alerts from social senders

SOCIAL_SECURITY = [
    row("LinkedIn", "security-noreply@linkedin.example", "Adi, we noticed a new sign-in to your account", True),
    row("LinkedIn", "security-noreply@linkedin.example", "Security alert: unusual sign-in activity", True),
    row("LinkedIn", "security-noreply@linkedin.example", "Adi, here's the PIN to verify your email"),
    row("Facebook", "security@facebookmail.example", "Someone tried to log in to your account", True),
    row("Instagram", "security@mail.instagram.example", "Your Instagram security code"),
    row("Discord", "noreply@discord.example", "Your password was changed", True),
    row("X", "verify@x.com", "Confirm it's you: new login from Chrome on Mac", True),
    row("Reddit", "noreply@reddit.example", "Your account has been locked"),
]

SHOP_SECURITY = [
    row("Shop Newsletter", "news@shop.example", "Security alert: verify your account"),
    row("Shop Newsletter", "news@shop.example", "New sign-in to your Shop account", True),
    row("Shop Deals", "deals@shop.example", "Your verification code for Shop"),
]

SOCIAL_ACTIVITY = [
    row("LinkedIn", "notifications@linkedin.example", "You appeared in 9 searches", True),
    row("LinkedIn", "invitations@linkedin.example", "Maya Chen wants to connect", True),
    row("LinkedIn", "notifications@linkedin.example", "People you may know: 5 new connection suggestions"),
    row("Instagram", "no-reply@instagram.example", "maya started following you", True),
    row("Facebook", "notification@facebookmail.example", "Sam liked your post", True),
    row("Reddit", "noreply@reddit.example", "\"Best password managers in 2026?\" trending in r/privacy"),
    row("X", "notify@x.com", "@maya mentioned you in a post"),
]


@pytest.mark.parametrize("message", SOCIAL_SECURITY + SHOP_SECURITY,
                         ids=[f"{r['sender']}|{r['subject'][:30]}" for r in SOCIAL_SECURITY + SHOP_SECURITY])
def test_an_account_security_message_needs_attention_whoever_sent_it(message):
    assert E.digest_category(message) == "attention"


@pytest.mark.parametrize("message", SOCIAL_ACTIVITY,
                         ids=[f"{r['sender']}|{r['subject'][:30]}" for r in SOCIAL_ACTIVITY])
def test_ordinary_social_activity_stays_in_the_social_roll_up(message):
    assert E.digest_category(message) == "social"


def test_a_linkedin_sign_in_alert_keeps_its_subject_in_the_attention_section():
    rows = [row("LinkedIn", "security-noreply@linkedin.example",
                "Adi, we noticed a new sign-in to your account", True, age_h=1),
            row("LinkedIn", "notifications@linkedin.example", "You appeared in 9 searches", True, age_h=2),
            row("Instagram", "no-reply@instagram.example", "maya started following you", True, age_h=3)]
    out = E.sender_digest(rows, "x")
    found = sections(out)
    assert found[ATTENTION][0] == 1
    alert = found[ATTENTION][1]
    # "LinkedIn" fronts two addresses here, so the full sender address is shown.
    assert alert == ["- ● **LinkedIn** · security-noreply@linkedin.example — "
                     "“Adi, we noticed a new sign-in to your account”"]
    # The ordinary activity is still collapsed to names, with no subjects.
    social_count, social_lines = found["💬 Social"]
    assert social_count == 2 and social_lines == ["LinkedIn, Instagram"]
    assert "You appeared in 9 searches" not in out and "started following" not in out
    assert headline(out) == "3 emails · 3 unread · 1 needs attention"


def test_genuine_newsletters_with_urgent_words_are_still_newsletters():
    for subject in ("Deadline extended: save 20% on everything", "Fall 2026 Security Newsletter",
                    "Two-factor authentication explained: a beginner's guide"):
        assert E.digest_category(row("Deals Weekly", "deals@store.example", subject, True)) != "attention", subject


# ------------------------------------------------- 2. counts before per-sender clipping

def canvas(n, start=0):
    return [row("Canvas", "notifications@instructure.example", f"Assignment {i} due tonight", True, age_h=1 + i)
            for i in range(start, start + n)]


@pytest.mark.parametrize("n", [1, 3, 4, 10])
def test_every_urgent_message_from_one_sender_is_counted(n):
    out = E.sender_digest(canvas(n), "x")
    noun = "email" if n == 1 else "emails"
    verb = "needs" if n == 1 else "need"
    assert headline(out) == f"{n} {noun} · {n} unread · {n} {verb} attention"
    count, lines = sections(out)[ATTENTION]
    assert count == n
    # The display still clips one sender to three lines, honestly.
    assert len(lines) == min(n, 3)
    if n > 3:
        assert lines[0].endswith(f"(+{n - 3} more from this sender)")
    else:
        assert "more from this sender" not in out
    assert messages_accounted_for(lines) == n
    assert f"represented {n} message{'s' if n != 1 else ''} from 1 sender note;" in out


def test_four_urgent_canvas_messages_are_headlined_as_four():
    out = E.sender_digest(canvas(4), "x")
    assert headline(out) == "4 emails · 4 unread · 4 need attention"
    assert "**🔴 Needs your attention** (4)" in out
    assert "(+1 more from this sender)" in out


def test_one_sender_split_across_sections_is_counted_in_each():
    rows = canvas(4) + [
        row("Canvas", "notifications@instructure.example", f"Lecture {i} recording has been posted", False, age_h=20 + i)
        for i in range(2)]
    out = E.sender_digest(rows, "x")
    found = sections(out)
    assert found[ATTENTION][0] == 4 and messages_accounted_for(found[ATTENTION][1]) == 4
    assert found["🎓 School & courses"][0] == 2
    assert messages_accounted_for(found["🎓 School & courses"][1]) == 2
    assert headline(out) == "6 emails · 4 unread · 4 need attention"


def test_mixed_senders_counts_equal_the_sum_of_sections():
    rows = (canvas(5)
            + [row("Priya Shah", "priya@acme.example", "Can you review the contract?", True, age_h=2),
               row("Maya Chen", "maya@friends.example", "photos from the weekend", False, age_h=9),
               row("LinkedIn", "security-noreply@linkedin.example", "New sign-in to your account", True, age_h=3),
               row("LinkedIn", "notifications@linkedin.example", "You appeared in 9 searches", True, age_h=4),
               row("LinkedIn", "notifications@linkedin.example", "Maya Chen wants to connect", False, age_h=5),
               row("Amazon", "ship@amazon.example", "Your order has shipped", False, age_h=7)]
            + [row("The New York Times", "nytdirect@nytimes.com", f"The Morning {i}", False, age_h=10 + i)
               for i in range(4)])
    out = E.sender_digest(rows, "x")
    found = sections(out)
    total = headline_total(out)
    assert total == len(rows) == sum(count for count, _ in found.values())
    assert found[ATTENTION][0] == 7          # 5 Canvas + Priya + the LinkedIn sign-in
    assert headline(out) == f"{len(rows)} emails · 8 unread · 7 need attention"
    for title in (ATTENTION, "👤 From people", "🧾 Accounts & receipts"):
        assert messages_accounted_for(found[title][1]) == found[title][0], title
    assert found["💬 Social"] == (2, ["LinkedIn (2)"])
    assert found["📰 Newsletters & updates"] == (4, ["The New York Times (4)"])
    assert f"represented {len(rows)} messages from 7 sender notes" in out


def test_an_over_limit_attention_section_accounts_for_every_message():
    rows = canvas(5) + [
        row(f"Person {chr(65 + i)}one", f"p{i}@example.test", f"Please review item {i}", True, age_h=30 + i)
        for i in range(10)]
    out = E.sender_digest(rows, "x", max_senders=12)
    count, lines = sections(out)[ATTENTION]
    assert count == 15 and headline(out) == "15 emails · 15 unread · 15 need attention"
    assert len([l for l in lines if not l.startswith("- …")]) == 10
    assert messages_accounted_for(lines) == 15
    assert "- …and 3 more in this group" in lines


def test_hidden_senders_are_disclosed_honestly_beside_the_counts():
    rows = canvas(4) + [row(f"Sender {i}", f"s{i}@example.test", f"General update {i}", age_h=40 + i)
                        for i in range(13)]
    out = E.sender_digest(rows, "x", max_senders=12)
    found = sections(out)
    # 12 sender notes: Canvas (4) + 11 general senders; 2 senders are hidden.
    assert headline_total(out) == 15 == sum(count for count, _ in found.values())
    assert "represented 15 messages from 12 sender notes" in out
    assert "truncated 2 messages (0 by scan limit, 2 by sender note limit)" in out
    footer = out.rstrip().splitlines()[-1]
    assert footer.startswith("2 more sender addresses (2 messages)")
    assert "included in the counts above" not in footer


def test_the_flat_daily_summary_layout_still_lists_every_message_count():
    flat = E.sender_digest(canvas(4), "x", layout="flat")
    assert "**Canvas <notifications@instructure.example>** (4 messages, 4 unread)" in flat
    assert flat.rstrip().endswith("; +1 more")
    assert "Needs your attention" not in flat
