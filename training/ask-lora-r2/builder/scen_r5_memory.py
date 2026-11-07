"""Round-5 family ``memory_contrast_r5``: ordinary chat, what this chat already said, and a real question about saved memory.

Why. r4 answered "rough day" with "Our memory has nothing saved for 'rough day'": 29 of 333 conversation replies, against
base's 1. ``chat_contrast_r5`` teaches the first half (a new topic gets a normal reply). It has no row where memory IS the
right move, so nothing shows the model the line between the three. This family holds the same topic on each side of it:

- ``chat``        the topic comes up for the first time, as a feeling, a plan or a hobby. No tool, no memory talk.
- ``chat_saved``  the same kind of remark while a related fact IS saved. Still no tool: a saved fact doesn't turn a remark
                  into a lookup.
- ``how_to``      a general question about the hobby or feeling. No tool, answered from knowledge.
- ``in_chat``     the fact was said earlier in this conversation and the user asks for it. No tool: the answer is in the
                  chat, so it states it.
- ``saved_hit``   the user plainly asks what's saved. ``recall``, and the answer states the fact.
- ``saved_miss``  the user plainly asks and nothing is saved. ``recall``, and the answer says so honestly.

Every final is ``BASE_WRITES``: base writes it later, so the voice stays base's. ``sc.base_ref`` is the gold contract
(``gpu/source_contract.py``): ``chat`` and ``in_chat`` modes can't talk about memory, and the facts must be stated.

All wording is fresh and synthetic, and none is taken from an evaluation prompt, answer or failure transcript. Each row goes
through ``leak.LeakIndex`` here, and ``seal_r5.py`` checks the finished build against every test set again.
"""
from __future__ import annotations

import datetime as dt

import writes as _writes
from harness import Call, Final
from scen import Scenario, setup
from scen_r3_followup import _check as _dry_run, turn
from scen_r5_debuglog import BASE_WRITES

FAMILY = "memory_contrast_r5"
WRITE_ALLOW_R5 = {FAMILY: frozenset()}
_writes.MULTITURN = _writes.MULTITURN | {FAMILY}
D = dt.timedelta
DROPPED: list = []

# key: the recall query. first: first mentions. feel: remarks while a related fact is saved. how_to: general questions.
# said: (statement, reply, question, any-of facts). saved: (memory text, any-of facts, question). miss: (question, query).
BUNDLES = [
    dict(key="pottery", first=["signed up for a pottery class on a whim, i've never touched clay in my life",
                               "my first bowl collapsed on the wheel and i laughed so hard"],
         feel=["the pottery studio is the only place my brain goes quiet lately"],
         how_to=["why does clay crack when it dries too fast"],
         said=("the studio i'm trying is called Mudroom, it's on Alder street", "Mudroom sounds like a cozy place to start. How did the first visit feel?",
               "wait what's that studio called again", ["Mudroom"]),
         saved=("Sam's pottery class meets on Thursday evenings at Mudroom.", ["Thursday"], "what do you have saved about my pottery class"),
         miss=("do you have anything saved about my kiln rental", "kiln")),
    dict(key="running", first=["ran three miles without stopping today, first time since high school",
                               "my knees are not happy with my new running habit"],
         feel=["i think running is the one thing keeping me sane this semester"],
         how_to=["how long should an easy run feel easy before i speed up"],
         said=("i'm doing the Riverbend 10k in may, my goal is under 60 minutes", "Under 60 is a great target for a first 10k. How's the training going so far?",
               "what was that goal time again", ["under 60", "60 minutes"]),
         saved=("Sam's running shoes are size 10 Brooks Ghost.", ["Ghost", "size 10"], "check your memory for what running shoes i wear"),
         miss=("what have you got saved about my marathon plan", "marathon")),
    dict(key="sourdough", first=["my sourdough starter finally doubled and i'm weirdly proud of it",
                                 "baked a loaf that could double as a doorstop"],
         feel=["baking bread on sundays has become my favorite part of the week"],
         how_to=["what makes sourdough taste more sour"],
         said=("i named my starter Doughlores and i feed it every morning", "Doughlores is an excellent name. Daily feedings should keep it lively.",
               "what's the starter called again?", ["Doughlores"]),
         saved=("Sam's sourdough recipe uses 75% hydration.", ["75%"], "pull up what you have saved on my sourdough recipe"),
         miss=("is anything saved about my rye bread recipe", "rye")),
    dict(key="guitar", first=["picked up a used guitar this weekend, my fingertips already hate me",
                              "i can finally switch between g and c without stopping"],
         feel=["playing guitar badly is still the best part of my evening"],
         how_to=["how do people build up calluses for guitar"],
         said=("my teacher's name is Ines and she wants me on the F chord by friday", "Ines sounds like she keeps you moving. F chords are tough at first, so short daily practice helps.",
               "what's the teacher's name again", ["Ines"]),
         saved=("Sam's guitar teacher charges $35 a lesson.", ["$35"], "what's in your memory about my guitar lessons"),
         miss=("do you have my amp settings saved anywhere", "amp")),
    dict(key="chess", first=["got destroyed at chess by a nine year old at the park today",
                             "i've been playing way too much online chess this week"],
         feel=["chess has been a nice way to shut my brain off after class"],
         how_to=["what's a good opening for someone who keeps losing fast"],
         said=("my rating is 840 right now, trying to hit 1000 by summer", "That's a solid goal, and 840 to 1000 mostly comes from fewer blunders.",
               "remind me, what's that rating", ["840"]),
         saved=("Sam plays chess at the Linden club on Tuesdays.", ["Tuesday", "Linden club"], "what do you remember saving about my chess club"),
         miss=("did you save anything about my chess coach", "coach")),
    dict(key="birding", first=["saw a kingfisher on my walk this morning and it made my whole day",
                               "i think i've accidentally become a person who owns binoculars"],
         feel=["birdwatching has been weirdly good for my anxiety"],
         how_to=["why do birds sing so much at dawn"],
         said=("the spot i found is the marsh behind Fernhill park", "A marsh is perfect for early mornings. Herons and kingfishers love those edges.",
               "where was that marsh again", ["Fernhill"]),
         saved=("Sam's birding life list is at 62 species.", ["62"], "check what's saved about my birding list"),
         miss=("anything saved about my bird feeder", "feeder")),
    dict(key="roommate", first=["my roommate ate my leftovers again and i'm trying not to be petty about it",
                                "living with someone is harder than i thought it would be"],
         feel=["i've been feeling kind of invisible at home with my roommate lately"],
         how_to=["how do you bring up chores without starting a fight"],
         said=("the thing that really gets me is the dishes sit for like four days", "Four days of dishes would wear on anyone. It makes sense that it's getting to you.",
               "how long do the dishes sit again", ["four", "4"]),
         saved=("Sam's roommate pays rent on the 3rd each month.", ["3rd"], "what do you have saved about my roommate"),
         miss=("did i ever have you save my roommate's work schedule", "schedule")),
    dict(key="sister", first=["my sister and i got into it over something so small tonight",
                              "had a really nice long call with my sister and i feel lighter"],
         feel=["i miss my sister more than i expected to since she moved"],
         how_to=["how do siblings get past old arguments"],
         said=("she's moving to Denver in june for a nursing job", "That's a big move, and a nursing job sounds exciting for her. How are you feeling about it?",
               "where's she moving again?", ["Denver"]),
         saved=("Sam's sister Carmen is allergic to walnuts.", ["walnut"], "what's saved in your memory about my sister"),
         miss=("do you have my sister's new address saved", "address")),
    dict(key="sleep", first=["i've been waking up at 4am for a week and it's wrecking me",
                             "slept nine hours last night and i feel like a new person"],
         feel=["i'm so tired of being tired, sleep has been rough"],
         how_to=["why do i feel worse after sleeping in on weekends"],
         said=("i've been cutting caffeine after 11am to see if it helps", "That's a good experiment. It often takes a week or so before you notice a difference.",
               "what's the caffeine cutoff again", ["11am", "11"]),
         saved=("Sam sleeps better with the bedroom at 66 degrees.", ["66"], "check your memory for what i told you about my sleep"),
         miss=("have you saved anything on my sleep tracker", "tracker")),
    dict(key="garden", first=["my tomato plants are finally doing something and i keep checking on them",
                              "killed another basil plant, i think i'm cursed"],
         feel=["tending the balcony garden is the calmest ten minutes of my day"],
         how_to=["how often should you water tomatoes in pots"],
         said=("the balcony only gets sun until about 2", "Morning sun until 2 still works for herbs and cherry tomatoes. Leafy greens will be happy too.",
               "how late does the balcony get sun again", ["2"]),
         saved=("Sam's garden plot number is 14 at the community garden.", ["14"], "what have you saved about my garden plot"),
         miss=("did you store anything about my compost bin", "compost")),
    dict(key="thesis", first=["i finally wrote two pages of my thesis and it felt huge",
                              "i stared at my thesis draft for three hours and deleted a sentence"],
         feel=["i'm scared my thesis isn't actually any good"],
         how_to=["how do people keep writing when a long project feels stuck"],
         said=("my advisor said chapter two needs a stronger opening", "That's a very fixable note. A stronger opening often comes easier after the rest of the chapter is settled.",
               "what does the advisor want changed again", ["opening"]),
         saved=("Sam's thesis committee has three members.", ["three", "3"], "what's in your memory about my thesis committee"),
         miss=("do you have my thesis defense date saved", "defense")),
    dict(key="move", first=["i think i want to move somewhere new next year but it scares me",
                            "started looking at apartments for fun and now i can't stop"],
         feel=["the idea of moving is exciting and terrifying at the same time"],
         how_to=["what do people forget when they move to a new city"],
         said=("i'm deciding between Tacoma and Boise, leaning Tacoma", "Both have their charm, and Tacoma puts you close to the water. What's pulling you that way?",
               "which city was it again", ["Tacoma"]),
         saved=("Sam wants to stay under $1,400 a month for rent after moving.", ["$1,400"], "what have you got saved about my moving budget"),
         miss=("did you save anything about my moving truck", "truck")),
    dict(key="cooking", first=["made dal from scratch tonight and it actually tasted like something",
                               "i burned rice, which i didn't know was possible at my age"],
         feel=["cooking after a long day has become oddly comforting"],
         how_to=["how do you keep a stir fry from getting soggy"],
         said=("i only have a tiny two burner stove in this place", "Two burners is plenty once you lean on one pot meals. Soups, dals and pasta all work well.",
               "how many burners was it again", ["two", "2"]),
         saved=("Sam can't eat shellfish.", ["shellfish"], "what food stuff do you have saved about me"),
         miss=("did you ever save my lasagna recipe", "lasagna")),
    dict(key="job", first=["i had a really awkward job interview today and i keep replaying it",
                           "got a callback for that design role and i'm trying not to get my hopes up"],
         feel=["job hunting is making me feel like i'm not good at anything"],
         how_to=["how do you answer the what's your weakness question without sounding fake"],
         said=("the role is at a company called Northwind and the second round is on the 14th", "Congratulations on the second round. Northwind must have liked what they saw.",
               "what's that company called again", ["Northwind"]),
         saved=("Sam's target salary is $72,000.", ["$72,000"], "what's saved about my salary target"),
         miss=("have you saved anything about my references", "references")),
    dict(key="dog", first=["we adopted a very nervous greyhound and he's already asleep on my feet",
                           "my dog learned to open the treat drawer and i'm a little impressed"],
         feel=["coming home to my dog is the best part of a hard day"],
         how_to=["why do dogs spin in circles before lying down"],
         said=("his name is Biscuit and the vet thinks he's about six", "Biscuit is a perfect name. Six is a lovely age for a calm companion.",
               "how old does the vet think he is again", ["six", "6"]),
         saved=("Sam's dog takes a heartworm pill on the 1st of the month.", ["heartworm"], "check your memory for what i said about my dog's meds"),
         miss=("do you have my dog's microchip number saved", "microchip")),
    dict(key="money", first=["checked my bank account and immediately closed the app again",
                             "paid off my last credit card today, i could cry"],
         feel=["money stress has been sitting in my chest all week"],
         how_to=["what's the difference between a roth and a regular ira"],
         said=("i'm putting $150 a month toward savings starting now", "That's a real habit to build, and $150 a month adds up faster than it feels like it will.",
               "how much per month was it again", ["$150"]),
         saved=("Sam keeps an emergency fund target of $3,000.", ["$3,000"], "what have you saved about my emergency fund"),
         miss=("did you save my budget spreadsheet link", "spreadsheet")),
    dict(key="friend", first=["i haven't seen my friends in weeks and i only just noticed",
                              "my friend remembered a tiny thing i said months ago and it made me tear up"],
         feel=["i feel like my friend group is drifting apart"],
         how_to=["how do adults actually make new friends"],
         said=("we're supposed to get together at Lantern on saturday night", "Lantern sounds like a good spot to catch up. I hope it's a nice night together.",
               "where are we getting together again", ["Lantern"]),
         saved=("Sam's friend Nadia prefers tea over coffee.", ["tea"], "what's in your memory about my friend nadia"),
         miss=("have you got anything saved on my friend's birthday", "birthday")),
    dict(key="painting", first=["started watercolors and everything i paint looks like a sad puddle",
                                "sold my first painting to a coworker, still can't believe it"],
         feel=["painting is the first thing in ages that's felt just for me"],
         how_to=["how do you stop watercolors from turning muddy"],
         said=("i've been using this brand called Holloway for my paints", "Holloway sounds like a nice set to learn on. How do the colors feel so far?",
               "what's that paint brand again", ["Holloway"]),
         saved=("Sam's painting class supply list needs a size 8 round brush.", ["size 8", "round brush"], "what's saved about my painting supplies"),
         miss=("did you save anything about my art show", "show")),
    dict(key="climbing", first=["finally sent that purple route i've been stuck on for a month",
                                "my forearms are useless after bouldering tonight"],
         feel=["climbing is the one place i stop worrying about everything else"],
         how_to=["how do you get better at slab without just getting scared"],
         said=("i climb at a gym called Basecamp on the east side", "Basecamp sounds fun. Do you usually go with friends or on your own?",
               "what's the gym called again", ["Basecamp"]),
         saved=("Sam's climbing shoes are a size 41.", ["41"], "check what you have saved on my climbing shoes"),
         miss=("did you store anything about my belay partner", "belay")),
    dict(key="meditation", first=["tried meditating this morning and mostly thought about lunch",
                                  "ten days in a row of sitting quietly, which is a record for me"],
         feel=["meditation is helping a little, but some days it just makes me sadder"],
         how_to=["what are you supposed to do when your mind keeps wandering in meditation"],
         said=("i've been doing twelve minutes before coffee", "Twelve minutes before coffee is a lovely rhythm. Keeping it the same each day really helps.",
               "how many minutes was it again", ["twelve", "12"]),
         saved=("Sam's meditation app is called Stillpoint.", ["Stillpoint"], "what's in your memory about my meditation app"),
         miss=("have you saved my retreat dates", "retreat")),
    dict(key="reading", first=["i stayed up until 2 finishing a novel and regret nothing",
                               "i've started four books this month and finished none"],
         feel=["reading before bed has been the only thing that calms me down lately"],
         how_to=["how do you get back into reading after a long break"],
         said=("the one i'm on now is The Salt Road, my coworker lent it to me", "The Salt Road is a nice one to come back with. Are you enjoying it so far?",
               "what's the book i'm on again", ["Salt Road"]),
         saved=("Sam's book club meets the second Sunday of each month.", ["second Sunday"], "what do you have saved on my book club"),
         miss=("did you save my reading list", "list")),
    dict(key="breakup", first=["i think i just need to say it out loud, we broke up last night",
                               "saw my ex at the grocery store and had to hide in the cereal aisle"],
         feel=["it's been a month since the breakup and some mornings still hit hard"],
         how_to=["how long does it usually take to feel normal after a breakup"],
         said=("we were together about three years", "Three years is a big part of your life. It makes sense that this is heavy.",
               "how long were we together again", ["three", "3"]),
         saved=("Sam asked not to bring up the breakup unless Sam mentions it first.", ["bring up", "mention"], "what did you save about the breakup"),
         miss=("do you have anything saved about my therapist", "therapist")),
    dict(key="swimming", first=["went swimming for the first time in years and nearly drowned in the slow lane",
                            "i can finally breathe on both sides when i swim"],
         feel=["swimming laps is the closest i get to feeling calm"],
         how_to=["how do you stop swallowing water when you breathe"],
         said=("the pool i use is at the Harbor Y and it opens at 6", "An early swim at the Harbor Y sounds like a good way to start the day.",
               "which pool is it again", ["Harbor"]),
         saved=("Sam's swimming lane time is Wednesday at 7 AM.", ["Wednesday", "7"], "what's saved about my swimming lane time"),
         miss=("did you keep anything about my swim meet", "meet")),
    dict(key="language", first=["started learning korean and i can now order a coffee very politely",
                                "i froze completely when someone spoke to me in spanish today"],
         feel=["learning a language is making me feel dumb and happy at once"],
         how_to=["how many words do you need to hold a basic conversation"],
         said=("my tutor Yuna meets me online on mondays", "Having Yuna on a steady schedule should really help it stick.",
               "who's the tutor again", ["Yuna"]),
         saved=("Sam's language exam level goal is B2.", ["B2"], "what's in your memory about my language exam"),
         miss=("do you have my flashcard deck saved", "flashcard")),
]
# Where a bundle's key isn't a word of its saved fact, the word recall searches for.
SAVED_QUERY = {"move": "moving", "cooking": "shellfish", "job": "salary", "money": "emergency", "reading": "book"}
MODES = ("chat", "chat_saved", "how_to", "in_chat", "saved_hit", "saved_miss")


def _world(rng):
    now, w = setup(rng)
    w.memories = ["Sam's major: computer science (sophomore) at Linden University."]
    return now, w


def _scenario(rng, mode, b, prompt, steps, gold, hist=None, memory=None, must_see=()):
    now, w = _world(rng)
    if memory:
        w.memories = list(w.memories) + [memory]
    hist = hist or []
    w.history = hist
    steps = steps(w)
    sc = Scenario(FAMILY, prompt, now, w, steps, f"memory contrast ({mode}): {b['key']}")
    sc.history = hist
    sc.approve = None
    sc.expect_cards = []
    sc.must_see = list(must_see)
    # a real question about something that isn't saved may say so: the same labels session_recall_r4 gives its recall rows
    sc.thread, sc.qkind = f"memory_{b['key']}_{mode}", {"saved_miss": "rec_none", "saved_hit": "rec_both"}.get(mode, "chat_contrast")
    sc.base_ref = dict(kind="memory_contrast", mode=mode, scripted=None, topic=b["key"], **gold)
    sc.mode = mode
    return sc


def _final(_w):
    return [Final(BASE_WRITES)]


def _recall(query):
    return lambda _w: [[Call("recall", query=query)], Final(BASE_WRITES)]


def candidates(rng, b):
    """Every scenario of one topic: (mode, scenario)."""
    out = [_scenario(rng, "chat", b, p, _final, dict(facts=[])) for p in b["first"]]
    mem_text, mem_facts, saved_q = b["saved"]
    out += [_scenario(rng, "chat_saved", b, p, _final, dict(facts=[]), memory=mem_text) for p in b["feel"]]
    out += [_scenario(rng, "how_to", b, p, _final, dict(facts=[])) for p in b["how_to"]]
    said, ack, question, facts = b["said"]
    out.append(_scenario(rng, "in_chat", b, question, _final, dict(facts=list(facts)), hist=[turn(said, ack)]))
    out.append(_scenario(rng, "saved_hit", b, saved_q, _recall(SAVED_QUERY.get(b["key"], b["key"])), dict(facts=list(mem_facts)), memory=mem_text,
                         must_see=[mem_facts[0]]))
    miss_q, miss_word = b["miss"]
    out.append(_scenario(rng, "saved_miss", b, miss_q, _recall(miss_word), dict(facts=[], prohibit=[])))
    return out


def fam_memory_contrast_r5(rng):
    DROPPED.clear()
    kept = []
    for b in BUNDLES:
        for sc in candidates(rng, b):
            ok, why = _dry_run(sc)
            if ok:
                kept.append(sc)
            else:
                DROPPED.append((b["key"], sc.mode, sc.prompt, why))
    return kept


FAMILIES_R5 = {FAMILY: fam_memory_contrast_r5}
