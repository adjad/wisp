"""Round-4 family ``lookup_answers_r4``: read the user's own notes, saved facts and mail, then state the facts.

Evidence: toolbench 20261102 Mac gate, r3 vs untuned. r3 lost 13 points on notes and 5 on memory, and its
answers dropped the fact ("I found a note" with no list, "you have some mail" with no sender). The r4 plan
(training/ask-lora-r4/PLAN.md, Stage 1 B) asks for finals that carry every fact the question asks for, because
the thought that precedes a final now lists those facts from the tool result.

What this family teaches (every turn is one lookup):

notes
- ``search_notes(query=<the word the note would hold>)``. This registry has no read_note tool and the search
  result already carries the whole body, so one search is the whole job; when the user's own words miss,
  one shorter word is tried before giving up.
- the final gives exactly what was asked, from the note's text: the list, the code, the date, the name;
- a typo in the question ("recipie", "pasword hint") is corrected in the query;
- a topic no note holds is said plainly ("No note mentions your lease");
- two notes match: both are named and the answer comes from the one that fits;
- a note that lacks the asked fact says what it does cover.

memory and profile
- ``recall(query=<a word the fact would hold>)`` (``recall()`` with no query lists everything stored);
- the final states every stored fact the question asks for, in the second person, in the result's own words;
- "what do you know about me" is a short, gentle summary of the stored facts;
- nothing stored: said plainly, with an offer to remember it and no write;
- positive control: an explicit "remember that ..." runs ``remember`` once.

mail
- counts: ``summarize_emails`` with the numbers exactly as the digest printed them. With ``unread=true`` the
  digest counts only unread mail; without it the header reads "N emails · M unread", and M of N must not turn
  into "N unread";
- "did <office> email me" and "what did <person> say about <topic>": ``view_emails(query=...)``, then sender,
  subject and the asked detail from the body; the newest mail wins when two disagree; mail without the detail
  or no mail at all is said plainly.

All wordings are new: none matches a toolbench case, a round-1 eval question or a chat-eval prompt (leak.py).
Every fact a final states is asserted at build time to sit in the tool result the scripted step returns.
"""
from __future__ import annotations

import datetime as dt

import gen
import harness as H
from gen import D
from harness import Call, Final
from replies import clock, day_word, join, plural, with_others
from scen import Scenario, setup
from scen_r3_safe import _remember_world
from world import Email, Note

FAMILY = "lookup_answers_r4"
WRITE_ALLOW_R4 = {FAMILY: frozenset({"remember"})}

LINDEN = "sam.rivera@linden.edu"

_FIRST: dict = {}


def _fw(text):
    words = text.replace("“", "").replace('"', "").split()
    return words[0].strip(",.:;!?'").lower() if words else ""


def fin(rng, *opts, cap=3):
    """One of the candidate finals, preferring a first word this family has used fewer than `cap` times."""
    ok = [o for o in opts if _FIRST.get(_fw(o), 0) < cap]
    o = rng.choice(ok or list(opts))
    _FIRST[_fw(o)] = _FIRST.get(_fw(o), 0) + 1
    return o


def _sc(prompt, now, w, steps, notes, **kw):
    s = Scenario(FAMILY, prompt, now, w, steps, notes)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _chk(w, tool, args, final, must, tag):
    """Build-time proof that every fact in `must` is in the tool result and in the final, in the same form."""
    text = w.handle(tool, dict(args))
    low, fl = text.lower(), final.lower()
    for m in must:
        assert m.lower() in low, f"{tag}: {m!r} is not in the {tool} result:\n{text[:600]}"
        assert m.lower() in fl, f"{tag}: {m!r} is not in the final: {final!r}"
    return text


def _day(now, k):
    d = (now + D(days=k)).date()
    return f"{d:%A, %B} {d.day}"


# =================================================================================== notes
# (title, folder, body, query, [(prompt, final)...], [must...])
NOTES = [
    ("Mushroom risotto", "Recipes",
     "1.5 cups arborio rice, 8 oz cremini mushrooms, 1 shallot, 4 cups warm stock, 1/2 cup parmesan.\nAdd the stock a ladle at a time and stir for about 25 minutes.",
     "risotto",
     [("what goes in the risotto recipie", "The risotto note lists 1.5 cups arborio rice, 8 oz cremini mushrooms, 1 shallot, 4 cups warm stock and 1/2 cup parmesan. You add the stock a ladle at a time and stir for about 25 minutes."),
      ("how long do i stir the risotto for", "About 25 minutes, adding the warm stock a ladle at a time. That's from your mushroom risotto note.")],
     [["1.5 cups arborio rice", "cremini mushrooms", "1/2 cup parmesan", "25 minutes"], ["25 minutes", "a ladle at a time"]]),
    ("Apartment wifi", "Home",
     "Network: Maple-5G\nPassword hint: first pet's name plus the year you moved in.\nIf it drops, hold the router's reset button for 10 seconds.",
     "wifi",
     [("whats the pasword hint for the apartment wifi", "Your wifi note says the password hint is your first pet's name plus the year you moved in. The network is Maple-5G."),
      ("which network name did i save for home internet", "The network is called Maple-5G. Your apartment wifi note also says to hold the router's reset button for 10 seconds if it drops.")],
     [["first pet's name plus the year you moved in", "Maple-5G"], ["Maple-5G", "reset button for 10 seconds"]]),
    ("Rec Center locker", "Home",
     "Locker 112 on the second level. Combination 14-27-9. Towels are at the front desk.",
     "locker",
     [("whats the combo for my rec center locker", "Your locker is 112 on the second level, and the combination is 14-27-9."),
      ("where do i get towels at the gym, did i write that", "Yes, your Rec Center locker note says towels are at the front desk.")],
     [["112", "14-27-9"], ["towels are at the front desk"]]),
    ("Dentist visit prep", "Health",
     "Cleaning on December 4 at 2:30 PM with Dr. Alvarez. Bring the insurance card and the signed X-ray release form.",
     "dentist",
     [("when's the dentist thing in my notes", "Your dentist note has the cleaning on December 4 at 2:30 PM with Dr. Alvarez."),
      ("what do i bring to the dentist", "Bring your insurance card and the signed X-ray release form, according to your dentist visit note.")],
     [["December 4 at 2:30 PM", "Dr. Alvarez"], ["insurance card", "signed X-ray release form"]]),
    ("Sci-fi to read", "Books",
     "- Piranesi\n- A Memory Called Empire\n- The Dispossessed\n- Binti",
     "sci-fi",
     [("which sci-fi books did i save", "You saved four sci-fi books:\n- Piranesi\n- A Memory Called Empire\n- The Dispossessed\n- Binti"),
      ("is binti on my reading list", "Yes, Binti is on your sci-fi reading list, along with Piranesi, A Memory Called Empire and The Dispossessed.")],
     [["Piranesi", "A Memory Called Empire", "The Dispossessed", "Binti"], ["Binti", "Piranesi", "The Dispossessed"]]),
    ("Bike maintenance", "Home",
     "Chain lube every 200 miles. Tire pressure: front 80 psi, rear 85 psi. Next full service in January at Spoke & Cog.",
     "tire",
     [("tire pressure for the bike?", "Front 80 psi and rear 85 psi, per your bike maintenance note."),
      ("where am i taking the bike for its service and when", "Your bike note says the next full service is in January at Spoke & Cog.")],
     [["front 80 psi", "rear 85 psi"], ["January", "Spoke & Cog"]]),
    ("Fernbrook interview prep", "Career",
     "Interviewer: Renata Szabo. Questions to ask: team size, on-call, mentorship. Bring a laptop and photo ID.",
     "interview",
     [("who's interviewing me at fernbrook, check my notes", "Renata Szabo, according to your Fernbrook interview prep note. It also says to bring a laptop and photo ID."),
      ("what questions did i plan to ask at the interview", "Your interview prep note lists three: team size, on-call and mentorship.")],
     [["Renata Szabo", "laptop", "photo ID"], ["team size", "on-call", "mentorship"]]),
    ("Car insurance", "Money",
     "Policy HR-77120. Renews June 1. Agent: Dolores at Cedar Mutual, (503) 555-0177.",
     "policy",
     [("what's my insurance policy number", "Your policy number is HR-77120, and it renews June 1."),
      ("who's my insurance agent and how do i reach her", "Your agent is Dolores at Cedar Mutual, and her number is (503) 555-0177.")],
     [["HR-77120", "June 1"], ["Dolores", "Cedar Mutual", "(503) 555-0177"]]),
    ("Oregon coast trip", "Travel",
     "Stops: Cannon Beach, Yachats, Newport aquarium. Book the cabin by November 20, $140 a night.",
     "coast",
     [("which places was i planning to hit on the coast trip", "Your coast trip note lists Cannon Beach, Yachats and the Newport aquarium."),
      ("how much was that cabin for the coast, and when do i have to book", "The cabin is $140 a night, and your note says to book it by November 20.")],
     [["Cannon Beach", "Yachats", "Newport aquarium"], ["$140 a night", "November 20"]]),
    ("Project 2 checklist", "School",
     "Left to do: fix the scheduler bug, write the README, run valgrind. Submit by Friday 11:59 PM.",
     "valgrind",
     [("what's still open on my project 2 checklist", "Three things: fix the scheduler bug, write the README and run valgrind. It's due Friday at 11:59 PM."),
      ("when's project 2 due, per my own note", "Your checklist says to submit by Friday 11:59 PM.")],
     [["fix the scheduler bug", "write the README", "run valgrind", "Friday"], ["Friday 11:59 PM"]]),
    ("Monthly budget", "Money",
     "Rent $1,150. Utilities $85. Phone $40. Groceries $220. Savings goal $150.",
     "budget",
     [("how much is rent in my budget note", "Rent is $1,150 a month in your budget note."),
      ("break down my monthly budget for me", "Your budget note has rent at $1,150, utilities $85, phone $40 and groceries $220, with a savings goal of $150.")],
     [["$1,150"], ["$1,150", "$85", "$40", "$220", "$150"]]),
    ("Advising meeting", "School",
     "Dr. Nguyen: take CS 340 in spring. Drop PHIL 110 only if the grade is under a C. Credit cap is 17.",
     "advising",
     [("what did my advisor say about spring classes", "Dr. Nguyen suggested CS 340 for spring, said to drop PHIL 110 only if the grade falls under a C, and noted a credit cap of 17."),
      ("whats the credit limit dr nguyen mentioned", "The credit cap is 17, from your advising meeting note.")],
     [["CS 340", "PHIL 110", "17"], ["17"]]),
    ("Plant care", "Home",
     "Pothos: water every 10 days. Fern: mist twice a week. Cactus: once a month.",
     "pothos",
     [("how often does the pothos get watered", "Every 10 days, according to your plant care note."),
      ("remind me how to look after the fern and the cactus", "Mist the fern twice a week and water the cactus once a month.")],
     [["every 10 days"], ["twice a week", "once a month"]]),
    ("Chore split", "Home",
     "Maya: dishes Mon/Wed/Fri. Me: trash and bathroom on Saturday. We alternate vacuuming every other Sunday.",
     "chore",
     [("which days is maya on dishes", "Maya does the dishes on Mon/Wed/Fri, per your chore split note."),
      ("what are my chores", "You have the trash and bathroom on Saturday, and you and Maya alternate vacuuming every other Sunday.")],
     [["Mon/Wed/Fri"], ["trash and bathroom on Saturday", "every other Sunday"]]),
    ("Scholarship essay", "School",
     "Prompt: a time you changed your mind. Limit 650 words. Due January 12. Recommender: Prof. Okafor.",
     "scholarship",
     [("whats the word limit on my scholarship essay", "650 words, according to your scholarship essay note. It's due January 12."),
      ("who is writing the recommendation for the essay, i put it in my notes", "Prof. Okafor is your recommender, per your scholarship essay note.")],
     [["650 words", "January 12"], ["Prof. Okafor"]]),
    ("Climbing gym costs", "Notes",
     "Day pass $18. Shoe rental $5. Open until 10 PM Monday to Friday. Belay class Sundays at 11 AM.",
     "belay",
     [("how much does a day pass cost at the climbing gym, per my notes", "A day pass is $18, and shoe rental is another $5."),
      ("what day is the belay class at the climbing gym, check my notes", "Sundays at 11 AM, according to your climbing gym costs note.")],
     [["$18", "$5"], ["Sundays at 11 AM"]]),
]

# distractor notes that never match the queries above
OTHER = [
    ("Podcast ideas", "Notes", "Interview a TA about study habits; a short one on cheap meal prep."),
    ("Gym playlist", "Notes", "Warmup 120 BPM, intervals 160 BPM, cooldown something slow."),
    ("Thesis topics brainstorm", "School", "Schedulers in tiny kernels; caching for build tools."),
    ("Shopping later", "Notes", "Desk lamp, USB-C hub, a better water bottle."),
    ("Weekend hikes", "Travel", "Dog Mountain, Angel's Rest, Wahkeena Falls loop."),
    ("Quotes I like", "Notes", "Make it work, make it right, make it fast."),
]


def _other(rng, n, avoid=()):
    pool = [o for o in OTHER if o[0] not in avoid]
    rng.shuffle(pool)
    return pool[:n]


def _notes_world(rng, target, extras=(), hours=(20, 400)):
    """setup + notes: `target` (title, folder, body) first, then `extras`, then a few unrelated ones."""
    now, w = setup(rng)
    rows = [target] + list(extras)
    notes = []
    for i, (title, folder, body) in enumerate(rows):
        notes.append(Note(title, body, now - D(hours=rng.randint(*hours) + i * 30, minutes=rng.randint(0, 59)), folder))
    for title, folder, body in _other(rng, rng.choice([2, 3]), avoid={t[0] for t in rows}):
        notes.append(Note(title, body, now - D(hours=rng.randint(*hours) + 200, minutes=rng.randint(0, 59)), folder))
    w.notes = notes
    return now, w


def fam_notes(rng, out):
    # ------------------------------------------------------------------ 1. the exact content asked for
    for ni, (title, folder, body, q, pairs, musts) in enumerate(NOTES):
        for j, (prompt, final) in enumerate(pairs):
            now, w = _notes_world(rng, (title, folder, body))
            _chk(w, "search_notes", {"query": q}, final, musts[j], f"notes {title} {j}")
            out.append(_sc(prompt, now, w, [[Call("search_notes", query=q)], Final(final)],
                           "note lookup: search_notes with the word the note holds (typos in the question fixed), "
                           "then exactly the list, code, date or name asked for"))

    # ------------------------------------------------------------------ 2. no note holds the topic
    missing = [
        ("did i write anything about my lease?", "lease",
         ["No note mentions your lease.", "None of your notes mention a lease."]),
        ("is there a note on kayaking somewhere", "kayak",
         ["I don't see a note about kayaking. None of your notes mention it.", "No note mentions kayaking."]),
        ("what did i jot down about the spanish course", "spanish",
         ["No note mentions a Spanish course.", "I can't find a Spanish note. None of your notes mention it."]),
        ("find my notes on the car loan", "loan",
         ["None of your notes mention a car loan.", "No note mentions a loan."]),
        ("check notes: what's my passport expiry", "passport",
         ["No note mentions your passport, so I don't have an expiry date for it.", "None of your notes mention a passport."]),
        ("any note about tax stuff?", "tax",
         ["No note mentions taxes.", "I don't see a note about taxes. None of your notes mention them."]),
    ]
    for i, (prompt, q, finals) in enumerate(missing):
        now, w = setup(rng)
        w.notes = [Note(t, b, now - D(hours=rng.randint(20, 300)), f) for t, f, b in _other(rng, 3)] \
            + [Note("Plant care", NOTES[12][2], now - D(days=rng.randint(2, 20)), "Home")]
        final = fin(rng, *finals)
        text = w.handle("search_notes", {"query": q})
        assert "No note matches" in text, (q, text[:200])
        out.append(_sc(prompt, now, w, [[Call("search_notes", query=q)], Final(final)],
                       "no note holds the topic: say so in one plain line, invent nothing"))

    # ------------------------------------------------------------------ 3. the user's wording misses, one shorter word hits
    retries = [
        ("what did i write for the soup recipe", "soup recipe", "soup", "Red lentil soup", "Recipes",
         "2 cups red lentils, 1 onion, 3 cloves garlic, 1 tsp cumin, 5 cups water. Simmer 25 minutes.",
         "Your red lentil soup note lists 2 cups red lentils, 1 onion, 3 cloves garlic, 1 tsp cumin and 5 cups water, simmered for 25 minutes.",
         ["2 cups red lentils", "3 cloves garlic", "25 minutes"]),
        ("whats the dorm room number in my notes", "dorm room", "dorm", "Dorm move-in", "Home",
         "Hartwell Hall, room 418. Move-in is August 22 at 9 AM. Parking pass pickup at the front office.",
         "Your dorm move-in note has Hartwell Hall, room 418, with move-in on August 22 at 9 AM.",
         ["Hartwell Hall", "room 418", "August 22 at 9 AM"]),
        ("pull up what i saved about the hackathon team", "hackathon team", "hackathon", "Hackathon plan", "Career",
         "Team: Jordan (backend), Aisha (design), me (frontend). Demo Saturday at 4 PM in the Student Union.",
         "Your hackathon note has Jordan on the backend, Aisha on design and you on the frontend, with the demo Saturday at 4 PM in the Student Union.",
         ["Jordan", "Aisha", "Saturday at 4 PM", "Student Union"]),
    ]
    for prompt, q1, q2, title, folder, body, final, must in retries:
        now, w = _notes_world(rng, (title, folder, body))
        t1 = w.handle("search_notes", {"query": q1})
        assert "No note matches" in t1, t1[:200]
        _chk(w, "search_notes", {"query": q2}, final, must, f"retry {title}")
        out.append(_sc(prompt, now, w, [[Call("search_notes", query=q1)], [Call("search_notes", query=q2)], Final(final)],
                       "the full phrase finds nothing: one shorter word before answering"))

    # ------------------------------------------------------------------ 4. two notes match: name both, answer from the right one
    pairs = [
        ("which wifi network did i write down for the apartment", "wifi",
         ("Apartment wifi", "Home", NOTES[1][2]),
         ("Old dorm wifi", "Home", "Network: Linden-Guest\nPassword hint: the dorm room number twice.\nThis was the dorm router; ignore it now."),
         "Two notes mention wifi, “Apartment wifi” and “Old dorm wifi”. The apartment one is the right one, and it lists the network Maple-5G.",
         ["Apartment wifi", "Old dorm wifi", "Maple-5G"]),
        ("what do i need to bring to the fernbrook interview", "fernbrook",
         ("Fernbrook interview prep", "Career", NOTES[6][2]),
         ("Internship apps", "Career", "Fernbrook Labs: applied, interview pending\nHarborline: applied Oct 2\nNimbus Data: need referral"),
         "“Fernbrook interview prep” says to bring a laptop and photo ID. “Internship apps” also mentions Fernbrook, but only that your interview is pending.",
         ["Fernbrook interview prep", "Internship apps", "laptop", "photo ID"]),
        ("check my notes: how much parmesan for the mushroom risotto", "mushroom",
         ("Mushroom risotto", "Recipes", NOTES[0][2]),
         ("Mushroom foraging", "Travel", "Chanterelles near the creek after rain. Bring a paper bag and a small knife."),
         "Your “Mushroom risotto” note calls for 1/2 cup parmesan. “Mushroom foraging” also matched, but it's about picking them, not cooking.",
         ["Mushroom risotto", "Mushroom foraging", "1/2 cup parmesan"]),
        ("what do my notes say about the bike service at the shop", "bike",
         ("Bike maintenance", "Home", NOTES[5][2]),
         ("Bike lock combo", "Home", "Lock is the green U-lock. Combination 3-1-8-2. Spare key is with Maya."),
         "Your “Bike maintenance” note puts the next full service in January at Spoke & Cog. “Bike lock combo” also matched, but it only covers the lock.",
         ["Bike maintenance", "Bike lock combo", "January", "Spoke & Cog"]),
    ]
    for prompt, q, a, b, final, must in pairs:
        now, w = _notes_world(rng, a, [b])
        text = _chk(w, "search_notes", {"query": q}, final, must, f"two {q}")
        assert text.count("Title:") == 2, text[:300]
        out.append(_sc(prompt, now, w, [[Call("search_notes", query=q)], Final(final)],
                       "two notes match: name both, answer from the one that fits the question"))

    # ------------------------------------------------------------------ 5. browsing without a topic
    for prompt, k in [("show me my recent notes by title", 4), ("what have i been writing down lately, just the titles", 3)]:
        now, w = setup(rng)
        picks = rng.sample(NOTES, k)
        w.notes = [Note(t, b, now - D(hours=10 + 30 * i, minutes=rng.randint(0, 59)), f) for i, (t, f, b, *_r) in enumerate(picks)]
        titles = [f"“{t}”" for t, *_r in picks]
        final = f"Your most recent notes are {join(titles)}."
        _chk(w, "search_notes", {}, final, [t for t, *_r in picks], "browse")
        out.append(_sc(prompt, now, w, [[Call("search_notes")], Final(final)],
                       "no topic named: search_notes with no query, then the titles it returned"))

    # ------------------------------------------------------------------ 6. note found, the asked fact is not in it
    nofact = [
        ("does my plant note say anything about fertilizer", "plant", NOTES[12][:3],
         "Your plant care note covers watering the pothos, misting the fern and watering the cactus, but it doesn't mention fertilizer.",
         ["Pothos", "fern", "cactus"]),
        ("did i write down the insurance deductible", "insurance", NOTES[7][:3],
         "Your car insurance note has the policy number HR-77120, the June 1 renewal and your agent Dolores, but no deductible.",
         ["HR-77120", "June 1", "Dolores"]),
        ("which hotel was i going to use on the coast", "coast", NOTES[8][:3],
         "Your coast trip note lists the stops and the $140 a night cabin to book by November 20, but no hotel.",
         ["$140 a night", "November 20"]),
    ]
    for prompt, q, (title, folder, body), final, must in nofact:
        now, w = _notes_world(rng, (title, folder, body))
        _chk(w, "search_notes", {"query": q}, final, must, f"nofact {q}")
        out.append(_sc(prompt, now, w, [[Call("search_notes", query=q)], Final(final)],
                       "the note exists but lacks the asked fact: say what it does hold, never guess"))


# =================================================================================== memory
# Stored fact texts are written the way recall prints them (no name, no third person). The final restates them in
# the second person. Query words must all occur in a memory (production recall needs every term).
MEM_OTHER = ["Takes the 7:40 bus to campus on weekdays.", "Usual coffee order: flat white with oat milk, no sugar.",
             "Prefers morning study sessions at Hollis Library.", "Advisor is Dr. Nguyen, office in Mercer Hall 220.",
             "Runs with the Saturday club at Riverside Track.", "Keeps a spare key with the neighbor in 3B."]

# (prompt, query, [stored facts that match], final(s), [must])
MEM = [
    ("remind me what i'm allergic to", "allergic", ["Allergic to shellfish and tree nuts; carries an EpiPen."],
     ["You're allergic to shellfish and tree nuts, and you carry an EpiPen.",
      "Shellfish and tree nuts. You carry an EpiPen for it."], ["shellfish", "tree nuts", "EpiPen"]),
    ("what's my allergy situation again", "allergy", ["Has a cat allergy; takes cetirizine in spring.", "Mild dust allergy in the apartment."],
     ["You have a cat allergy, which you manage with cetirizine in spring, and a mild dust allergy at the apartment."],
     ["cat allergy", "cetirizine", "dust allergy"]),
    ("which medicines can't i take", "allergic", ["Allergic to ibuprofen (hives). Uses acetaminophen instead."],
     ["You're allergic to ibuprofen, which gives you hives, so you use acetaminophen instead."], ["ibuprofen", "hives", "acetaminophen"]),
    ("who am i working for these days", "work", ["Works part-time at the Hollis Library help desk, Tuesday and Thursday evenings."],
     ["You work part-time at the Hollis Library help desk, on Tuesday and Thursday evenings.",
      "At the Hollis Library help desk, part-time, on Tuesday and Thursday evenings."],
     ["Hollis Library help desk", "Tuesday and Thursday evenings"]),
    ("where am i interning this summer", "internship", ["Summer internship: Fernbrook Labs, starts June 8, mentor is Renata Szabo."],
     ["You're interning at Fernbrook Labs starting June 8, and Renata Szabo is your mentor."], ["Fernbrook Labs", "June 8", "Renata Szabo"]),
    ("what jobs do i have going", "job", ["Job: part-time at the Hollis Library help desk.", "Second job: TA for CS 162, grades on Mondays."],
     ["You have two: part-time at the Hollis Library help desk, and TA for CS 162, where you grade on Mondays."],
     ["Hollis Library help desk", "TA for CS 162", "Mondays"]),
    ("what's my full name on file", "name", ["Name: Sam Rivera, goes by Sam."],
     ["Your name is Sam Rivera, and you go by Sam.", "Sam Rivera, though you go by Sam."], ["Sam Rivera", "Sam"]),
    ("how do you address me", "called", ["Prefers to be called Sam, not Samuel."],
     ["You'd like to be called Sam rather than Samuel."], ["Sam", "Samuel"]),
    ("what did i say my goals are this term", "goals", ["Goals this semester: an A in Operating Systems, finish project 2 a week early, and make the running club twice a week."],
     ["This semester you want an A in Operating Systems, to finish project 2 a week early, and to make the running club twice a week."],
     ["an A in Operating Systems", "project 2 a week early", "running club twice a week"]),
    ("what am i aiming for this semester", "goal", ["Goal: finish the lab writeup before Wednesday each week.", "Goal: be in bed by 11 PM on weeknights."],
     ["You're aiming to finish the lab writeup before Wednesday each week and to be in bed by 11 PM on weeknights."],
     ["lab writeup before Wednesday", "bed by 11 PM"]),
    ("remind me of my study goals", "goals", ["Study goals: two practice exams a week for the OS midterm, and office hours every Thursday."],
     ["Your study goals are two practice exams a week for the OS midterm and office hours every Thursday."],
     ["two practice exams a week", "office hours every Thursday"]),
    ("which bus do i take to campus", "bus", ["Takes the 7:40 bus to campus on weekdays."],
     ["You take the 7:40 bus to campus on weekdays."], ["7:40 bus", "weekdays"]),
    ("when do i usually wake up", "wakes", ["Wakes at 6:30 AM on weekdays and 8 AM on weekends."],
     ["You wake at 6:30 AM on weekdays and 8 AM on weekends."], ["6:30 AM on weekdays", "8 AM on weekends"]),
    ("where did i say i grew up", "grew up", ["Grew up in Eugene, Oregon."],
     ["You grew up in Eugene, Oregon."], ["Eugene, Oregon"]),
    ("what's my usual coffee order", "coffee", ["Usual coffee order: flat white with oat milk, no sugar."],
     ["A flat white with oat milk and no sugar."], ["flat white", "oat milk", "no sugar"]),
    ("who is my advisor, and where is the office", "advisor", ["Advisor is Dr. Nguyen, office in Mercer Hall 220."],
     ["Your advisor is Dr. Nguyen, and the office is in Mercer Hall 220."], ["Dr. Nguyen", "Mercer Hall 220"]),
    ("how do maya and i split rent", "rent", ["Rent is split 50/50 with Maya, due on the 1st."],
     ["You and Maya split rent 50/50, and it's due on the 1st."], ["50/50", "Maya", "the 1st"]),
    ("what am i studying", "major", ["Major: computer science, sophomore at Linden University."],
     ["Computer science. You're a sophomore at Linden University."], ["computer science", "sophomore", "Linden University"]),
    ("who's my emergency contact", "emergency", ["Emergency contact: Carmen Rivera, (503) 555-0163."],
     ["Your emergency contact is Carmen Rivera, at (503) 555-0163."], ["Carmen Rivera", "(503) 555-0163"]),
    ("where is my spare key", "spare key", ["Keeps a spare key with the neighbor in 3B."],
     ["Your spare key is with the neighbor in 3B."], ["neighbor in 3B"]),
    ("what do i usually do on saturdays", "saturday", ["Runs with the Saturday club at Riverside Track at 8 AM."],
     ["You run with the Saturday club at Riverside Track at 8 AM."], ["Saturday club", "Riverside Track", "8 AM"]),
    ("what have i told you about how i study", "study", ["Prefers morning study sessions at Hollis Library.", "Study goals: two practice exams a week."],
     ["You prefer morning study sessions at Hollis Library, and you aim for two practice exams a week."],
     ["morning study sessions at Hollis Library", "two practice exams a week"]),
]

# whole-profile stores for "what do you know about me": (facts, final, must)
ABOUT = [
    (["Name: Sam Rivera, goes by Sam.", "Major: computer science, sophomore at Linden University.",
      "Works part-time at the Hollis Library help desk.", "Allergic to shellfish."],
     "You're Sam Rivera, a computer science sophomore at Linden University. You work part-time at the Hollis Library help desk, and you're allergic to shellfish.",
     ["Sam Rivera", "computer science", "Hollis Library help desk", "shellfish"]),
    (["Prefers to be called Sam.", "Goals this semester: an A in Operating Systems and a summer internship.",
      "Takes the 7:40 bus to campus on weekdays.", "Usual coffee order: flat white with oat milk."],
     "You like to be called Sam. This semester you're after an A in Operating Systems and a summer internship, you take the 7:40 bus on weekdays, and you usually order a flat white with oat milk.",
     ["Sam", "an A in Operating Systems", "summer internship", "7:40 bus", "flat white with oat milk"]),
    (["Rent is split 50/50 with Maya, due on the 1st.", "Runs with the Saturday club at Riverside Track.",
      "Advisor is Dr. Nguyen.", "Cat allergy; takes cetirizine in spring."],
     "You split rent 50/50 with Maya, and you run with the Saturday club at Riverside Track. Dr. Nguyen is your advisor, and you have a cat allergy.",
     ["50/50 with Maya", "Saturday club at Riverside Track", "Dr. Nguyen", "cat allergy"]),
    (["Major: computer science.", "Summer internship: Fernbrook Labs, starts June 8.", "Emergency contact: Carmen Rivera."],
     "You study computer science, you have a summer internship at Fernbrook Labs starting June 8, and Carmen Rivera is your emergency contact.",
     ["computer science", "Fernbrook Labs", "June 8", "Carmen Rivera"]),
    (["Works part-time at the Hollis Library help desk.", "Prefers morning study sessions at Hollis Library.",
      "Birthday is March 14."],
     "Not a huge amount yet. You work part-time at the Hollis Library help desk, you like morning study sessions there, and your birthday is March 14.",
     ["Hollis Library help desk", "morning study sessions", "March 14"]),
    (["Name: Sam Rivera.", "Allergic to ibuprofen (hives).", "Keeps a spare key with the neighbor in 3B."],
     "You're Sam Rivera, you're allergic to ibuprofen, and you keep a spare key with the neighbor in 3B.",
     ["Sam Rivera", "ibuprofen", "neighbor in 3B"]),
]
ABOUT_PROMPTS = ["so what have you got on me", "tell me what you've learned about me so far", "what's saved about me?",
                 "give me a quick rundown of what you remember about me", "how much do you actually know about me",
                 "sum up what you have stored about me in a sentence or two"]

UNKNOWN = [
    ("what's my shoe size", "shoe size", "your shoe size"),
    ("remind me of my favorite restaurant", "restaurant", "a favorite restaurant"),
    ("what's my dentist's name", "dentist", "your dentist's name"),
    ("do you have my mom's birthday saved", "birthday", "your mom's birthday"),
    ("what's my home address", "address", "your home address"),
]
UNKNOWN_FINALS = [
    "I don't have {x} saved. If you tell me, I'll remember it.",
    "{X} isn't something I have stored. Tell me and I'll keep it for next time.",
    "Nothing saved about {x} yet. I can remember it if you tell me.",
    "I don't have {x} yet. Tell me, and I'll hold onto it.",
    "I haven't been told {x}, so there's nothing to give you. I can save it if you share it.",
]

REMEMBER = [
    ("please remember i don't eat pork", "I don't eat pork", 81, "Noted, you don't eat pork."),
    ("keep in mind that i'm left-handed", "I'm left-handed", 82, "I'll remember that you're left-handed."),
    ("remember my emergency contact is carmen", "My emergency contact is Carmen", 83, "Got it, your emergency contact is Carmen."),
    ("note for later: i prefer aisle seats on flights", "I prefer aisle seats on flights", 84, "I'll remember that you prefer aisle seats."),
    ("remember that my rent is due on the 1st", "My rent is due on the 1st", 85, "Noted, your rent is due on the 1st."),
]


def fam_memory(rng, out):
    # ------------------------------------------------------------------ 1. the stored fact, asked for by topic
    for i, (prompt, q, facts, finals, must) in enumerate(MEM):
        now, w = setup(rng)
        extra = [m for m in rng.sample(MEM_OTHER, 3) if not any(t.lower() in m.lower() for t in q.split())]
        w.memories = list(facts) + extra[:2]
        rng.shuffle(w.memories)
        final = fin(rng, *finals)
        text = _chk(w, "recall", {"query": q}, final, must, f"mem {prompt}")
        assert text.count("[memory") == len(facts), (q, text)
        out.append(_sc(prompt, now, w, [[Call("recall", query=q)], Final(final)],
                       "a question about a stored fact: recall with a word the fact holds, then every stored fact asked for"))

    # ------------------------------------------------------------------ 2. what do you know about me
    for i, (facts, final, must) in enumerate(ABOUT):
        now, w = setup(rng)
        w.memories = list(facts)
        _chk(w, "recall", {}, final, must, f"about {i}")
        out.append(_sc(ABOUT_PROMPTS[i], now, w, [[Call("recall")], Final(final)],
                       "'what do you know about me': recall with no query lists the stored facts; a short gentle summary of them"))

    # ------------------------------------------------------------------ 3. nothing stored: say so, offer to remember, no write
    for i, (prompt, q, x) in enumerate(UNKNOWN):
        now, w = setup(rng)
        w.memories = rng.sample(MEM_OTHER, 3)
        final = fin(rng, *[t.format(x=x, X=x[:1].upper() + x[1:]) for t in UNKNOWN_FINALS])
        assert "No matching memory" in w.handle("recall", {"query": q})
        out.append(_sc(prompt, now, w, [[Call("recall", query=q)], Final(final)],
                       "nothing stored for that: say so plainly and offer to remember it, no write"))
    for prompt, final in [("give me the short version of what you know about me", "I don't have anything saved about you yet. Tell me a few things, like where you work or what you study, and I'll remember them."),
                          ("tell me everything you've got stored on me", "There's nothing stored about you yet. If you tell me things you'd like me to keep, I'll remember them.")]:
        now, w = setup(rng)
        w.memories = []
        assert "No matching memory" in w.handle("recall", {})
        out.append(_sc(prompt, now, w, [[Call("recall")], Final(final)],
                       "empty memory: say so plainly and offer to remember, no write"))

    # ------------------------------------------------------------------ 4. positive control: an explicit remember runs once
    for prompt, fact, mid, reply in REMEMBER:
        now, w = setup(rng)
        _remember_world(w, mid)
        out.append(_sc(prompt, now, w, [[Call("remember", fact=fact)], Final(reply)],
                       "an explicit 'remember': one remember call with the statement as said (positive control)",
                       expect_cards=[]))


# =================================================================================== mail
ORGS = [
    # (prompt, query, sender, addr, subject, body, final, must) ; {d} is a date a few days ahead
    ("did the registrar email me", "registrar", "Linden Registrar", "registrar@linden.edu", "Your advising hold was lifted",
     "The advising hold on your account was removed. Spring enrollment opens {d} at 8 AM.",
     "Yes. The Linden Registrar wrote “Your advising hold was lifted”: the hold is off your account, and spring enrollment opens {d} at 8 AM.",
     ["Linden Registrar", "Your advising hold was lifted", "{d} at 8 AM"]),
    ("anything from financial aid yet", "financial aid", "Linden Financial Aid", "finaid@linden.edu", "Spring disbursement scheduled",
     "Your spring aid of $3,200 will be disbursed on {d}. No action is needed.",
     "Yes. Linden Financial Aid sent “Spring disbursement scheduled”: $3,200 will be disbursed on {d}, and you don't need to do anything.",
     ["Linden Financial Aid", "Spring disbursement scheduled", "$3,200", "{d}"]),
    ("has the landlord answered about the heater", "heater", "Lakeview Property Mgmt", "office@lakeviewproperties.example", "Re: heater not working",
     "A technician will visit on {d} between 1 PM and 3 PM. Please leave the hallway door unlocked.",
     "Yes. Lakeview Property Mgmt replied to “Re: heater not working”: a technician comes on {d} between 1 PM and 3 PM, and the hallway door should be unlocked.",
     ["Lakeview Property Mgmt", "Re: heater not working", "{d}", "between 1 PM and 3 PM"]),
    ("did the airline send my seat assignment", "seat", "Cascade Air", "noreply@cascadeair.example", "Your seat is confirmed",
     "You are in seat 14C on the flight to Denver. Boarding begins 40 minutes before departure.",
     "Yes. Cascade Air confirmed “Your seat is confirmed”: you're in seat 14C on the flight to Denver, and boarding starts 40 minutes before departure.",
     ["Cascade Air", "Your seat is confirmed", "14C", "40 minutes"]),
    ("did the pharmacy text or email about the refill", "refill", "Pine Street Pharmacy", "rx@pinestreetpharmacy.example", "Refill ready",
     "Your refill is ready for pickup. Pickup hours are 9 AM to 7 PM, and it's held until {d}.",
     "Yes. Pine Street Pharmacy emailed “Refill ready”: it can be picked up between 9 AM and 7 PM and is held until {d}.",
     ["Pine Street Pharmacy", "Refill ready", "9 AM", "7 PM", "{d}"]),
    ("any reply from the bookstore about my return", "return", "Hollis Bookstore", "orders@hollisbookstore.example", "Return received",
     "We received your return of 1 item. Your refund of $42.50 goes back to your card in 3 to 5 business days.",
     "Yes. Hollis Bookstore sent “Return received”: your refund of $42.50 goes back to your card in 3 to 5 business days.",
     ["Hollis Bookstore", "Return received", "$42.50", "3 to 5 business days"]),
    ("did the career center confirm my resume review", "resume", "Linden Career Center", "careers@linden.edu", "Resume review confirmed",
     "Your resume review is booked for {d} at 11:15 AM in Mercer Hall 40 with Ms. Bell.",
     "Yes. Linden Career Center confirmed “Resume review confirmed”: {d} at 11:15 AM in Mercer Hall 40 with Ms. Bell.",
     ["Linden Career Center", "Resume review confirmed", "{d} at 11:15 AM", "Mercer Hall 40"]),
    ("did the gym reply about freezing my membership", "membership", "Basalt Climbing Gym", "hello@basaltclimbing.example", "Membership hold approved",
     "Your membership is on hold until {d}. No charges will be made while it is paused.",
     "Yes. Basalt Climbing Gym approved “Membership hold approved”: your membership is paused until {d}, with no charges while it is on hold.",
     ["Basalt Climbing Gym", "Membership hold approved", "{d}"]),
]

# (prompt, query, full name, addr, subject, body, final, must)
PEOPLE = [
    ("what did priya say about the deadline", "Priya", "Priya Nair", "pnair@linden.edu", "Poster deadline",
     "The poster deadline moved to {d} at noon. Send me your slides before 10 AM that day.",
     "Priya Nair's email “Poster deadline” says it moved to {d} at noon, and she wants your slides before 10 AM that day.",
     ["Priya Nair", "Poster deadline", "{d} at noon", "10 AM"]),
    ("what time did jordan say for the demo", "Jordan", "Jordan Ellis", "jellis@linden.edu", "Demo rehearsal",
     "Let's rehearse the demo at 6:30 PM in Kerr Hall 301. I'll bring the projector cable.",
     "Jordan wrote “Demo rehearsal”: 6:30 PM in Kerr Hall 301, and Jordan is bringing the projector cable.",
     ["Demo rehearsal", "6:30 PM", "Kerr Hall 301", "projector cable"]),
    ("did dana say whether the shift swap is ok", "Dana", "Dana Whitfield", "dwhitfield@linden.edu", "Shift swap",
     "The swap is fine. You take Thursday 4 to 8 PM and Theo takes your Tuesday.",
     "Yes, Dana's “Shift swap” email says it's fine: you take Thursday 4 to 8 PM and Theo takes your Tuesday.",
     ["Shift swap", "Thursday 4 to 8 PM", "Theo", "Tuesday"]),
    ("what's okafor want for the extension", "Okafor", "Prof. Adaeze Okafor", "aokafor@linden.edu", "Project 2 extension",
     "You can have until {d} at 11:59 PM. Include a short note on what changed.",
     "Prof. Okafor's “Project 2 extension” email gives you until {d} at 11:59 PM, and she wants a short note on what changed.",
     ["Project 2 extension", "{d} at 11:59 PM", "short note on what changed"]),
    ("what's aisha say about study group", "Aisha", "Aisha Karim", "akarim@linden.edu", "Study group moved",
     "Study group moved to 7 PM tonight in room 3B. Can you bring the linear algebra notes?",
     "Aisha's “Study group moved” email puts it at 7 PM tonight in room 3B, and she asks you to bring the linear algebra notes.",
     ["Study group moved", "7 PM", "room 3B", "linear algebra notes"]),
    ("what did hana write about my regrade", "Hana", "Hana Kowalczyk", "hkowalczyk@linden.edu", "Regrade result",
     "I added 3 points to question 4. Your homework 5 score is now 47 out of 50.",
     "Hana's “Regrade result” email says she added 3 points to question 4, so your homework 5 score is 47 out of 50.",
     ["Regrade result", "3 points", "question 4", "47 out of 50"]),
]

DISTRACT = [("LindenLearn", "notifications@linden.edu", "Announcement: PHIL 110 reading list"),
            ("DevDigest Weekly", "news@devdigest.example", "This week: build caching explained"),
            ("Trailhead Running Co.", "hello@trailheadrunning.example", "Your training plan for week 6"),
            ("Soundwave Premium", "billing@soundwave.example", "Your student plan renews soon")]


def _mail_world(rng, items, keep=()):
    now, w = setup(rng)
    mine = {i.sender for i in items}
    dis = [d for d in DISTRACT if d[0] not in mine]
    rng.shuffle(dis)
    extra = [Email(s, a, subj, now - D(hours=rng.randint(30, 90), minutes=rng.randint(0, 59)),
                   "Newsletter content.", rng.random() < 0.5, False) for s, a, subj in dis[:rng.choice([2, 3])]]
    for m in items:
        m.when = min(m.when, now - D(hours=2))
    w.emails = list(items) + extra
    return now, w


def _when(rng, now, lo=3, hi=40):
    return now - D(hours=rng.randint(lo, hi), minutes=rng.randint(0, 59))


def _digest(rows):
    people_ = [m for m in rows if m.person]
    news = [m for m in rows if not m.person]
    parts = []
    if people_:
        bits = [f"{m.sender} (“{m.subject}”)" for m in people_[:3]]
        parts.append("From people: " + join(bits) + ("" if len(people_) <= 3 else " and others") + ".")
    if news:
        parts.append("The rest are updates from " + with_others(sorted({m.sender for m in news}), 3).rstrip(".") + ".")
    return " ".join(parts)


def _counted_world(rng, u, people=None, news=None):
    """A world with exactly `u` unread emails; returns (now, w, all_rows_newest_first)."""
    for _ in range(40):
        now, w = setup(rng, people=people or rng.choice([2, 3]), news=news or rng.choice([3, 4, 5]))
        if len(w.emails) > u + 1:
            break
    for m in w.emails:
        m.when = min(m.when, now - D(minutes=40))
        m.unread = False
    idx = list(range(len(w.emails)))
    rng.shuffle(idx)
    for i in idx[:u]:
        w.emails[i].unread = True
    if u >= 1 and not any(m.unread and m.person for m in w.emails):
        pe = [m for m in w.emails if m.person]
        if pe:
            pe[0].unread = True
            others = [m for m in w.emails if m.unread and not m.person]
            if others and sum(m.unread for m in w.emails) > u:
                others[0].unread = False
    return now, w, sorted(w.emails, key=lambda m: m.when, reverse=True)


def fam_mail(rng, out):
    # ------------------------------------------------------------------ 1. counts: inbox total and unread, as the digest printed them
    total_prompts = ["what's piling up in my inbox, how many are still unopened", "number of emails i haven't opened yet?",
                     "how full is my inbox and how much of it is new", "count the mail i still haven't opened",
                     "give me the tally of my inbox, total and not yet opened"]
    for i, pr in enumerate(total_prompts):
        u = [3, 5, 2, 6, 4][i]
        now, w, rows = _counted_world(rng, u)
        n, un = len(rows), sum(m.unread for m in rows)
        text = w.handle("summarize_emails", {})
        assert f"{n} emails · {un} unread" in text, text[:300]
        unread_rows = [m for m in rows if m.unread]
        final = fin(rng,
                    f"{plural(un, 'email')} {'is' if un == 1 else 'are'} unread, out of {n} in your recent inbox. " + _digest(unread_rows),
                    f"You have {un} unread out of {n} emails in your recent inbox. " + _digest(unread_rows))
        out.append(_sc(pr, now, w, [[Call("summarize_emails")], Final(final)],
                       "total and unread in one digest: 'N emails · M unread' is M of N, never 'N unread'"))
    only_prompts = ["which emails are still unopened", "anything waiting in my mail that i haven't looked at?",
                    "quick, what new mail do i have", "unopened mail, who's it from"]
    for i, pr in enumerate(only_prompts):
        u = [4, 2, 5, 1][i]
        now, w, rows = _counted_world(rng, u)
        unread_rows = [m for m in rows if m.unread]
        un = len(unread_rows)
        text = w.handle("summarize_emails", {"unread": True})
        assert f"{un} emails · {un} unread" in text or un == 1, text[:300]
        lead = "You have one unread email. " if un == 1 else f"You have {un} unread emails. "
        final = lead + _digest(unread_rows)
        out.append(_sc(pr, now, w, [[Call("summarize_emails", unread=True)], Final(final)],
                       "unread mail: the count and who it is from, exactly as the digest printed them"))
    for pr in ["anything sitting in my inbox that i haven't opened?", "is there any unopened mail right now"]:
        now, w = setup(rng, people=2, news=3)
        for m in w.emails:
            m.unread = False
            m.when = min(m.when, now - D(minutes=30))
        w.overrides["summarize_emails"] = "No emails found in your recent inbox."
        final = fin(rng, "You have no unread emails right now.", "Nothing unread in your recent inbox.")
        out.append(_sc(pr, now, w, [[Call("summarize_emails", unread=True)], Final(final)],
                       "no unread mail: say it is empty"))

    # ------------------------------------------------------------------ 2. did <office> email me: sender, subject, asked detail
    for prompt, q, sender, addr, subj, body, final, must in ORGS:
        now, w = setup(rng)
        d = _day(now, rng.randint(2, 8))
        em = Email(sender, addr, subj, _when(rng, now), body.format(d=d), rng.random() < 0.6, False)
        now, w = _mail_world(rng, [em])
        f = final.format(d=d)
        m_ = [x.format(d=d) for x in must]
        _chk(w, "view_emails", {"query": q}, f, m_, f"org {sender}")
        out.append(_sc(prompt, now, w, [[Call("view_emails", query=q)], Final(f)],
                       "did an office write: view_emails by a word the user said, then the sender, subject and the details"))

    # ------------------------------------------------------------------ 3. what did <person> say about <topic>
    for prompt, q, full, addr, subj, body, final, must in PEOPLE:
        now, w = setup(rng)
        d = _day(now, rng.randint(2, 8))
        em = Email(full, addr, subj, _when(rng, now), body.format(d=d), True, True)
        now, w = _mail_world(rng, [em])
        f = final.format(d=d)
        m_ = [x.format(d=d) for x in must]
        _chk(w, "view_emails", {"query": q}, f, m_, f"person {full}")
        out.append(_sc(prompt, now, w, [[Call("view_emails", query=q)], Final(f)],
                       "what a person wrote about a topic: view_emails by their first name, then the asked detail from the body"))

    # two mails from one person: the newest wins, and the final says it replaced the earlier one
    pairs = [
        ("is the poster deadline still friday, per priya", "Priya", "Priya Nair", "pnair@linden.edu",
         ("Poster deadline", "The poster deadline is Friday at noon."),
         ("Poster deadline moved", "Sorry, the deadline moved to Monday at 9 AM."),
         "No. Priya's newest email, “Poster deadline moved”, says it moved to Monday at 9 AM. Her earlier one had Friday at noon.",
         ["Poster deadline moved", "Monday at 9 AM", "Friday at noon"]),
        ("where is jordan's group meeting now", "Jordan", "Jordan Ellis", "jellis@linden.edu",
         ("Lab group meeting", "We meet in room 3B at 5:45 PM."),
         ("Room change", "Room 3B is taken, so we meet in room 5A at 5:45 PM."),
         "It changed. Jordan's “Room change” email says room 5A at 5:45 PM, since room 3B is taken.",
         ["Room change", "room 5A at 5:45 PM", "room 3B"]),
    ]
    for prompt, q, full, addr, (s1, b1), (s2, b2), final, must in pairs:
        now, w = setup(rng)
        old = Email(full, addr, s1, now - D(days=rng.randint(3, 5), hours=rng.randint(0, 6)), b1, False, True)
        new = Email(full, addr, s2, now - D(hours=rng.randint(4, 30)), b2, True, True)
        now, w = _mail_world(rng, [new, old])
        _chk(w, "view_emails", {"query": q}, final, must, f"two mails {full}")
        out.append(_sc(prompt, now, w, [[Call("view_emails", query=q)], Final(final)],
                       "two mails disagree: the newest one wins and the final says the earlier detail changed"))

    # ------------------------------------------------------------------ 4. found, but the asked detail is not in it
    nodetail = [
        ("did priya give a new deadline", "Priya", "Priya Nair", "pnair@linden.edu", "Poster feedback",
         "Your poster looks great. I left a few comments on the layout in the shared folder.",
         "No new deadline. Priya's “Poster feedback” email only says the poster looks great and that she left layout comments in the shared folder.",
         ["Poster feedback", "layout"]),
        ("what time is the registrar's office open", "registrar", "Linden Registrar", "registrar@linden.edu", "Transcript request received",
         "We received your transcript request. It will be processed within 5 business days.",
         "The registrar's email doesn't give hours. “Transcript request received” only says your request will be processed within 5 business days.",
         ["Transcript request received", "5 business days"]),
    ]
    for prompt, q, full, addr, subj, body, final, must in nodetail:
        now, w = _mail_world(rng, [Email(full, addr, subj, setup(rng)[0], body, True, full.startswith("Priya"))])
        w.emails[0].when = now - D(hours=rng.randint(4, 40))
        _chk(w, "view_emails", {"query": q}, final, must, f"nodetail {full}")
        out.append(_sc(prompt, now, w, [[Call("view_emails", query=q)], Final(final)],
                       "the mail was found but does not hold the detail: say what it does say"))

    # ------------------------------------------------------------------ 5. no mail at all from them
    for prompt, q, final in [
        ("has the housing office emailed me back", "housing", "No. I don't see an email from the housing office in your recent mail."),
        ("what did priya say about the deadline, anything?", "Priya", "Nothing from Priya has come in, so there's no deadline to give you."),
        ("did the bursar email me a receipt", "bursar", "I don't see anything from the bursar in your recent mail."),
    ]:
        now, w = _mail_world(rng, [])
        assert "No emails matching" in w.handle("view_emails", {"query": q})
        out.append(_sc(prompt, now, w, [[Call("view_emails", query=q)], Final(final)],
                       "no mail from them: one plain line, nothing invented"))

    # ------------------------------------------------------------------ 6. a day-scoped lookup
    for prompt, q, full, addr, subj, body, final, must in [PEOPLE[1], PEOPLE[2]]:
        now, w = setup(rng)
        d = _day(now, 3)
        when = now.replace(hour=0, minute=0) - D(days=1) + D(hours=rng.randint(9, 20), minutes=rng.randint(0, 59))
        em = Email(full, addr, subj, when, body.format(d=d), True, True)
        _, w2 = _mail_world(rng, [em])
        w2.now = now
        em.when = when
        H.set_clock(now)
        f = final.format(d=d).rstrip(".") + f". It came {day_word(when.date(), now)} at {clock(when)}."
        m_ = [x.format(d=d) for x in must] + [clock(when)]
        _chk(w2, "view_emails", {"query": q, "day": "yesterday"}, f, m_, f"yesterday {full}")
        out.append(_sc(f"anything from {q.lower()} yesterday, what was it about", now, w2,
                       [[Call("view_emails", query=q, day="yesterday")], Final(f)],
                       "a day-scoped person lookup: query plus day, then the detail and when it arrived"))


def fam_lookup(rng):
    _FIRST.clear()
    out = []
    fam_notes(rng, out)
    fam_memory(rng, out)
    fam_mail(rng, out)
    rng.shuffle(out)     # neighbouring turns share no thought wording
    return out


FAMILIES_R4 = {FAMILY: fam_lookup}
