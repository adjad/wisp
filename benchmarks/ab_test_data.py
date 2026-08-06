"""Raw test data for A/B testing router/summarizer candidates against the
current production models.

The mail and calendar fixtures below are SYNTHETIC, in the same shape and
proportions as a real Mail.app inbox and a real `/assistant/upcoming` response
(same `epochSecs | sender | subject` format MailReader.swift produces, same
promo-to-signal ratio, same ~4-day span). They stand in for a live capture so
this file can be published and re-run by anyone. Names and domains follow the
convention used by scripts/wisp_testdata.py — invented people, `.test` and
`.example` domains.

iMessage is NOT included: reading chat.db requires Full Disk Access for the
reading process. See tests/ab_test_models.py docstring for how to add it once
available.
"""

# ---------------------------------------------------------------------------
# 1. ROUTER / CLASSIFICATION test set — the exact cases used to validate and
#    fix rule_route() across this project's session history. (role, tools)
#    are the EXPECTED/correct answer, established and hand-verified earlier.
# ---------------------------------------------------------------------------
ROUTER_CASES = [
    # (prompt, expected_role, expected_needs_tools)
    ("quit safari", "agent", True),
    ("play some music", "agent", True),
    ("check my calendar", "agent", True),
    ("summarize my emails", "agent", True),
    ("what's in my inbox", "agent", True),
    ("cancel my lunch with sam", "agent", True),
    ("when is my next meeting", "agent", True),
    ("do I have any meetings today", "agent", True),
    ("remind me to call mom at 4pm", "agent", True),
    ("add a dentist appointment friday at 2pm", "agent", True),
    ("look at my screen and tell me what app is in focus", "agent", True),
    ("what am I looking at", "agent", True),
    ("take a screenshot", "agent", True),
    ("delete the temp files", "agent", True),
    ("run a script to list files in my downloads", "agent", True),
    ("write a python function to sort a list", "coding", False),
    ("fix the bug in my palindrome function", "coding", False),
    ("prove that sqrt 2 is irrational", "reasoning", False),
    ("find the derivative of x^2", "reasoning", False),
    ("compare electric and gas cars", "general", False),
    ("explain how photosynthesis works", "general", False),
    ("hello there", "fast", False),
    ("thanks bud", "fast", False),
    ("good morning", "fast", False),
    ("make it vegetarian", None, False),  # genuinely ambiguous follow-up
    # safety case: greeting + destructive action must NOT be treated as trivial
    ("hello can you delete all my files", "agent", True),
]

# ---------------------------------------------------------------------------
# 2. EMAIL SUMMARIZATION — inbox headers, "epochSecs | sender | subject"
#    (same format MailReader.swift produces). ~40 messages spanning roughly
#    four days. Synthetic, but deliberately noisy in the same proportions a
#    real consumer inbox is: retail blasts, job-board digests, security
#    boilerplate, and exactly two messages from actual humans.
# ---------------------------------------------------------------------------
EMAIL_HEADERS_RAW = """
1783999310 | Summit Outfitters | Clearance | Up to 70% off select styles is ON! Shop clearance while it lasts
1783994615 | JobFeed Alerts | Cashier/Host at Harbor Tavern. 9 more new cashier jobs in Westbrook
1783992888 | JobFeed Alerts | Cashier/Host at Harbor Tavern. 11 more new cashier jobs in Fairview
1783977044 | CareerBoard Community | Does anyone else feel like the interview process has become so rigorous with so...
1783972397 | Mail Provider | Security alert
1783972388 | Mail Provider | Security alert
1783972002 | JobFeed | Team Member @ Northgate Cinemas, Inc
1783963967 | Riverbend State University | What is 'RiverID'?
1783960559 | Orientation Leader Team | Comparative Literature Info Session on July 15
1783959310 | HireLoop | Robotics Instructor opening at CodeCamp Northside
1783948147 | Priya Raghunathan | Re: Summer Bootcamp Inquiry
1783947733 | Summit Outfitters | Flash Sale | Last chance! Don't miss up to 50% OFF our Flash Sale.
1783941040 | Corner Barbershop | Sad News & Good News (Welcome Nicholas)
1783935133 | Sam @ HireLoop | I think this job might be right for you!
1783926140 | HackBoard | Northwind Labs is Hiring! Join Our Team - Summer Hacks V1
1783926070 | Summit Fan Shop | Gear up for the semifinals
1783916700 | CareerBoard Jobs | Crew Member at Ashfield Grill and 12 more jobs in Fairview for you.
1783915185 | JobFeed Alerts | Cashier at Wingbasket. 16 more new cashier jobs in Westbrook
1783913877 | CareerBoard Jobs | New jobs in Westbrook. Apply Now.
1783913792 | Summit Outfitters | Flash Sale | Score up to 50% off during our Flash Sale! (ends TONIGHT)
1783911680 | JobFeed Alerts | Cashier at Wingbasket. 13 more new cashier jobs in Fairview
1783899493 | Mail Provider | Review your account settings
1783899432 | Mail Provider | Security alert
1783898323 | Social Network | About Isaiah and others: 1 other new notification and 1 friend request.
1783893882 | CareerBoard Jobs | Cashier role at Stonefire Pizza: you would be a great fit!
1783885607 | JobFeed | Cashier @ Homestead Supply
1783880855 | Valley College Online | Recent Coursework Notifications
1783871299 | Campus Photos | Deadline Extended with Free Shipping.
1783863128 | Summit Outfitters | Clearance | CLEARANCE DEALS with extra 30% OFF select styles!
1783842698 | HireLoop | Behavior Technician - Childcare Experience Needed opening at Bright Path Learning
1783839717 | HackBoard | Orbit II - Summer Hacks V1
1783834114 | Sam @ HireLoop | I think this job might be right for you!
1783830353 | CareerBoard Jobs | Team Member at Quick Bites and 8 more jobs in Fairview for you.
1783828755 | CareerBoard Jobs | New jobs in Westbrook. Apply Now.
1783827707 | JobFeed Alerts | Cashier/Host at Harbor Tavern. 9 more new cashier jobs in Westbrook
1783826041 | Summit Outfitters | Flash Sale | Flash Sale is live: up to 50% off now
1783825039 | JobFeed Alerts | Cashier/Host at Harbor Tavern. 8 more new cashier jobs in Fairview
1783812966 | Dana Whitfield | Fwd: Order Confirmation - Order #38617800
1783807733 | CareerBoard Jobs | Part Time Service Team Member role at Craftworks: you would be a great fit!
1783803103 | Summit Fan Shop | Argentina moves on!
""".strip()

# A harder case: mostly promo/noise with exactly TWO items that actually need
# a reply (Priya, Dana) — good stress test for whether a small model can
# still surface the signal instead of just listing everything flatly.
EMAIL_SIGNAL_ITEMS = ["Priya Raghunathan", "Dana Whitfield"]

# ---------------------------------------------------------------------------
# 3. CALENDAR SUMMARIZATION — synced events in the shape
#    GET /assistant/upcoming?days=45 returns, formatted the same way the
#    get_upcoming tool renders them for the model. The bracketed name is the
#    calendar owner, which is how real synced events carry the account name.
# ---------------------------------------------------------------------------
CALENDAR_EVENTS_RAW = """
- Tue Jul 15 2026, 9:00 AM (meeting): Meeting with Dana [Alex Rivera]
- Mon Jul 21 2026, 6:00 PM (meeting): Westbrook Community Office Hours [Alex Rivera] @ Corner Bakery, 120 Mill St, Westbrook
- Sun Aug 3 2026, 6:00 PM (meeting): Westbrook Community Office Hours [Alex Rivera] @ Corner Bakery, 120 Mill St, Westbrook
""".strip()

# ---------------------------------------------------------------------------
# 4. iMESSAGE — BLOCKED. chat.db needs Full Disk Access for the reading
#    process; neither the shell used to pull this data, nor (unconfirmed)
#    Wisp.app itself, has that grant. What IS accessible without FDA (group
#    chat names only, via Messages.app AppleScript) is below for context, but
#    it's not enough to build a summarization test case.
# ---------------------------------------------------------------------------
IMESSAGE_GROUP_NAMES = [
    "Summer Bootcamp 2026", "Grad Chat", "Carnival Crew", "Randos",
    "Founders Ad Hoc", "Study Group", "Nikhil's Birthday", "The gang",
    "Trivia Night",
]
IMESSAGE_BLOCKED_REASON = (
    "chat.db read requires Full Disk Access. To fill this in: "
    "System Settings -> Privacy & Security -> Full Disk Access -> add Terminal "
    "(or whichever app runs the pull) -> then run: "
    "sqlite3 ~/Library/Messages/chat.db "
    "\"SELECT datetime(date/1000000000 + 978307200,'unixepoch','localtime'), "
    "text FROM message WHERE text IS NOT NULL ORDER BY date DESC LIMIT 40;\""
)
