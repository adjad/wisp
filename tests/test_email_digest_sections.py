"""The inbox digest sorts mail by what needs attention.

Before, it was a flat list grouped by sender, with a dense statistics paragraph on
top and "(1 message, 1 unread; accounts: Google)" on every line, so "Office Hours
CANCELLED" sat between two promotions. Classification is header-only (sender, address,
subject, unread flag); message bodies are never read and no model is involved.
"""
import re
import time

import pytest

from service.tools import email_tools as E

NOW = time.time()


def row(name, address, subject, unread=False, account="Personal", age_h=1):
    return {"sender": name, "sender_address": address, "subject": subject, "unread": unread,
            "account": account, "account_id": account, "ts": NOW - age_h * 3600,
            "message_id": f"<{name}-{subject}-{age_h}>", "native_id": ""}


def section_of(output, needle):
    """The section title a line mentioning `needle` sits under."""
    title = None
    for line in output.splitlines():
        match = re.match(r"\*\*(.+?)\*\* \(\d+\)$", line)
        if match:
            title = match.group(1)
        elif needle in line and title:
            return title
    return None


CASES = [
    # (row, expected category)
    (row("Priya Shah", "priya@acme.example", "Can you review the contract?", True), "attention"),
    (row("Priya Shah", "priya@acme.example", "Lunch last week", False), "people"),
    (row("Registrar", "registrar@ucsc.edu", "Enrollment appointment is due Oct 3", True), "attention"),
    (row("Canvas", "notifications@instructure.com", "Midterm exam rescheduled to Oct 8"), "attention"),
    (row("Amy Zeng via Ed", "notification@edstem.org", "Office Hours CANCELLED", True), "attention"),
    (row("Canvas", "notifications@instructure.com", "PA 1 and the next reading assignment posted"), "attention"),
    (row("Canvas", "notifications@instructure.com", "Lecture 2 recording has been posted"), "school"),
    (row("Psychology Advising", "psyadv@ucsc.edu", "Welcome to UCSC! Meet your advisor"), "school"),
    (row("BYU Independent Study", "indstudy@byu.edu", "Your flexible algebra solution!", True), "school"),
    (row("Chase Alerts", "alerts@chase.example", "Security alert: new sign-in", True), "attention"),
    # A security/payment problem is surfaced whoever sent it (labelled "Subject says:").
    (row("Shop Newsletter", "news@shop.example", "Security alert: verify your account"), "attention"),
    # An ordinary promotion that merely says it expires is still a promotion.
    (row("Shop Newsletter", "news@shop.example", "Your coupon expires tonight"), "news"),
    (row("Amazon", "ship@amazon.example", "Your order has shipped"), "accounts"),
    (row("Venmo", "venmo@venmo.example", "Receipt: you paid Sam $12.00"), "accounts"),
    (row("LinkedIn", "notifications@linkedin.example", "You appeared in 9 searches", True), "social"),
    (row("Instagram", "no-reply@instagram.example", "maya started following you", True), "social"),
    (row("The New York Times", "nytdirect@nytimes.com", "The Morning: Markets rally", True), "news"),
    (row("NVIDIA", "news@nvidia.com", "New ways to build AI agents", True), "news"),
    (row("ChatGPT", "noreply@email.example", "Translate anything, your way", True), "news"),
    (row("Opal", "opal-noreply@google.example", "Opal is graduating to skills", True), "news"),
    (row("Slug Security Announcements", "slugsec@ucsc.edu", "Fall 2026 Security Newsletter", True), "news"),
]


@pytest.mark.parametrize("message,expected", CASES, ids=[f"{r['sender']}|{r['subject'][:28]}" for r, _ in CASES])
def test_classification(message, expected):
    assert E.digest_category(message) == expected


def test_urgent_words_in_a_promotion_do_not_demand_attention():
    for subject in ("Deadline extended: save 20% on everything", "Last chance: sale ends, offer expires tonight",
                    "Your weekly digest is due"):
        assert E.digest_category(row("Deals Weekly", "deals@store.example", subject, True)) != "attention", subject


def test_an_unread_broadcast_is_not_mistaken_for_a_person_waiting():
    for name, address in (("University Registrar", "reg@school.example"), ("Acme Support Team", "help@acme.example"),
                          ("NVIDIA", "hello@nvidia.example"), ("Sender 7", "s7@example.test"),
                          ("Community Updates", "c@example.test"), ("ZIPRECRUITER", "z@example.test")):
        assert E.digest_category(row(name, address, "Hello there", True)) != "attention", name


@pytest.mark.parametrize("name,expected", [
    ("Priya Shah", True), ("Dad", True), ("Maya", True), ("Jean-Luc O'Brien", True), ("Renée Dupont", True),
    ("Amy Zeng via Ed", True), ("NVIDIA", False), ("BYU Independent Study", False), ("Psychology Advising", False),
    ("Acme Inc", False), ("Sender 3", False), ("", False), ("jane@acme.example", False),
])
def test_person_detection(name, expected):
    assert E._looks_like_person(name, "x@example.test") is expected


# ------------------------------------------------------------------ layout

INBOX = [
    row("Priya Shah", "priya@acme.example", "Can you review the contract before Friday?", True, "Work", 1),
    row("Chase Alerts", "alerts@chase.example", "Security alert: new sign-in to your account", True, "Personal", 2),
    row("Maya Chen", "maya@friends.example", "photos from the weekend", False, "Personal", 9),
    row("Amazon", "ship@amazon.example", "Your order has shipped", False, "Personal", 7),
    row("LinkedIn", "notifications@linkedin.example", "You appeared in 9 searches", True, "Personal", 4),
    row("The New York Times", "nytdirect@nytimes.com", "The Morning: Markets rally", False, "Personal", 10),
    row("The New York Times", "nytdirect@nytimes.com", "The Evening: Weekend reads", False, "Personal", 11),
    row("NVIDIA", "news@nvidia.com", "New ways to build AI agents", True, "Personal", 12),
]


@pytest.fixture
def digest():
    return E.sender_digest(INBOX, "your recent inbox", scanned=212, truncated=198)


def test_sections_come_in_reading_order(digest):
    titles = re.findall(r"^\*\*(.+?)\*\* \(\d+\)$", digest, re.M)
    assert titles == ["🔴 Needs your attention", "👤 From people", "🧾 Accounts & receipts",
                      "💬 Social", "📰 Newsletters & updates"]


def test_the_top_line_is_a_plain_count_not_a_statistics_paragraph(digest):
    first, second = digest.splitlines()[:2]
    assert first == "📬 **Inbox digest — your recent inbox**"
    assert second == "8 emails · 4 unread · 2 need attention"
    assert "Scanned" not in "\n".join(digest.splitlines()[:4])


def test_each_message_is_one_tidy_line_with_sender_domain_and_unread_mark(digest):
    assert "- ● **Priya Shah** · acme.example — “Can you review the contract before Friday?” · Work" in digest
    assert "- **Maya Chen** · friends.example — “photos from the weekend” · Personal" in digest
    assert "(1 message" not in digest and "accounts:" not in digest      # the old per-line clutter
    assert "<priya@acme.example>" not in digest                           # no angle-bracket address


def test_urgent_subjects_are_still_labelled_as_the_senders_claim(digest):
    assert "Subject says: “Security alert: new sign-in to your account”" in digest


def test_noisy_sections_are_one_line_of_names_without_subjects(digest):
    social = section_of(digest, "LinkedIn")
    assert social == "💬 Social"
    assert "LinkedIn" in digest and "You appeared in 9 searches" not in digest
    roll_up = next(l for l in digest.splitlines() if "NVIDIA" in l)
    assert "The New York Times (2)" in roll_up and "NVIDIA" in roll_up and "—" not in roll_up
    assert "The Morning: Markets rally" not in digest


def test_the_coverage_disclosure_is_kept_but_moved_to_the_end(digest):
    assert digest.rstrip().splitlines()[-1].startswith("**Coverage:** Scanned 212 headers")
    assert "truncated 198 messages" in digest and "Actual dates:" in digest
    assert digest.index("**Coverage:**") > digest.index("Newsletters & updates")


def test_a_display_name_with_two_addresses_shows_them_in_full():
    rows = [row("Nina", "nina@example.test", "Project update", True),
            row("Nina", "other@evil.example", "Project update", True, age_h=2)]
    out = E.sender_digest(rows, "x")
    assert "**Nina** · nina@example.test" in out and "**Nina** · other@evil.example" in out


def test_senders_with_unknown_addresses_are_never_merged():
    rows = [row("University", "", "Notice one"), row("University", "", "Notice two", age_h=2)]
    out = E.sender_digest(rows, "x")
    assert out.count("University (address unavailable)") == 2


def test_the_account_tag_appears_only_with_more_than_one_account():
    one = E.sender_digest([row("Priya Shah", "priya@acme.example", "Review this?", True)], "x")
    assert " · Personal" not in one
    two = E.sender_digest([row("Priya Shah", "priya@acme.example", "Review this?", True, "Work"),
                           row("Maya", "maya@friends.example", "hi", True, "Personal", 3)], "x")
    assert " · Work" in two and " · Personal" in two


def test_long_subjects_are_clipped_and_markup_is_escaped():
    out = E.sender_digest([row("Priya Shah", "priya@acme.example", "Review " + "very " * 60 + "*bold*", True)], "x")
    line = next(l for l in out.splitlines() if l.startswith("- ● **Priya"))
    assert "…" in line and len(line) < 200


def test_an_attention_section_over_its_limit_says_how_many_are_not_shown():
    rows = [row(f"Person {chr(65 + i)}one", f"p{i}@example.test", f"Please review item {i}", True, age_h=i + 1)
            for i in range(12)]
    out = E.sender_digest(rows, "x", max_senders=12)
    assert "…and 2 more in this group" in out


def test_the_flat_layout_is_still_available_for_the_daily_summary():
    flat = E.sender_digest(INBOX, "your recent inbox", layout="flat")
    assert "**Priya Shah <priya@acme.example>** (1 message, 1 unread; accounts: Work)" in flat
    assert "Needs your attention" not in flat
    import inspect
    from service.assistant import brief
    assert 'layout="flat"' in inspect.getsource(brief._email_block)


def test_classification_never_needs_a_message_body_or_a_model():
    # digest_category takes only header fields; a row with no body keys is complete.
    assert set(E.digest_category.__code__.co_varnames[:1]) == {"row"}
    assert E.digest_category({"subject": "x", "sender": "y"}) in {"people", "news", "accounts", "school", "social", "attention"}
