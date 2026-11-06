"""Round-5 family ``chat_contrast_r5``: a NEW topic, a feeling or a plan gets a normal warm reply, never a memory lookup.

Why. The r4 Mac gate answered ordinary chat as a memory lookup: "rough day" got "Our memory has nothing saved for
'rough day'" and "i'm thinking about picking up a new hobby" got "Nothing is saved about a new hobby yet." That is 29 of
333 conversation replies against base's 1. The likely teacher was ``session_recall_r4``'s "we haven't talked about {x} in
this chat, and there's nothing saved about it" finals, which r5 now keeps only for prompts that really ask about something
earlier (see that module). This family is the contrast: the user brings up something that was never said before, and the
reply is a plain chat reply.

Every row here:
- is a user bringing up a venting moment, a hobby, a trip, a gift, dinner, a plan, good news or small talk, with no earlier
  mention of it anywhere (a single turn, or a short chat whose earlier turns are about the same thread of talk);
- has NO tool call and NO mention of memory, saving, "we haven't talked about" or "nothing saved";
- is written by base: the scripted final is ``BASE_WRITES`` (scen_r5_debuglog), which must not survive into a final build
  (``scen_r5_debuglog.unwritten``). The user likes base's own chat voice, so r5 learns it from base's text, not from a script.
  The distill step keeps a base reply only if ``MEMORY_TALK`` does not match it (``row.meta`` carries the same regex through
  ``sc.base_ref``).

The wording is fresh and synthetic. It is checked against the eval sets at build time (``scen_r2_act._check`` runs
``leak.LeakIndex`` over the eval questions, the toolbench cases and the chat eval) and by this module's test against
``training/ask-lora-r4/chateval/prompts_r4.yaml`` and the held-out wordings (``scen_r5_debuglog.heldout_folds``).

Earlier assistant turns in the short chats are scripted context only (the loss is on the final); they are written in the
same warm, specific voice and never talk about memory.
"""
from __future__ import annotations

import re

from harness import Final
from scen import Scenario, setup
from scen_r3_followup import _check as _dry_run, turn
from scen_r5_debuglog import BASE_WRITES

FAMILY = "chat_contrast_r5"
WRITE_ALLOW_R5 = {FAMILY: frozenset()}
# Some rows carry seeded earlier turns: the validator reads the last user message as the prompt and allows the earlier
# user turns (the same registration scen_r4_devsets makes for its own families).
import writes as _writes  # noqa: E402
_writes.MULTITURN = _writes.MULTITURN | {FAMILY}

# What no reply, scripted history turn or prompt of this family may say. The distill step applies it to base's text.
MEMORY_TALK = re.compile(
    r"\b(?:(?:our|my|your|the) memory|memory (?:has|is|doesn't|does not)|saved|stored|on file|nothing (?:is )?(?:saved|stored)|(?:haven't|have not) (?:talked|mentioned|told)|"
    r"we haven't|not in this chat|in this chat|tool|lookup|look it up)\b", re.I)

# (category, prompt). Single turns: the user brings something up for the first time.
SINGLES: list[tuple[str, str]] = [
    # ---- venting, tiredness, small worries
    ("vent", "ugh the whole day has been one small disaster after another"),
    ("vent", "i snapped at a coworker today and now i feel awful"),
    ("vent", "feeling really flat lately, nothing sounds fun"),
    ("vent", "i can't focus on anything and the deadline is monday"),
    ("vent", "so tired of being the one who always organizes everything"),
    ("vent", "that was a long shift. my feet hurt and my patience is gone"),
    ("vent", "dreading tomorrow, big review at nine"),
    ("vent", "the group project partners have gone silent again and i'm fuming"),
    ("vent", "i keep overthinking a comment someone made at lunch"),
    ("vent", "everything feels like too much this week"),
    ("vent", "i feel guilty for taking a day off"),
    ("vent", "the neighbors' party kept me up till 3 and i have an early class"),
    ("vent", "tough conversation with my dad tonight, still a little shaky"),
    ("vent", "didn't get the scholarship. kind of gutted"),
    ("vent", "two hours in traffic and i'm so done"),
    ("vent", "i feel like everyone else has it together except me"),
    ("vent", "my best friend is moving away and i'm taking it harder than i expected"),
    ("vent", "i've been putting off a hard email all day and it's making me anxious"),
    ("vent", "the landlord says the rent is going up again"),
    ("vent", "woke up at four and couldn't get back to sleep, now i'm a zombie"),
    ("vent", "the day got away from me and i got nothing done"),
    ("vent", "feeling kind of invisible at work lately"),
    # ---- hobbies
    ("hobby", "thinking of getting into birdwatching, no idea where to start"),
    ("hobby", "i want to learn the ukulele"),
    ("hobby", "been wanting to try sourdough but everyone says it's hard"),
    ("hobby", "evenings are all screens lately, i want a hobby that gets me off the couch"),
    ("hobby", "considering taking up swimming again after years away"),
    ("hobby", "got the urge to start painting but i have zero talent"),
    ("hobby", "want to start a little herb garden on the balcony"),
    ("hobby", "thinking about joining a pickup soccer group"),
    ("hobby", "found an old film camera in a drawer and now i want to shoot again"),
    ("hobby", "can't decide between learning chess or learning to knit"),
    ("hobby", "signed up for a beginner woodworking class, a bit nervous"),
    ("hobby", "i'd love to get into hiking but i'm really out of shape"),
    ("hobby", "thinking about starting a podcast with a friend, silly idea?"),
    ("hobby", "just ordered a cheap synthesizer, wish me luck"),
    ("hobby", "want to learn to cook thai food properly"),
    # ---- trips
    ("trip", "going to montreal for four days in march, any thoughts?"),
    ("trip", "we're planning a road trip down the coast in july"),
    ("trip", "thinking of a solo trip somewhere quiet"),
    ("trip", "a cousin and i might do iceland in winter, crazy?"),
    ("trip", "first time flying with a toddler next month and i'm nervous"),
    ("trip", "long weekend in chicago, mostly to eat"),
    ("trip", "can't decide between mountains and the beach for a week off"),
    ("trip", "got invited to a wedding in sicily"),
    ("trip", "thinking about crossing canada by train"),
    ("trip", "planning a birthday trip for five friends and everyone has a different budget"),
    ("trip", "going camping for the first time this summer"),
    ("trip", "wondering if a week in kyoto is enough"),
    ("trip", "a weekend away with my partner, somewhere within three hours of here"),
    ("trip", "we're driving eight hours tomorrow, how do we keep from going insane"),
    ("trip", "finally visiting my grandparents abroad after years apart"),
    # ---- gifts
    ("gift", "need a gift for my brother's 30th, he has everything"),
    ("gift", "what do you get a coworker who's leaving?"),
    ("gift", "a present for my mom, she loves gardening"),
    ("gift", "anniversary coming up, want something thoughtful but not pricey"),
    ("gift", "housewarming gift for friends who just bought their first place"),
    ("gift", "teacher gift ideas for the end of the year?"),
    ("gift", "want to surprise my partner with something small, no occasion"),
    ("gift", "secret santa limit is 20 dollars and i drew the quiet guy from accounting"),
    ("gift", "a baby shower gift that isn't just more clothes?"),
    ("gift", "my grandfather turns 85 and he's impossible to shop for"),
    ("gift", "a thank-you for the neighbor who watered my plants"),
    ("gift", "something for a friend recovering from surgery"),
    # ---- dinner and food
    ("dinner", "friends are coming saturday and i want something easy that looks fancy"),
    ("dinner", "trying to eat more vegetables, dinner ideas that aren't salad?"),
    ("dinner", "i've got chicken thighs, rice and half a lemon. thoughts"),
    ("dinner", "dinner for one tonight, something cozy"),
    ("dinner", "my partner is vegetarian and i'm cooking tonight"),
    ("dinner", "tacos or pasta tonight, help me pick"),
    ("dinner", "never made risotto, is tonight a bad time to start?"),
    ("dinner", "hosting eight people and one of them has a nut allergy"),
    ("dinner", "craving something spicy but the kids are eating too"),
    ("dinner", "meal prep ideas for a busy week?"),
    ("dinner", "leftover rice again. what can i do with it besides fried rice"),
    ("dinner", "date night at home, i want to plan the menu"),
    ("dinner", "need a dessert that takes ten minutes"),
    ("dinner", "what goes well with roast chicken"),
    ("dinner", "trying to cook with less oil, any tricks"),
    # ---- small talk
    ("small", "just made a really good cup of coffee and wanted to tell someone"),
    ("small", "it's raining and i'm in a cozy mood"),
    ("small", "random thought: do octopuses dream"),
    ("small", "it's friday and my brain has already left the building"),
    ("small", "i've been humming the same song for three days"),
    ("small", "just saw the biggest dog ever on my walk"),
    ("small", "procrastinating by sorting my bookshelf by color"),
    ("small", "back from lunch and fully in a food coma"),
    ("small", "the sunset tonight was unreal"),
    ("small", "bored. entertain me"),
    ("small", "do you have a favorite season?"),
    ("small", "spilled soup on my shirt right before a meeting, classic"),
    ("small", "the cat just knocked a glass off the table and stared right at me"),
    ("small", "starting a new book tonight, feeling optimistic"),
    ("small", "give me a word that sounds funnier than it should"),
    # ---- plans, decisions, good news
    ("plan", "thinking about repainting the living room, maybe a deep green?"),
    ("plan", "i might ask for a raise but i'm nervous about it"),
    ("plan", "thinking about adopting a dog, is that crazy with a full-time job?"),
    ("plan", "decided to wake up at six every day starting monday, wish me luck"),
    ("plan", "hosting a game night next week and want it to feel special"),
    ("plan", "thinking of rearranging the room so the desk faces the window"),
    ("plan", "i finally cleaned out the garage!"),
    ("plan", "ran a mile without stopping today for the first time"),
    ("plan", "the plant on the windowsill finally flowered"),
    ("plan", "just finished a thousand-piece puzzle"),
    ("plan", "starting to think about moving to a new city"),
    ("plan", "going to try a week without social media"),
    ("plan", "thinking of surprising my sister by turning up at her graduation"),
    ("plan", "our team won trivia night and i'm unreasonably proud"),
    ("plan", "should i learn spanish or just keep leaning on the app"),
    ("plan", "thinking about getting a small tattoo"),
    ("plan", "the kid lost a first tooth tonight!"),
    ("plan", "nervous about a first date on friday"),
    ("plan", "weighing a job closer to home for less pay"),
    ("plan", "finally booked the dentist after avoiding it for a year"),
]

# Short chats on one thread of talk. Each is a list of (user, assistant) pairs; the last user turn has no reply. A row is
# made at every cut, so a thread of n user turns gives n-1 rows (the history is the pairs before the prompt).
THREADS: dict[str, tuple[str, list[tuple[str, str | None]]]] = {}


def TH(name, cat, *pairs):
    assert name not in THREADS, name
    THREADS[name] = (cat, list(pairs))


TH("meeting", "vent",
   ("had a brutal meeting this afternoon and i'm still wound up",
    "That sounds draining, and it makes sense that you are still buzzing from it. The body can stay in fight mode long after the room has emptied. Do you want to tell me what happened, or would you rather get your mind on something else for a bit?"),
   ("the director shot down the proposal i spent a month on",
    "A month of work, gone in a single meeting. That stings, and it is fair to feel angry as well as disappointed. A rejection in the room does not always say as much about the work as it feels like right now."),
   ("yeah. i don't know if i should push back or let it go",
    "You do not have to decide tonight. A night's sleep often changes how a choice like this looks, and if you do push back, it will land better from a calmer version of you. What would pushing back actually look like?"),
   ("maybe asking what specifically was wrong with it", None))

TH("bread", "hobby",
   ("i've decided i'm going to learn to bake bread",
    "That is a lovely thing to take up, and it rewards patience more than skill. A simple no-knead loaf is a gentle way in: mix it, leave it overnight, bake it in a covered pot. Have you got a heavy pot with a lid at home?"),
   ("there's a dutch oven in the cupboard but it's never been used",
    "Then you are already set. The pot does most of the work by trapping steam, which is what gives you a crisp, crackly crust. Flour, water, salt and a pinch of yeast is all the first loaf needs."),
   ("ok and how long does the whole thing take",
    "Most of it is waiting: roughly twelve to eighteen hours of rising, then about forty-five minutes in the oven. The hands-on time is only around ten minutes, so it fits easily around a normal day."),
   ("so i could start it tonight and have bread for lunch tomorrow", None))

TH("japan", "trip",
   ("i'm planning ten days in japan in the autumn",
    "Autumn is a wonderful choice, with cooler air and the leaves turning, especially around Kyoto. Ten days gives you room for a few places without rushing. Are you leaning toward temples and gardens, or big-city energy?"),
   ("a mix, but i hate being rushed",
    "Then a short list works best: a few days in Tokyo, a few in Kyoto, and maybe one quiet stop in between. Fewer bases means less time hauling a suitcase and more time wandering."),
   ("tokyo and kyoto for sure. is the bullet train worth it",
    "Yes, it is part of the experience as much as the transport. It is quick, punctual and comfortable, and the ride between the two cities takes about two and a half hours. Sit on the right side going west for a chance of seeing Mount Fuji."),
   ("ha, good tip. now i'm wondering about a rail pass", None))

TH("teacher_gift", "gift",
   ("need a present for my sister, she just started her first teaching job",
    "What a nice milestone to mark. A first year of teaching is exhausting and joyful in equal measure, so a gift that makes things easier or softer tends to land well. Does she lean practical, or does she like sentimental things?"),
   ("sentimental, she cries at commercials",
    "Then something with a personal touch would suit her. A small framed print with a line from a book she loves, or a mug with a note tucked inside, would mean a lot. A handwritten card on top matters more than the price."),
   ("i like the framed quote idea. what kind of quote",
    "Something short and warm about teaching or curiosity works well, so it feels like encouragement rather than a poster. A line from a favorite children's book is a lovely route, because it is already part of her world."),
   ("she loves charlotte's web actually", None))

TH("tired_dinner", "dinner",
   ("trying to figure out dinner, i'm wiped out",
    "On a tired night, simple wins. A big bowl of something warm with very little chopping is usually the answer. Do you have pasta or rice in the cupboard, and anything fresh to go with it?"),
   ("pasta, a can of tomatoes, some garlic and a bit of parmesan",
    "That is a good dinner already. Soften the garlic gently in olive oil, add the tomatoes, let it bubble for ten minutes while the pasta cooks, and finish with the cheese. It is nearly impossible to get wrong."),
   ("perfect. is it ok to add a pinch of sugar or is that cheating",
    "Not cheating at all. A small pinch softens the acidity of canned tomatoes, and plenty of Italian cooks do the same. Taste first, because some brands are sweet enough already."),
   ("nice, tasting as i go then", None))

TH("new_city", "vent",
   ("just moved to a new city and the evenings are very quiet",
    "Moving is a big change, and the first weeks can feel quiet after the noise of unpacking. It takes a while for a place to feel like yours. How are you finding it so far, apart from the evenings?"),
   ("i like it, i just don't know anyone yet",
    "That is a very normal stage, and it does pass. Small, repeated things tend to work better than big efforts: the same cafe, a weekly class or a club where the faces start to repeat."),
   ("there's a board game cafe nearby with a weekly meetup",
    "A board game cafe is a lovely fit. A game gives everyone something to do with their hands and something to talk about, which takes the pressure off meeting people. Going at the same time each week lets faces start to repeat."),
   ("going to try the meetup on thursday", None))

TH("dad_party", "plan",
   ("want to throw my dad a surprise party for his 60th",
    "That will mean a great deal to him. The key to a good surprise is a small circle of people who can keep a secret, and one person whose only job is getting him there on time. Is he more of a big-crowd or a close-friends kind of person?"),
   ("close friends and family, maybe 20 people",
    "Twenty is a cozy size. A backyard or a private room at a favorite restaurant would keep it warm without feeling like an event. A short guest list also makes the secret much easier to protect."),
   ("he loves the backyard. food is my worry",
    "A backyard asks for relaxed food, so go for things that sit well: a big grill, a few salads, and a dessert table. A caterer, or dishes split among family, keeps it from landing on you alone."),
   ("family splitting dishes sounds good, how do i ask without spoiling it", None))

TH("organic_chem", "vent",
   ("exam tomorrow and i feel like i know nothing",
    "That feeling of knowing nothing right before an exam is very common, and it is usually not true. Nerves tend to hide what you do know. What subject is it, and how long do you have tonight?"),
   ("organic chemistry, maybe three hours of studying left in me",
    "Three focused hours is plenty to tighten things up. Skip new material now and spend the time on the kinds of problems you are most likely to see. Trying a few from scratch shows you what you actually know."),
   ("ok, old problem sets it is. i'm so anxious though",
    "Anxious is fair, and it does not mean you are unprepared. Breaking the three hours into short blocks with a few minutes' rest helps, and try to stop in time for a proper night of sleep. That will help you more than one more hour of cramming."),
   ("i think i'm going to stop at nine and sleep", None))

TH("rescue_cat", "small",
   ("adopted a rescue cat last week and she hides under the couch",
    "Congratulations, that is lovely news. Hiding is completely normal for a rescue in the first week or two, because everything smells and sounds new. Give her a quiet corner and let her come out on her own terms."),
   ("ok. should i try to coax her out with treats",
    "A treat left near her hiding spot, rather than held out to her, is a gentle way to build trust. Sitting quietly nearby and talking in a low voice helps as well. Let her set the pace."),
   ("she came out for a minute and sniffed my shoe!",
    "That is real progress. A sniff of your shoe means she is getting curious about you, which is a very good sign. Keep things calm and predictable, and she will keep coming closer."),
   ("her name is biscuit, by the way", None))

TH("coast_drive", "trip",
   ("two friends and i are driving to the coast next month",
    "A coast road trip with friends is hard to beat. How long are you planning to be away, and is it a straight run or do you want stops along the way?"),
   ("three days, and we want some stops. no real plan yet",
    "Three days leaves room for one or two good detours. A loose plan works well: pick one big stop each day and leave the rest open. Small towns with a good bakery or a viewpoint often end up being the memories."),
   ("good idea. what about snacks and music",
    "Snacks first: a cooler with fruit, cheese and sandwiches saves you from gas-station dinners. For music, let each person build a short playlist and take turns by the hour, which spreads the taste around fairly."),
   ("we might also camp one night, is that crazy on the same trip", None))

TH("anniversary", "gift",
   ("our fifth anniversary is next week and i have nothing planned",
    "There is still plenty of time to do something good. What matters most is that it feels thoughtful, not that it is grand. What does your partner enjoy most, an evening out or something at home?"),
   ("home, we're both homebodies",
    "A home evening can be very romantic. You could cook something you ate on an early date, put on music from back then, and set the table properly. It feels personal in a way a restaurant rarely does."),
   ("cooking the first meal we ever had together, i love it",
    "That makes it a story as well as a dinner. If you can find a photo from that time, put it somewhere on the table. It will start a conversation about everything since."),
   ("it was an awful lasagna, honestly, but we ate every bite", None))

TH("friend_rift", "vent",
   ("had a falling out with a close friend over the weekend",
    "I am sorry, that kind of rift is painful, especially with someone close. Do you want to tell me what happened, or would you rather just have some company with it for a while?"),
   ("she said i never make time for her, and it stung because it's partly true",
    "That is a hard thing to hear, and it takes honesty to admit there is some truth in it. It can sting without meaning the friendship is over. It may be that you both feel a bit unseen right now."),
   ("i want to reach out but i don't know what to say",
    "A simple, sincere message goes a long way. Something that acknowledges what she said and says you value her, without a long defense, usually opens the door better than explanations."),
   ("i'll try. is it better to text or call", None))

TH("allergy_dinner", "dinner",
   ("hosting six people on sunday and one of them can't have dairy",
    "That is easy to plan around, as plenty of good dishes are naturally dairy-free. Are you thinking of a main with a few sides, or something more relaxed like a taco or noodle spread?"),
   ("a main with sides, nothing too fussy",
    "A roast chicken with lemon and herbs, a tray of roasted vegetables and a pot of rice or potatoes would suit everyone and hold well while you enjoy the evening. Olive oil does the job of butter in nearly everything."),
   ("love it. dessert is the part i'm unsure about",
    "Fruit-based desserts are the simplest answer here: a baked pear with honey and cinnamon, or a bowl of berries with coconut cream. Everyone can share the same dessert, so nobody is singled out."),
   ("baked pears sound great, how long in the oven", None))

TH("novel", "small",
   ("just finished a novel that wrecked me in the best way",
    "Those are the best ones, the books that leave you quiet for a while afterward. What was it, and what hit you hardest?"),
   ("a quiet family story set on a farm, the ending got me",
    "Quiet family stories on a farm often do that, because the big feelings sit under small everyday moments. A good ending that lands softly can be harder to shake than a dramatic one."),
   ("yes exactly. what should i read next without crying again",
    "Something gentler and warmer might suit you after that. A funny, comforting story, like a village comedy or a light literary novel with plenty of humor, would let your heart rest. What do you usually go back to?"),
   ("mostly literary stuff, but i'd take a comedy break", None))

TH("lease", "plan",
   ("signed a lease on a new apartment, moving in three weeks",
    "Congratulations on the new place. Three weeks is enough time if you start early and take it in small pieces. Are you doing it yourself or hiring movers?"),
   ("doing it myself with a couple of friends",
    "Friends and pizza can get a lot done. Packing a room a night starting now makes the last weekend far less frantic, and labeling boxes by room will save you an hour of confusion on moving day."),
   ("yeah labeling, good. what's the thing people always forget",
    "Most people forget a first-night box: bedding, toiletries, a phone charger, a kettle and a few snacks. After a long day of hauling, being able to find those makes the new place feel like home right away."),
   ("great idea. the book boxes are going to be so heavy though", None))

TH("new_job", "vent",
   ("starting a new job on monday and my stomach is in knots",
    "That is a big day, and nerves are a sign that you care how it goes. Most people are far friendlier on a first day than we imagine. Do you know what the first morning will look like?"),
   ("just orientation and meeting the team, i think",
    "That is a gentle start. People expect you to ask questions and not know where anything is, so you do not need to prove yourself yet. Just learn names and be curious."),
   ("i always forget names right after hearing them",
    "Repeating a name back when you hear it, such as using it in your reply, helps it stick. Jotting a quick note about each person on your break works well too. People appreciate the effort."),
   ("ok, i'll bring a little notebook", None))

TH("balcony", "hobby",
   ("got a tiny balcony and i want to grow some food on it",
    "A small balcony can be surprisingly productive. How much sun does it get, and are you picturing herbs, vegetables or a bit of both?"),
   ("about five hours of sun, mostly herbs and cherry tomatoes",
    "Five hours is enough for both. Herbs like basil, parsley and chives are happy in pots, and cherry tomatoes will do well in a big container with a stake. Pots at least a foot wide give the roots room."),
   ("when should i start",
    "It depends on your climate, but the safest approach is to wait until the last frost has passed for the tomatoes. Herbs are more forgiving and can start sooner. Small plants from a nursery make the first season easier than seeds."),
   ("nursery plants it is. do the herbs need anything special in the pots", None))

TH("burnout", "vent",
   ("i think i'm burned out, i dread opening my laptop every morning",
    "That sounds heavy, and dreading the start of each day is worth taking seriously. It can build gradually until the whole world feels like a to-do list. Has anything changed recently, or has it been creeping up for a while?"),
   ("creeping up for months. too many projects, no breaks",
    "That is a long time to run on empty, and no wonder you are tired. Burnout usually comes from sustained load without recovery, not from a lack of discipline. Even small breaks that are genuinely protected can start to help."),
   ("i don't know how to take breaks without feeling lazy",
    "That guilt is common, especially for people who care about doing good work. It can help to treat rest as part of the work, like charging a phone. A short walk without a screen counts."),
   ("ok. i could try a real lunch break away from the desk", None))

TH("mini_holiday", "plan",
   ("the weekend looks wide open and i want it to feel like a mini holiday",
    "That is a lovely aim. A mini holiday is mostly about changing the rhythm: slow mornings, one outing, no chores. Do you want to stay close to home or get away somewhere?"),
   ("close to home, i'm on a budget",
    "Close to home works nicely. You could pick a neighborhood you rarely visit and treat it like a tourist: a long breakfast, a walk, a bookshop, a picnic. It costs very little and feels like a different place."),
   ("a picnic would be nice if the weather holds",
    "A picnic suits that plan. Pack things that need no assembling, like bread, cheese, fruit and something sweet, plus a blanket and a book. Have a backup, such as a corner of a cafe, in case it rains."),
   ("what if it does rain, can the picnic move indoors", None))

TH("old_dog", "vent",
   ("my childhood dog passed away this morning",
    "I am so sorry. Losing a dog who has been there since childhood is losing a piece of your growing up, and the grief is real. I am here if you want to talk about them."),
   ("she was 15. she was always at the door when i got home",
    "Fifteen years of someone waiting at the door is a long, loving history. Those small daily greetings are often what we miss the most. She was clearly very loved."),
   ("i don't know what to do with myself today",
    "There is no right way to spend a day like this. Be gentle with yourself: eat something, sit outside if you can, and let yourself feel it. Some people find it helps to write down a few favorite memories."),
   ("maybe i'll look through old photos", None))

TH("solo_trip", "trip",
   ("thinking of taking a solo trip, first time ever",
    "That can be a wonderful experience, and many people find it freeing to follow their own pace. Where are you drawn to, and how long would you go for?"),
   ("a long weekend somewhere in europe, a city that's easy to walk",
    "A walkable city is a good first solo choice. Compact, safe places like Copenhagen, Vienna or Edinburgh let you wander without a plan, and a long weekend is enough to feel like a real getaway."),
   ("edinburgh sounds amazing. is it ok to eat alone in restaurants",
    "Completely normal, and many people grow to enjoy it. Sitting at a counter or bar makes it feel less formal, and lunch is often easier than dinner at first. A book is a pleasant companion."),
   ("good to know, i'm a little nervous about it", None))

TH("french_press", "small",
   ("trying to make better coffee at home",
    "Good goal, and a few small changes make a big difference. What are you using now, a drip machine, a French press or something else?"),
   ("a french press, but it tastes bitter",
    "Bitterness in a French press usually means the coffee is steeping too long or the grind is too fine. Try a coarse grind and about four minutes of steeping, then pour it all out right away so it does not keep extracting."),
   ("oh i leave it in the press after. that's probably it",
    "That is very likely the culprit. Coffee left sitting with the grounds keeps extracting and turns harsh. Pour it into a mug or a thermos as soon as it is done."),
   ("will do. is it worth getting a grinder too", None))


def _check_text():
    """Mechanical rules on the wording itself: no memory talk anywhere, no duplicate prompt, no empty cell."""
    seen = set()
    for cat, p in SINGLES:
        assert p.strip() and p.lower() not in seen, f"duplicate or empty single {p!r}"
        seen.add(p.lower())
        assert not MEMORY_TALK.search(p), f"memory talk in {p!r}"
    for name, (cat, pairs) in THREADS.items():
        assert 3 <= len(pairs) <= 5 and pairs[-1][1] is None and all(a for _, a in pairs[:-1]), name
        for u, a in pairs:
            assert u.lower() not in seen, f"duplicate prompt {u!r}"
            seen.add(u.lower())
            assert not MEMORY_TALK.search(u + " " + (a or "")), f"memory talk in {name}: {u!r} / {a!r}"
            assert not re.search(r"[*#\n]", a or ""), f"markup in {name}"


DROPPED: list = []


def _scenario(rng, prompt, cat, hist, tag):
    now, w = setup(rng)
    w.history = hist
    sc = Scenario(FAMILY, prompt, now, w, [Final(BASE_WRITES)],
                  f"chat contrast ({cat}): a new topic or feeling gets a normal reply from base, no tool, no memory talk")
    sc.history = hist
    sc.approve = None
    sc.expect_cards = []
    sc.must_see = []
    sc.thread, sc.qkind = tag, "chat_contrast"
    sc.base_ref = dict(kind="chat_contrast", forbid=MEMORY_TALK.pattern, scripted=None)
    return sc


def fam_chat_contrast_r5(rng):
    _check_text()
    cands = [(cat, p, [], f"single_{cat}") for cat, p in SINGLES]
    for name, (cat, pairs) in THREADS.items():
        for cut in range(1, len(pairs)):
            hist = [turn(u, a) for u, a in pairs[:cut]]
            cands.append((cat, pairs[cut][0], hist, f"thread_{name}"))
    out = []
    for cat, prompt, hist, tag in cands:
        sc = _scenario(rng, prompt, cat, hist, tag)
        ok, why = _dry_run(sc)
        if not ok:
            DROPPED.append((tag, prompt, why))
            continue
        out.append(sc)
    return out


FAMILIES_R5 = {FAMILY: fam_chat_contrast_r5}
