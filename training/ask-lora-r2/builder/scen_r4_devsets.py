"""Round-4 DEV sets: ``recall_dev_r4`` and ``session_style_dev`` (RL-SCOPE P1, §4.2, C6b, C16).

These two families are held-out evaluation data for the GRPO probes (S-text recall rows, S-session). They are never
training data, so they load ONLY when ``ASK_LORA_DEVSETS=1``; without it ``FAMILIES_R4`` is empty and the SFT build,
the validator and every other round-4 tool see nothing of them. The data below is always importable (the tests read it).

Build (a separate directory, never merged into the SFT build or data-rl):

    cd training/ask-lora-r2/builder
    ASK_LORA_DEVSETS=1 ASK_LORA_ROUND=4 ASK_LORA_OUT=$OUT/devsets \\
        ~/Desktop/MOE_Project/.venv/bin/python build.py recall_dev_r4 session_style_dev
    ASK_LORA_DEVSETS=1 ASK_LORA_ROUND=4 ASK_LORA_OUT=$OUT/devsets ~/Desktop/MOE_Project/.venv/bin/python validate.py $OUT/devsets/partial.jsonl

``recall_dev_r4``: 12 NEW threads, two questions each = 24. 17 recall questions (7 the user said, 7 the assistant said,
3 both) and 7 never-said ones, in ``scen_r4_session_chat.Q()`` terms: ``qkind`` rec_user | rec_asst | rec_both | rec_none,
``recall_refs`` on the recall kinds (what the answer must bring back), ``recall_absent`` (+ ``recall_ok``) on rec_none,
``thread`` on every row. Three threads are long chats (6-8 exchanges of full-length replies) and three are 22-24
exchanges, past the 40-message window, so production condenses the oldest turns into the "Earlier in this conversation"
note (the digest path); the asked fact lives in those oldest turns and survives only inside the note (checked on the
captured request, as scen_r4_session_recall does).

``session_style_dev``: 12 held-out NON-recall session questions (emo 3, close 2, build 4, fact 3), one per NEW thread, none
shared with ``recall_dev_r4``. They carry ``thread`` and ``qkind`` but none of the recall fields.

Histories are what production stores: the user text and the final assistant text of each earlier turn, nothing else
(``scen_r3_followup.turn``). Voice, entity and repetition rules are the session_chat_r4 ones (imported, not copied); the
host dry-run (``scen_r3_followup._check``) proves each scenario runs as scripted.

No thread, topic, name or wording is shared with session_chat_r4, session_recall_r4, instructions_r4, the toolbench
``session`` cases or ``chateval/prompts_r4.yaml``: ``test_devsets.py`` proves it with the leak index, the held-out
shingles, folds, and a name/number comparison.
"""
from __future__ import annotations

import copy
import os
import random
import re

from harness import Final
from scen import Scenario, setup
import harness as H
import scen_r3_followup as F
from scen_r3_followup import turn
from scen_r3_chat import BANNED_OPENERS, CLOSERS, EMOJI, PERSONA, _first3
import scen_r4_session_chat as S
from scen_r4_session_recall import _interleave
from service.memory.context import DIGEST_HEADER
from service.model_only import asks_personal_data, capability_question

RECALL = "recall_dev_r4"
STYLE = "session_style_dev"
PREFIX = {RECALL: "rdev", STYLE: "sdev"}
ENABLED = os.environ.get("ASK_LORA_DEVSETS") == "1"
WRITE_ALLOW_R4 = {RECALL: frozenset(), STYLE: frozenset()} if ENABLED else {}

THREADS: dict[str, dict] = {}     # thread id -> {"fam", "name", "mode", "ex": [(user, assistant)], "opening": n}
QS: dict[str, list[dict]] = {RECALL: [], STYLE: []}


def T(fam, name, mode, *ex):
    """mode: short (3-8 exchanges) | long (6-10 exchanges of full-length replies) | digest (22-24 exchanges: the written
    opening exchanges, then short filler exchanges, so the host condenses the oldest turns)."""
    tid = f"{PREFIX[fam]}_{name}"
    assert tid not in THREADS, tid
    THREADS[tid] = dict(fam=fam, name=name, mode=mode, ex=[(u, a) for u, a in ex], opening=len(ex))


def Q(fam, name, kind, prompt, final, *, ref=(), ctx=(), absent=None, ok=()):
    """kind: rec_user | rec_asst | rec_both | rec_none (recall_dev_r4) ; emo | close | build | fact (session_style_dev)."""
    QS[fam].append(dict(fam=fam, thread=f"{PREFIX[fam]}_{name}", kind=kind, prompt=prompt, final=final,
                        ref=tuple(ref), ctx=tuple(ctx), absent=absent, ok=tuple(o.lower() for o in ok)))


# =====================================================================================================
# filler exchanges (digest threads): short general-knowledge turns that sit between the opening and the question
# =====================================================================================================
FILLER = [
    ("how many keys are on a standard piano", "A standard piano has 88 keys, 52 white and 36 black."),
    ("what's the largest desert on earth", "Antarctica is the largest desert, since it gets very little precipitation, and the Sahara is the largest hot one."),
    ("how are pearls formed", "A pearl forms when an irritant gets inside an oyster and the oyster coats it in layers of a smooth substance called nacre."),
    ("what's the hardest natural material", "Diamond is the hardest natural material, though it can still chip along its cleavage planes."),
    ("why do bananas turn brown", "When the peel is bruised or the fruit ripens, enzymes react with oxygen and form brown pigments."),
    ("what's the deepest part of the ocean", "The Challenger Deep in the Mariana Trench, at nearly 11,000 meters below the surface."),
    ("how many chambers does a human heart have", "Four, two upper atria and two lower ventricles."),
    ("how tall is the tallest mountain above sea level", "Mount Everest, at about 8,849 meters."),
    ("who composed the four seasons", "Antonio Vivaldi, an Italian composer, who published it in 1725."),
    ("which language has the most native speakers", "Mandarin Chinese has the most native speakers, followed by Spanish."),
    ("what is the point of a snorkel with a valve at the top", "The valve closes when a wave washes over the tube, so water stays out and you can keep breathing through it."),
    ("does water boil at a lower temperature up a high mountain", "Yes, because there is less air pressure up there, so water boils at a cooler temperature and food takes longer to cook."),
    ("what is a group of owls called", "A parliament, which is the traditional name for a group of owls."),
    ("what is the chemical symbol for sodium", "Na, from the Latin word natrium."),
    ("how many players are on a soccer team", "Eleven per side, including the goalkeeper."),
    ("why do woodpeckers not get headaches", "Their skulls and tongues are thought to spread out the force of each strike, and their small, tightly packed brains take little shock."),
    ("what is the currency of switzerland", "The Swiss franc, which is also used in Liechtenstein."),
    ("how long does the moon take to orbit the earth", "About 27 days for one orbit, though the cycle of phases takes closer to 29 and a half."),
    ("which planet has the coldest atmosphere", "Uranus has the coldest recorded atmosphere, even though Neptune is farther from the sun."),
    ("what makes a boomerang come back", "Its two wings are shaped like airfoils and spin as it flies, and the lift and the spin together curve it back toward the thrower."),
    ("what's a palindrome", "A word or phrase that reads the same forward and backward, like level or racecar."),
    ("how long is a day on venus", "A single day on Venus lasts about 243 Earth days, which is longer than its year of roughly 225 days."),
    ("which instrument has keys and also pipes", "The pipe organ, which uses keyboards to open pipes of different lengths."),
    ("what's the main ingredient in guacamole", "Avocado, usually mashed with lime juice, salt, onion and cilantro."),
    ("what is the plural of fungus", "Fungi is the usual plural, and funguses is also accepted."),
    ("which river is the longest in africa", "The Nile, which flows north about 6,650 kilometers to the Mediterranean Sea."),
    ("what's the shortest month", "February, with 28 days in most years."),
    ("where do penguins live", "Almost all of them live in the Southern Hemisphere, and only the Galapagos penguin lives near the equator."),
    ("what is the heaviest animal", "The blue whale, which can weigh well over a hundred tons."),
    ("how many wings does a bee have", "Four, a larger front pair and a smaller back pair that hook together in flight."),
    ("what temperature is room temperature", "Usually about 20 to 22 degrees Celsius, or the low 70s in Fahrenheit."),
    ("what does a barometer measure", "Air pressure, which helps forecast changes in the weather."),
    ("why is grass green", "Grass is rich in chlorophyll, which absorbs red and blue light and reflects green."),
    ("how fast does sound travel", "About 343 meters per second in air at room temperature."),
    ("what do pandas eat", "Mostly bamboo, which can take up to 14 hours of their day."),
    ("how many letters are in the greek alphabet", "Twenty-four, starting with alpha and ending with omega."),
    ("why do ships have a bulb at the front below the waterline", "The bulb reshapes the wave the hull makes, which lowers drag and saves fuel at cruising speed."),
    ("which metal is liquid at room temperature", "Mercury, which is why it was once used in thermometers."),
    ("what is the biggest island", "Greenland is the largest island that is not a continent, at about two million square kilometers."),
    ("how fast do garden snails move", "Very slowly, covering a few dozen meters an hour at best."),
    ("what is the capital of norway", "Oslo, on the southern coast at the head of the Oslofjord."),
    ("who wrote the hobbit", "J. R. R. Tolkien, who published it in 1937."),
    ("what do you call a group of crows", "A murder of crows, which is the traditional name for the group."),
]


def _fillers(tid, n, avoid):
    """n filler exchanges for thread tid, a fixed shuffle of the bank; none may hold a string the question depends on."""
    pool = [f for f in FILLER if not any(a and S._norm(a) in S._norm(f[0] + " " + f[1]) for a in avoid)]
    order = sorted(range(len(pool)), key=lambda i: S._h("filler", tid, pool[i][0]))
    return [pool[i] for i in order[:n]]


# =====================================================================================================
# recall_dev_r4 : 12 new threads, 24 questions
# =====================================================================================================

# ------------------------------------------------------------------ swim lessons (short)
T(RECALL, "swim", "short",
  ("signed up for adult swim lessons and honestly i'm a little embarrassed, i can barely float",
   "Plenty of adults learn to swim, and the beginner class is usually the friendliest room in the building. Floating is the real first skill, since everything else builds on trusting the water to hold you. Do you know yet when your first lesson is?"),
  ("tuesdays at 7:30 at the sorrel hill rec center, the coach is a woman named thandiwe",
   "A Tuesday evening slot gives you a full week between lessons to let each one settle. Coaches who teach beginners tend to be patient, and a name you already know makes the first night easier to walk into. Arrive a few minutes early so you are not rushing onto the pool deck."),
  ("what goes in my swim bag",
   "A swimsuit you can move in, a towel, goggles and a swim cap if your hair is long. A small bag with a change of clothes and a bottle of water makes the evening less hectic. Most rec centers also ask for a quick shower before you get in."),
  ("ok goggles, any kind",
   "Choose a pair that seals gently around the eye sockets without pressing hard, and test them by pressing them on without the strap. If they stay for a second or two, they will likely hold in the water. Silicone gaskets are the most comfortable for long sessions."),
  ("and what if i panic in the deep end",
   "Most beginner classes start in the shallow end, and nobody is asked to go deep before they are ready. If panic comes, the move is to stop, hold the wall and breathe out slowly, and a coach will meet you there. Telling Thandiwe on the first night that deep water makes you nervous lets her plan around it."))
Q(RECALL, "swim", "rec_user", "which night were the lessons again?",
  "Tuesdays at 7:30, at the Sorrel Hill rec center. That leaves a full week between classes, and arriving a few minutes early gives you time to shower and settle before you get in the water.",
  ref=("Tuesdays", "7:30"))
Q(RECALL, "swim", "rec_none", "what's the pool's street address btw",
  "The address has not come up. What you have told me is that it is the Sorrel Hill rec center, on Tuesdays at 7:30, with Thandiwe as the coach. The sign-up confirmation probably lists the street, and it is worth saving before the first night.",
  absent="address", ctx=("Sorrel Hill", "Thandiwe"))

# ------------------------------------------------------------------ bookshelf (short)
T(RECALL, "shelf", "short",
  ("i'm building my first bookshelf this weekend, pine boards from the hardware store, about 6 feet tall",
   "A pine bookshelf is a good first project, since the wood is forgiving and easy to cut. At six feet tall it will want a firm anchor, so plan on fixing the top to a wall stud once it is standing. How wide are you thinking, and will it hold heavy books?"),
  ("30 inches wide, and yes mostly heavy art books",
   "Heavy art books change the plan a little. Over a 30 inch span, shelves of three quarter inch pine will start to sag unless they are supported. A strip of wood along the back edge, glued and screwed under each shelf, keeps them flat for years."),
  ("ok. do i need wood glue or just screws",
   "Both is best. Glue makes the joint strong and the screws hold it while the glue dries, so you get a bond that outlasts either one alone. Wipe away any glue that squeezes out right away, because dried glue blocks stain."),
  ("stain or paint?",
   "Pine takes paint beautifully, and paint hides the knots. Stain can look blotchy on pine unless you brush on a wood conditioner first, so for a first build, paint is the easier route. Two thin coats look better than one thick one."))
Q(RECALL, "shelf", "rec_asst", "what thickness boards were you saying for the shelves",
  "Three quarter inch pine, with a strip of wood glued and screwed along the back edge under each shelf. Across 30 inches and loaded with heavy art books, that strip is what stops them from bowing.",
  ref=("three quarter inch", "back edge"))
Q(RECALL, "shelf", "rec_none", "which color did we land on for the finish",
  "A color has not come up. You were weighing paint against stain, and paint was the easier route for pine. Once you see the shelf standing in its spot, the color is much easier to choose.",
  absent="color", ctx=("paint", "stain"))

# ------------------------------------------------------------------ used car (short)
T(RECALL, "car", "short",
  ("i'm looking at a used honda fit, 2016, for 9800 dollars. it has 74,000 miles",
   "Fits hold their value and have a name for being reliable, so those numbers are a reasonable place to start. At 74,000 miles on a 2016, the maintenance history matters more than the odometer. Ask for the service records and see what was done to the brakes and the transmission fluid."),
  ("the seller is a guy named desmond, he says he's the only owner",
   "A single owner is a good sign, especially if Desmond kept the paperwork. It is worth asking why he is selling, and whether the car has ever been in an accident. A vehicle history report costs a little but can confirm what he tells you."),
  ("what should i offer him",
   "Start a little under the asking price, since most sellers expect some back and forth. For a 9800 dollar car, an opening offer around 9000 leaves you room to meet in the middle. If the inspection turns up a problem, use it to lower the number further."),
  ("ok. i'll have a mechanic look at it first",
   "A pre-purchase inspection is the best money you can spend here, and it usually costs somewhere around 100 to 150 dollars. A mechanic will spot rust, leaks and worn parts that a test drive can hide. If the seller objects to one, treat that as a warning."))
Q(RECALL, "car", "rec_both", "what was the price he's asking?",
  "He is asking 9800 dollars for the 2016 Honda Fit, which has 74,000 miles. Opening around 9000 and having a mechanic inspect it first is a sound way to approach it.",
  ref=("9800", "74,000"))
Q(RECALL, "car", "rec_none", "what color is the car",
  "The color has not come up. What you have told me is that it is a 2016 Honda Fit with 74,000 miles, owned by Desmond and priced at 9800 dollars. Color rarely changes the value much, so it is fine to leave it to the end.",
  absent="color", ctx=("2016", "Desmond"))

# ------------------------------------------------------------------ telescope (short)
T(RECALL, "scope", "short",
  ("my kid wants a telescope for her birthday, she's 11 and obsessed with space",
   "An eleven year old who loves space is at the perfect age for a first telescope. The best beginner choice is usually a Dobsonian, which is simple to aim and gives a large, bright view for the price. Cheap telescopes on wobbly mounts tend to frustrate people, so a sturdy base matters more than a big number on the box."),
  ("budget is around 350. her name is signe by the way",
   "Around 350 is enough for a good six inch Dobsonian, which is a strong first scope. Signe will be able to see the rings of Saturn and the moons of Jupiter with it, and the craters on the moon are spectacular. Plan on a spot away from streetlights for the best views."),
  ("does she need anything else with it",
   "A red flashlight is the one extra worth getting, because white light ruins your night vision. A simple star chart or a phone app that shows the sky helps her find things. A folding chair and a blanket will make longer sessions much more comfortable."),
  ("what's the best thing to look at first",
   "Start with the moon, since it is bright and easy to find, and the detail along the edge of the shadow is striking. After that, Jupiter is a good second target, because its four large moons show up as tiny dots in a line. Saturn's rings are the one that usually makes people gasp."))
Q(RECALL, "scope", "rec_asst", "how big a telescope was that",
  "A six inch Dobsonian, which fits the 350 budget and is easy for Signe to aim. The sturdy base is the part to protect, so skip anything on a wobbly mount.",
  ref=("six inch", "Dobsonian"))
Q(RECALL, "scope", "rec_user", "what age is signe",
  "Signe is 11, and with her interest in space she will likely get years of use out of a good first scope.",
  ref=("11", "Signe"))

# ------------------------------------------------------------------ first campaign (short)
T(RECALL, "campaign", "short",
  ("i'm running my first dungeons and dragons campaign next sunday for four friends",
   "A first campaign is easier than it looks if you keep the first session small. One simple opening scene, a clear goal and a few interesting characters to meet is plenty for four players. Do you have a setting in mind, or would you like to start from a ready-made adventure?"),
  ("a published one. it's called the sunken bell, and the players are all total beginners",
   "A published adventure is a kind choice for a first run, because the maps and encounters are already done and you can focus on being a good host. With four beginners, expect the first hour to go slowly while everyone learns their sheets. That is normal and usually part of the fun."),
  ("how do i keep them from derailing everything",
   "Let them wander a little, because beginners often enjoy poking at things you did not plan for. When the story drifts too far, you can nudge it back with a new event, such as a messenger arriving or a noise from the next room. The goal is a story that feels free but still has somewhere to go."),
  ("what if i forget a rule",
   "Make a call, keep the game moving and look it up after the session. Players remember a fun scene much longer than a correct ruling, and most groups are happy to let the host decide. Jot the rule in a notebook so it is right next time."))
Q(RECALL, "campaign", "rec_user", "what was the adventure called again",
  "The Sunken Bell. A published adventure is a good fit for four beginners, since the maps and encounters are done for you and you can concentrate on hosting.",
  ref=("sunken bell",))
Q(RECALL, "campaign", "rec_none", "which friend is playing the wizard",
  "The characters have not come up yet, only that there are four beginners and a Sunday start. Once everyone picks a class, a short list on an index card will help you keep track of who is who.",
  absent="wizard", ctx=("beginners", "Sunday"))

# ------------------------------------------------------------------ watercolors (short)
T(RECALL, "paint", "short",
  ("decided to learn watercolors, i bought a cheap kids set and my paintings look muddy",
   "Muddy colors are the most common beginner problem, and the cause is usually too much mixing rather than the paint. Each time you stir a third color into a mix, it drifts toward brown. Keeping to two colors per mixture and letting each wash dry before adding another keeps things clear."),
  ("ok. should i buy better paper or better paint first",
   "Paper first. Watercolor paper that is 140 pound cotton or cellulose holds water without buckling, and it changes the result more than the paint does. A small pad of cold press paper is enough to start."),
  ("what brushes",
   "One round brush in a size 8 and a flat brush about an inch wide cover almost everything a beginner needs. Synthetic brushes are cheaper and work well. Rinse them after each session and let them dry lying flat."),
  ("and what do i paint to practice",
   "Simple washes and gradients are the best drills: a flat color, then a color fading into water. After that, try clouds, a pear or a row of leaves. Small, quick studies teach more than one careful painting."))
Q(RECALL, "paint", "rec_asst", "what weight of paper was it again",
  "140 pound, in cold press, which holds water without buckling. A small pad is enough to start, and it will help the colors stay clean.",
  ref=("140 pound", "cold press"))
Q(RECALL, "paint", "rec_none", "what brand was that kids set",
  "A brand has not come up. You only said it was a cheap kids set and that the colors were turning muddy. Switching to better paper will help more than the name on the lid.",
  absent="brand", ctx=("kids set", "muddy"))

# ------------------------------------------------------------------ bake sale (long)
T(RECALL, "bakesale", "long",
  ("i volunteered to run the pta bake sale at lincoln elementary on october 18th and i have no idea where to start",
   "A bake sale runs smoothly when it has three things settled early: a date, a place and a plan for how the baked goods arrive. Since you already have October 18th, the next step is to confirm the spot with the school office, ideally somewhere with a table or two and foot traffic from the pickup line. Then send a short sign-up sheet to parents so you know roughly how much to expect. Do you know how many families are in the school?"),
  ("about 240 families. we want to raise 1,500 dollars for the library",
   "With 240 families, a goal of 1,500 dollars is realistic. If a third of them contribute baked goods and you sell most items for one or two dollars, you will land close to it. A few special items, like whole cakes or boxed cookie sets priced higher, help you cover the gap. Keep a simple count of what comes in so you can see how the goal is shaping up."),
  ("how should i price things",
   "Keep it simple so the line moves: one dollar for a cookie or a small muffin, two for a cupcake or a brownie, and four or five for a whole loaf or a decorated cake. Round numbers mean you do not need coins, and it helps to have a cash box with a stack of ones and some quarters. A small sign at each table with the prices saves a lot of repeated questions."),
  ("what about allergies, last year someone had a nut reaction",
   "That deserves real care. Ask every baker to label what is in each item, and set up a separate table, or at least a clear sign, for anything that contains nuts. Some schools prefer a nut-free sale altogether, so it is worth checking the school policy before you send the sign-up form. A printed ingredient card next to each tray makes it easy for parents to check."),
  ("ok i'll ask the office. who do i get to help on the day",
   "Four volunteers cover it well: two at the tables selling, one handling the cash box and one restocking and greeting. Shifts of about an hour keep people fresh, and a sign-up sheet in the same message as the baking form saves you from chasing people later. If a couple of older students want service hours, they can handle setup and cleanup."),
  ("good idea. should i advertise it somewhere",
   "A flyer in backpacks a week before and a reminder in the class newsletter will reach most families. On the day itself, a hand-lettered sign at the school entrance and one by the pickup line draws people who forgot. If the school has a parent group chat, a photo of the best treats the night before builds some appetite."),
  ("last thing, how do i handle leftovers",
   "Pack up what is still fresh and offer it to the teachers' lounge or the front office, since staff are usually glad to take a treat home. Items with cream or fillings should go out of the heat quickly, and anything that has sat out for hours is better thrown away than shared. A box by the exit labeled for volunteers is a nice thank you too."))
Q(RECALL, "bakesale", "rec_user", "what date is the sale again",
  "October 18th, at Lincoln Elementary. A flyer in backpacks a week ahead of it would reach most families in time.",
  ref=("October 18th", "Lincoln"))
Q(RECALL, "bakesale", "rec_asst", "what were you suggesting for the cupcake price",
  "One dollar for a cookie or a small muffin, two for a cupcake or a brownie, and four or five for a whole loaf or a decorated cake. Round numbers keep the line moving and mean nobody needs coins.",
  ref=("one dollar for a cookie", "two for a cupcake"))

# ------------------------------------------------------------------ apartment hunt (long)
T(RECALL, "flat", "long",
  ("i'm apartment hunting in chicago and my budget is 1,900 a month, i need to be near the blue line",
   "Chicago has plenty of options near the Blue Line at that budget, especially in neighborhoods like Logan Square, Avondale and Irving Park, where a one bedroom often falls somewhere close to it. It is worth deciding in advance how long a commute you can live with and whether you need to be walking distance from a station or just a short bus ride away. Do you want a one bedroom, or would you share?"),
  ("one bedroom. i work from home three days a week, so there has to be room for a desk",
   "A one bedroom with a dining nook or a bit of extra wall space can hold a desk comfortably, and some older buildings have a sunroom that works well as an office. When you tour, look at where the window faces and how loud the street is at midday, since you will be there on those days. Ask about internet providers too, because some older buildings have limited options."),
  ("how early should i start looking",
   "The Chicago market moves fast in summer, and a good listing can be gone within a few days, so starting about 60 days before your move date is a good rule. Landlords rarely hold apartments longer than that. Keep a folder with your pay stubs, ID and references ready to send quickly, so you can apply the same day you tour."),
  ("ok. lease would start august 1st",
   "Then late May is the time to start browsing and early June is when to tour seriously. That leaves several weeks to compare, and room if a place falls through. Many Chicago leases start on the first of the month, so an August 1st date lines up well with what is usually listed."),
  ("what's a security deposit usually",
   "Most Chicago landlords ask for one month of rent as a deposit, so about 1,900 dollars at your budget, and some also charge a move-in fee. The city rules generally require the deposit to be held in a separate account with interest, which is worth asking about. Get the terms in writing before you hand over any money."),
  ("what do i look for on a tour",
   "Run the taps and flush the toilet, check the water pressure, and open the windows to see how they seal. Look for signs of water damage on ceilings and under sinks, test the outlets with your phone charger and notice cell reception in each room. Ask about heat, since many older Chicago buildings control it centrally and winter bills vary."),
  ("ok last one, how do i not get scammed",
   "Never send a deposit for an apartment you have not seen, and be wary of a landlord who cannot meet in person or insists on payment by gift card or wire. Check that the listing matches the building's address and that the person showing it can prove they manage it. Real listings rarely come with pressure to decide the same day."))
Q(RECALL, "flat", "rec_both", "what is the rent ceiling we landed on",
  "1,900 dollars a month, near the Blue Line. A deposit of one month would come to the same 1,900, if the landlord asks for one.",
  ref=("1,900", "Blue Line"))
Q(RECALL, "flat", "rec_none", "who's moving in with me again",
  "You have not said anyone is moving in with you. What we have settled is a one bedroom near the Blue Line, 1,900 dollars a month and a lease starting August 1st. If a partner or roommate is joining, a two bedroom changes the numbers.",
  absent="roommate", ctx=("Blue Line", "August 1st"))

# ------------------------------------------------------------------ local-history podcast (long)
T(RECALL, "podcast", "long",
  ("i want to start a podcast about the history of my hometown, it's a mill town called harlan falls",
   "A local history podcast is a lovely idea because every town has stories that nobody has written down. Starting with a small number of episodes, say five or six, lets you find your format before you commit to a long run. It also helps to decide early whether you will tell the stories alone or interview people who remember them, because the equipment and the planning are different. Which feels more natural to you?"),
  ("interviews, i want to talk to the old mill workers. my aunt worked the night shift there for thirty years",
   "Your aunt sounds like the ideal first guest, because she can introduce you to others and she will be at ease on tape. Before the interview, write a short list of open questions and begin with easy ones, like what a normal shift looked like, so she settles in. Let her stories wander, since the best moments are rarely in the questions you planned."),
  ("what microphone should i get",
   "A USB dynamic microphone around 80 dollars, such as the Samson Q2U, is a dependable choice, since it records clearly in an ordinary room and ignores a lot of background noise. It also has an XLR output, so you can upgrade later without replacing it. For interviews away from home, a simple recorder with a small clip-on microphone works well too."),
  ("roughly how long per episode",
   "For interview episodes, 25 to 40 minutes is a comfortable length, long enough to go deep and short enough to finish in one commute. If a conversation runs long, you can split it into two parts rather than cutting good stories. Listeners forgive length when the material is strong and the editing is tidy."),
  ("how do i record the intro and stuff",
   "Write the intro as a few short sentences: the name of the show, one line on what it covers and the episode title. Say it aloud several times and record it in a quiet room with soft surfaces, like a closet full of clothes, which damps echo nicely. Then add a short piece of music under it, and keep the whole thing under thirty seconds."),
  ("ok and where do i put it so people can find it",
   "A podcast host such as Buzzsprout or Podbean takes your audio files and sends them to Apple Podcasts, Spotify and the other directories for you. You upload each episode once, and the feed updates itself. Most have a free or cheap starter plan, so you can try one before you commit."),
  ("alright. what should the first episode be about",
   "Open with the mill itself: when it opened, what it made and what a normal day there felt like. That gives listeners a picture of the town before the individual stories begin. A short episode with your aunt, telling how she started on the night shift, would make a warm first guest."))
Q(RECALL, "podcast", "rec_asst", "what microphone model did you suggest",
  "The Samson Q2U, a USB dynamic microphone at around 80 dollars. It also has an XLR output, so it can grow with you if you upgrade later.",
  ref=("Samson Q2U",))
Q(RECALL, "podcast", "rec_user", "which town is the show about",
  "Harlan Falls, your mill town. Your aunt, who worked the night shift there, would make a lovely first guest.",
  ref=("Harlan Falls",))

# ------------------------------------------------------------------ printer offline (digest)
T(RECALL, "printer", "digest",
  ("my printer keeps going offline, it's a Brother HL-L2350DW and the router is an Eero in the hallway",
   "That model should hold a steady connection, so a drop usually means the printer lost its place on the network. Start by printing a network report from the printer's menu and noting the signal strength. Then check whether the router gave it a new address since last time."),
  ("ok. the wifi name is MapleHouse_5G, could that matter",
   "Yes, it matters. That printer connects only on the 2.4 gigahertz band, and a name ending in 5G suggests the faster band it cannot use. Look for a 2.4 gigahertz network in the router app and join the printer to that one."))
Q(RECALL, "printer", "rec_user", "what's the printer model again",
  "It's a Brother HL-L2350DW. Its wireless is the part to watch, since it connects only on the 2.4 gigahertz band.",
  ref=("Brother HL-L2350DW",))
Q(RECALL, "printer", "rec_none", "what is the password for that network",
  "A password has not come up. All I have is the printer model and the network name, MapleHouse_5G, which looks like the faster band the printer cannot join. The password is usually on the router label or in its app.",
  absent="password", ctx=("MapleHouse_5G",))

# ------------------------------------------------------------------ real estate license exam (digest)
T(RECALL, "license", "digest",
  ("i'm studying for my real estate license exam, it's in march and i've only done two chapters",
   "Two chapters in is a fine start. A steady 45 minutes a day on weekdays, with a two hour review on Saturdays, is a plan that works for most people. Both the national and the state sections must be passed."),
  ("what are flashcards worth for this",
   "Flashcards are especially good for vocabulary, such as easement, lien and escrow. The Quizlet app has ready-made decks, which saves you the work of making them. Ten new cards a day, reviewed on a spaced schedule, builds recall without eating your study time."))
Q(RECALL, "license", "rec_asst", "what study schedule did you suggest",
  "45 minutes a day on weekdays, with a two hour review on Saturdays. A steady rhythm like that is easier to keep up than long weekend cramming.",
  ref=("45 minutes",))
Q(RECALL, "license", "rec_asst", "which flashcard app did you mention",
  "Quizlet, which has ready-made decks for vocabulary like easement, lien and escrow, so you can skip building your own.",
  ref=("Quizlet",))

# ------------------------------------------------------------------ octopus costume (digest)
T(RECALL, "costume", "digest",
  ("i'm sewing an octopus costume for my daughter Marguerite, she's 6, and the parade at Alder Grove School is on the 31st",
   "An octopus is a friendly first costume to sew, because the body can be a simple hooded tunic and the legs are just tubes stuffed with batting. Eight arms sound like a lot, but each one is the same pattern repeated. Cut them from a soft jersey so they drape instead of standing stiff."),
  ("what fabric for the arms",
   "Stretchy jersey or fleece in purple works best, since it holds its shape and does not fray. About 2 yards covers all eight arms if you cut them 6 inches wide and 24 inches long."))
Q(RECALL, "costume", "rec_user", "which school is the parade at",
  "Alder Grove School, for the parade on the 31st. Marguerite will have the best-looking arms in the line.",
  ref=("Alder Grove",))
Q(RECALL, "costume", "rec_both", "what costume were we planning again",
  "An octopus, a hooded tunic with eight stuffed arms cut from soft jersey. It is for Marguerite's parade at Alder Grove School on the 31st.",
  ref=("octopus",))

# the digest threads: 22-24 exchanges in all (production keeps the newest 40 messages and condenses the rest)
DIGEST_N = {"rdev_printer": 22, "rdev_license": 23, "rdev_costume": 24}


# =====================================================================================================
# session_style_dev : 12 new threads, 1 non-recall question each (emo 3, close 2, build 4, fact 3)
# =====================================================================================================

# ------------------------------------------------------------------ passed over for team lead (emo, long)
T(STYLE, "promo", "long",
  ("i found out today that i was passed over for the team lead role, they gave it to someone who joined after me",
   "That is a hard thing to hear, especially when you have put years into a place and watched someone newer move ahead. It is normal to feel angry and hurt at the same time, and nothing about those feelings needs to be tidied up today. Do you know yet why they made the decision, or did it come without much explanation?"),
  ("my manager said they wanted someone with more experience in 'stakeholder management', whatever that means. i've been there four years",
   "Four years of showing up and doing the work, and the explanation was a phrase that sounds vague even to you. That would leave most people feeling both dismissed and confused. It might help to ask, when you feel steadier, for specific examples of what they meant, so that the reason becomes something you can act on or fairly question."),
  ("i keep thinking i should just quit",
   "Thinking about leaving makes sense when you feel overlooked, and it is a feeling worth listening to rather than pushing away. It is also a big decision to make on the same day as the disappointment, when everything feels sharper than it will in a week. Nothing needs deciding tonight, and you can let the idea sit while you see how you feel after a few days."),
  ("yeah. i was already tired, the last quarter was brutal, i covered for mireille when she was out for six weeks",
   "Covering six weeks of someone else's work on top of your own is a lot to carry, and it is understandable that being passed over feels like a slap after that. The effort you gave was real whether or not it was recognized this time. Has anyone at work acknowledged what you did while Mireille was out?"),
  ("my manager said thanks once in a meeting, that's about it",
   "One thank you in a meeting is not much for six weeks of extra work, and it makes sense that it does not feel like enough. Recognition matters more than people admit, because it tells you your effort was seen. It could be worth writing down what you handled during that stretch, both for yourself and in case you later decide to raise it."),
  ("maybe. i just feel really small right now",
   "Feeling small after a decision like this is a very human reaction, but it is not a measure of what you are worth or what you did. Four years of steady work, and holding things together while Mireille was away, says a lot about you whatever the title says. Be gentle with yourself for the rest of the evening."))
Q(STYLE, "promo", "emo", "i keep replaying the conversation with my manager over and over",
  "Replaying it is a very natural way for the mind to try to make sense of something that hurt. The part about stakeholder management is probably still bothering you because it gave you nothing real to hold on to, and after covering for Mireille you deserved a clearer reason. A walk or some quiet noise in the background tonight might soften the loop, and the conversation can wait until you are rested.",
  ref=("stakeholder management", "Mireille"))

# ------------------------------------------------------------------ childhood home sold (emo)
T(STYLE, "house", "short",
  ("my parents finally sold the house i grew up in, closing was today",
   "A childhood home holds so many ordinary memories that its sale can feel like a small grief, even when you knew it was coming. Closing day makes it final in a way that planning never does. How are you feeling about it right now?"),
  ("weird. kind of numb. i keep thinking about the oak tree in the backyard, we had a swing on it",
   "Numb is a common first response, and the oak tree is a lovely thing to hold on to, because it carries the sounds and the afternoons of an entire childhood. Memories often attach to a single object like that. The swing probably holds more of your younger self than any room in the house."),
  ("my dad cried when he handed over the keys",
   "Seeing a parent cry at a moment like that can be both tender and hard to witness. It shows how much that house meant to him, and it probably makes the day feel heavier for you too. Having your own feelings alongside his is understandable."),
  ("i don't even live there, why does it hurt so much",
   "Living elsewhere does not weaken a place's claim on you. The house was where you came from, and it was the setting for your family's story even after you moved out. It makes sense that losing it hurts, and it does not need a practical reason."))
Q(STYLE, "house", "emo", "i wish i'd taken a picture of the tree",
  "A photo of the tree would have been nice, but the picture in your mind of the oak tree and the swing is already vivid. You and your dad both carry that house with you, and it does not go to the new owners with the keys. Would it help to ask him what he remembers most about it?",
  ref=("oak tree", "swing"))

# ------------------------------------------------------------------ failed first lab report (emo)
T(STYLE, "lab", "short",
  ("got my first physics lab report back and it's a 54. i've never failed anything before",
   "A 54 stings, particularly when you are used to doing well, and it is fair to feel shaken by it. One report is one piece of the course, though, and first lab reports are often graded harshly because the format is new. Did the grader leave any comments on what cost you the most?"),
  ("mostly my error analysis. i didn't know we needed uncertainty propagation",
   "That is a specific, fixable gap, which is good news. Uncertainty propagation is something most students only learn by seeing it done once or twice, and the next report is a fresh chance to use it. Asking your teaching assistant for a short walkthrough would turn this one into the clearest lesson of the term."),
  ("i feel so stupid. everyone else seemed to get it",
   "You are probably seeing only the people who spoke up, since students who struggled rarely announce it. Missing a technique nobody explained clearly is not the same as being stupid, and a grade of 54 on a first report says very little about your ability. The fact that you pinpointed the problem so quickly says the opposite."),
  ("my lab partner is ravi and he got a 71, he did the analysis section",
   "That helps explain it. If Ravi wrote the analysis section, he was the one practicing that skill, so the gap between your scores makes sense. Next time you could split the analysis between you, so you both get the practice."))
Q(STYLE, "lab", "emo", "i'm thinking about dropping the class",
  "Disappointment makes that thought loud right now, and a 54 on a first report is a hard thing to sit with. You found the cause quickly, and error analysis is learnable, with your teaching assistant and Ravi both within reach. It could be worth waiting for the next report before deciding anything big.",
  ref=("error analysis", "Ravi"))

# ------------------------------------------------------------------ closet (close)
T(STYLE, "closet", "short",
  ("my closet is a disaster and i want to finally sort it this weekend",
   "A closet sort goes much faster in three piles: keep, donate and unsure. Take everything out first so you can see how much there is, and be honest about what you have not worn in a year. Start with the shelves and the floor, where the forgotten things hide."),
  ("ok, i have like 40 shirts and honestly wear about 8 of them",
   "That is a very common ratio. Keep the 8 you reach for, then check whether any of the other 32 are worth wearing again, perhaps with a quick try-on. The ones that no longer fit or make you feel good are ready to donate."),
  ("what do i do with the unsure pile",
   "Put the unsure pile in a box, seal it and write the date on it. If you have not opened the box by three months from now, you have your answer, and the box can go to donation without a second look. The date keeps the decision easy."))
Q(STYLE, "closet", "close", "perfect, going to start with the shelves",
  "Good plan. Keep the 8 shirts you actually wear, and let the date on the unsure box do the deciding later.",
  ref=("8", "unsure"))

# ------------------------------------------------------------------ juggling (close)
T(STYLE, "juggle", "short",
  ("i'm trying to learn to juggle with three tennis balls and i keep dropping them",
   "Dropping the balls is part of the learning, and most people drop them for the first week or two. Start with a single ball, tossing it in an arc from hand to hand at about eye height. When that feels easy, add a second one before the first lands."),
  ("ok. how long each day",
   "Ten minutes a day beats an hour once a week, because the skill is in the rhythm and your hands remember short, frequent sessions. Stand over a bed or a sofa so a drop does not send the ball rolling away. Stop when you feel yourself getting frustrated."),
  ("i can do about two throws before they scatter",
   "Two throws in a row is real progress, since that is the point where most people feel the pattern for the first time. Keep the arcs the same height and let the balls come to you instead of reaching for them. Three clean throws usually follow within a few days of practice."))
Q(STYLE, "juggle", "close", "great, gonna do ten minutes after dinner",
  "Ten minutes after dinner is a good rhythm. Standing over the bed will save you from chasing tennis balls around the room.",
  ref=("ten minutes", "bed"))

# ------------------------------------------------------------------ office book swap (build, long)
T(STYLE, "bookswap", "long",
  ("i'm organizing a book swap at my office for the first friday of december, about 30 people work here",
   "A book swap is a cheap and friendly event, and a Friday lunchtime is a good slot because people are relaxed and often around. With about 30 people, one or two tables are enough for the books, with a sign-in sheet or a basket for people to add what they bring. It helps to announce it two weeks ahead, so everyone has time to look through their shelves. Do you have a space in mind?"),
  ("the break room, it has two long tables. should people bring books beforehand or that day",
   "Letting them bring books on the morning of the swap is easiest, because there is nothing to store and nobody forgets. You could set up a collection box near the front door the week before for early donors, which helps the tables look full from the start. A simple rule of one book in, one book out keeps the numbers balanced."),
  ("what about leftover books",
   "Plan for the leftovers ahead of time so that no one ends up with a pile to carry. Many offices donate them to a local library, a school or a little free library, and a charity shop will usually take a box. Mention the plan on the invitation so people know their books will go on to a good place."),
  ("good. and how do i make it feel less like a chore and more like an event",
   "Small touches change the mood: a few hot drinks, some snacks, and handwritten cards where people can say why they loved a book. Putting books in loose categories, such as mysteries, cooking and children's, helps people browse. A short stretch of music in the background makes the room feel like a gathering and not a drop-off point."),
  ("love that. any ideas for getting the quiet people to join in",
   "A card system helps, because it lets shy colleagues join without having to speak. Ask people to tape a short note inside the cover of the book they bring, then let the person who takes it read the note. Pair it with a low-pressure prompt on the invitation, such as bring a book that surprised you, which gives everyone an easy way in."),
  ("should i ask for a head count in advance",
   "A head count is useful but not essential. A quick poll or a sign-up sheet in the kitchen tells you roughly how many people to expect, which helps with the snacks and the table space. Treat it as a rough guide rather than a firm list, since colleagues often decide on the day."))
Q(STYLE, "bookswap", "build", "can you help me word the invitation",
  "Keep it short and warm: the date, the break room, the one book in, one book out rule, and a line asking people to tape a note inside the cover. Adding the prompt about a book that surprised you gives quiet colleagues an easy way in, and saying that leftovers go to a local library tells everyone where their books end up.",
  ref=("break room", "one book in"))

# ------------------------------------------------------------------ garage gym (build)
T(STYLE, "gym", "short",
  ("i'm turning half of my garage into a home gym, it's about 10 by 12 feet and the floor is concrete",
   "Concrete is a good base for a gym because it is solid, but it is hard on joints and on dropped weights. Rubber stall mats from a farm supply store cover a 10 by 12 area cheaply and cushion the floor. Add them before the rest, since the rest sits on top."),
  ("i have 600 dollars. i want strength stuff, not cardio",
   "For 600 dollars, an adjustable bench, a barbell with plates and a pair of adjustable dumbbells give you the most range. Buying used plates and a bar from a local marketplace often cuts the price by half. A squat rack is a wise addition when you can afford it, because it makes heavy lifts safer when you train alone."),
  ("ok. what order do i buy things in",
   "Start with the mats, then dumbbells and a bench, since those let you train safely from day one. Add the barbell and plates next. The rack comes last, once you know how often you are using the gym."))
Q(STYLE, "gym", "build", "should i worry about the garage being cold in winter",
  "A little, yes. Concrete holds the cold, so the rubber mats will help your feet, and a small electric heater on a timer can take the edge off before a session. Spend a few extra minutes warming up when it is cold, especially before anything heavy.",
  ref=("rubber", "concrete"))

# ------------------------------------------------------------------ scanning old photos (build)
T(STYLE, "photos", "short",
  ("i found two shoeboxes of old family photos and want to scan them before they fade",
   "Scanning before they fade is a lovely goal, and the first step is to sort them, since you will want to scan them in groups. A flatbed scanner at 600 dots per inch is plenty for prints, and a phone scanning app is a fine shortcut if you have a lot. Handle the photos by their edges and wipe the glass between batches."),
  ("ok. how do i name the files so i can find them later",
   "Use the year first, then a short description, like 1987 beach trip. Starting with the year keeps them in order when you sort by name. If you do not know the exact year, put the decade and fix it later when you ask relatives."),
  ("and where do i keep them so they don't get lost",
   "Keep one copy on your computer, one on an external drive and one in a cloud folder, so a single failure cannot take them all. Name the folder clearly and add a short text note listing what is inside. Check the drive once a year to make sure the files still open."))
Q(STYLE, "photos", "build", "my aunt has a box too, can she help",
  "She can, and it would be a good way to name the people in the pictures, since she may know who is who. Ask her to write names and years on the back of the prints in pencil, then scan her box with the same year first naming and keep it on the external drive with yours.",
  ref=("year first", "external drive"))

# ------------------------------------------------------------------ memory quilt (build)
T(STYLE, "quilt", "short",
  ("my grandmother's old shirts are in a bag and i want to make a memory quilt from them. i've never quilted",
   "A memory quilt is a thoughtful project, and shirts are a good place to begin because the fabric is already softened by wear. The main decision is how large to make the squares, and 6 inch squares are easy for a first quilt. Cotton shirts are the simplest to sew, so sort those out from any stretchy ones."),
  ("there are about 12 shirts, some are flannel",
   "Twelve shirts can give you a lap quilt, especially if you take squares from the back panels and the sleeves too. Flannel is cozy but stretches, so iron a light fusible backing onto it before you cut. Mixing flannel and cotton works well when each piece has the same backing."),
  ("do i need a sewing machine",
   "A machine makes it quicker, but a quilt can be sewn by hand if you are patient. For a first quilt with straight seams, a basic machine is plenty and often available at a library makerspace or from a friend. Press every seam flat as you go, since that does more for a neat result than any other step."))
Q(STYLE, "quilt", "build", "how big will it end up with 12 shirts",
  "With 12 shirts cut into 6 inch squares, you can expect a lap quilt, about the size of a throw blanket. Taking squares from the backs and sleeves will help you reach it, and the flannel needs its backing first so it does not stretch.",
  ref=("6 inch", "flannel"))

# ------------------------------------------------------------------ solar system poster (fact)
T(STYLE, "poster", "short",
  ("i'm helping my nephew make a solar system poster for school",
   "A solar system poster is a fun project, and the planets are easy to put in order: Mercury, Venus, Earth, Mars, Jupiter, Saturn, Uranus and Neptune. Using circles cut from colored paper in rough relative sizes makes the gas giants look appropriately huge. A small label under each planet with one fact keeps the poster readable."),
  ("he picked mars as the planet to feature",
   "Mars is a great choice because it has so much to show: red dust, the tallest volcano in the solar system and polar ice caps. A reddish orange circle with a darker patch for the volcano region would look striking. He might add one line about how a Martian day is slightly longer than an Earth day."),
  ("what color should the background be",
   "Dark blue or black, because it makes the planets stand out and suggests space without any extra effort. A scattering of small white dots made with a paint pen or a sticker sheet adds stars. Keep the area behind the text lighter so the labels stay easy to read."))
Q(STYLE, "poster", "fact", "how many moons does mars have",
  "Mars has two small moons, Phobos and Deimos. Both are lumpy and much smaller than our moon, so a pair of tiny gray dots next to the red circle would show that nicely.",
  ok=("Phobos", "Deimos"))

# ------------------------------------------------------------------ paper cranes (fact)
T(STYLE, "cranes", "short",
  ("my friend is recovering from surgery and i want to fold paper cranes for her",
   "Paper cranes are a kind gesture, and the folding is calming in itself. A square of lightweight origami paper is the best material, and 6 inch squares are easy to handle. If you are new to it, learn the base fold slowly and the rest will follow."),
  ("someone told me there's a tradition about a thousand of them",
   "There is, and it comes from Japan. The idea is that anyone who folds a thousand cranes is granted a wish, often a wish for health or a long life. Many people fold smaller strings of cranes, and that is meaningful too."),
  ("should i write anything on them",
   "A short wish or a kind line written on the inside of the wings is a lovely touch, because it stays hidden until the crane is opened. Use a fine felt pen and write before you fold, since the paper is flat and easier to mark then. Keep it brief so the folds stay crisp."))
Q(STYLE, "cranes", "fact", "why cranes specifically?",
  "In Japanese tradition the crane is said to live for a thousand years, which is why it came to stand for long life and good fortune. That is the reason cranes are folded for people who are recovering, and even a single string carries the wish.",
  ok=("Japanese",))

# ------------------------------------------------------------------ bagels (fact)
T(STYLE, "bagel", "short",
  ("trying to make bagels at home for the first time, the recipe says to boil them first and it seems weird",
   "Boiling is what makes a bagel a bagel, so that step is correct. Dropping the shaped dough into simmering water for about a minute on each side sets the outside before the oven. Adding a spoonful of honey or baking soda to the water gives a deeper color and shine."),
  ("ok. high gluten flour or regular?",
   "High gluten flour gives the chewiest crumb, but bread flour is a good compromise if you do not want to order something special. Either way, knead the dough until it is stiff and smooth, because a firm dough holds the ring shape. Let the shaped rings rest for a short time before the boil."),
  ("what temperature should the oven be",
   "About 20 minutes in a hot oven, around 220 degrees Celsius, until they are deep golden. Turn the tray halfway through so they color evenly. Let them cool on a rack for a few minutes before slicing, because the inside is still setting."))
Q(STYLE, "bagel", "fact", "but why does boiling make them chewy",
  "The hot water gelatinizes the starch on the surface, which forms a thin, firm skin before the dough bakes. That skin keeps the bagel dense inside and gives the outside its shiny, chewy bite. Without the boil you would get a bread roll with a hole in it.")


# =====================================================================================================
# checks (the session_chat_r4 rules, applied to these threads)
# =====================================================================================================
MODES = {"short": (3, 8), "long": (6, 10), "digest": (22, 24)}
KINDS = {RECALL: ("rec_user", "rec_asst", "rec_both", "rec_none"), STYLE: ("emo", "close", "build", "fact")}
RECALL_KINDS = ("rec_user", "rec_asst", "rec_both")
_FILLER_ASSISTANT = {a for _u, a in FILLER}


def history_of(tid):
    """The exchanges of thread tid as the build seeds them: the written ones, then (digest) the fillers."""
    th = THREADS[tid]
    ex = list(th["ex"])
    if th["mode"] == "digest":
        n = DIGEST_N[tid]
        avoid = [r for q in QS[th["fam"]] if q["thread"] == tid for r in (*q["ref"], q["absent"])]
        ex += _fillers(tid, n - len(ex), avoid)
    return ex


def _check_thread(tid):
    th = THREADS[tid]
    ex = history_of(tid)
    lo, hi = MODES[th["mode"]]
    assert lo <= len(ex) <= hi, f"{tid}: {len(ex)} exchanges for mode {th['mode']}"
    long = th["mode"] == "long"
    for k, (u, a) in enumerate(ex):
        written = k < th["opening"]
        S._voice(a, f"{tid} history[{k}]", long=long)
        assert len(a) >= (250 if long else 90 if written else 30), f"{tid} history[{k}] too curt: {a!r}"
        assert not EMOJI.search(u), f"{tid}: emoji in a user turn"
    seen = {}
    for k, (u, a) in enumerate(ex):
        for s in S._sents(a):
            ns = S._norm(s)
            if len(ns.split()) >= 3:
                assert ns not in seen, f"{tid}: history sentence repeats (turns {seen[ns]} and {k}): {s!r}"
                seen[ns] = k
    users = [S._norm(u) for u, _ in ex]
    assert len(set(users)) == len(users), f"{tid}: a user turn repeats"
    chars = sum(len(u) + len(a) for u, a in ex)
    if th["mode"] == "long":
        assert chars >= 2200, f"{tid}: long thread of {chars} chars"
    if th["mode"] != "digest":
        assert chars < S.MAX_HISTORY_CHARS - 300, f"{tid}: history {chars} chars"
    else:
        assert chars <= 30000, f"{tid}: history {chars} chars"
    return ex


def _check_q(q):
    tid = q["thread"]
    th = THREADS[tid]
    ex = history_of(tid)
    fam, kind, prompt, final = q["fam"], q["kind"], q["prompt"], q["final"]
    assert kind in KINDS[fam], f"{tid}: kind {kind} not allowed in {fam}"
    n = len(ex)
    htxt = "\n".join(u + "\n" + a for u, a in ex)
    assert not asks_personal_data(prompt), f"host would force a tool for this prompt: {prompt!r}"
    assert not capability_question(prompt), f"host reads this as a capability question: {prompt!r}"
    hlow, flow = htxt.lower(), final.lower()
    assert prompt.lower() not in {u.lower() for u, _ in ex}, "prompt repeats an earlier user turn"
    assert 60 <= len(final) <= 650, f"final length {len(final)}"
    S._voice(final, "final")
    for r in q["ref"]:
        assert r.lower() in hlow, f"ref {r!r} not in history"
        assert r.lower() in flow, f"ref {r!r} not in final"
    if kind in RECALL_KINDS:
        assert q["ref"], "recall needs a ref"
        p = q["ref"][0].lower()
        where_u = [i for i, (u, a) in enumerate(ex) if p in u.lower()]
        where_a = [i for i, (u, a) in enumerate(ex) if p in a.lower()]
        if kind in ("rec_user", "rec_both"):
            assert where_u, f"{p!r} never said by the user"
        if kind in ("rec_asst", "rec_both"):
            assert where_a, f"{p!r} never said by the assistant"
        idx = sorted(set(where_u + where_a))
        assert idx[0] <= n - 2, f"{p!r} only in the last exchange"
        if th["mode"] == "digest":
            assert idx[-1] <= 1, f"{p!r} must sit in the oldest two exchanges of a digest chat"
            assert kind != "rec_both" or where_u == [0], "digest rec_both: the user states it in the first exchange"
            for u, a in ex[2:]:
                assert p not in (u + " " + a).lower(), f"{p!r} repeats later in the chat"
        else:
            assert idx[0] >= n - 10, f"{p!r} first appears {n - idx[0]} exchanges back"
    if kind == "rec_none":
        assert q["absent"] and q["absent"].lower() not in hlow, f"absent {q['absent']!r} is in the history"
        assert S.NOT_SAID.search(final), "rec_none final must say it did not come up"
        assert not q["ref"], "rec_none carries no recall refs (the detail is absent; ctx names the context)"
        for c in q["ctx"]:
            assert c.lower() in hlow, f"ctx {c!r} not in history"
    else:
        assert not q["absent"] and not q["ctx"], "absent/ctx are for rec_none"
    if kind in ("emo", "build"):
        assert q["ref"], f"{kind} needs a ref to an earlier specific"
    if kind == "emo":
        assert len(final) >= 150, "emotional final too short"
    if kind == "close":
        assert len(final) <= 200 and "?" not in final, "a closer is one or two short sentences, no question"
    extra = S._ents(final) - S._vocab(htxt + "\n" + prompt)
    if kind not in ("fact", "build"):
        assert not q["ok"], "ok is for fact and build turns only"
    extra -= set(q["ok"])
    assert not extra, f"final names things absent from the history and prompt: {sorted(extra)}"
    seen = set()
    for u, a in ex:
        for s in S._sents(a):
            seen.add(S._norm(s))
    for s in S._sents(final):
        assert S._norm(s) not in seen, f"final repeats an earlier assistant sentence: {s!r}"
    fopen = " ".join(S._norm(final).split()[:4])
    for k, (u, a) in enumerate(ex):
        assert fopen != " ".join(S._norm(a).split()[:4]), f"final opens like assistant turn {k}"


def check_all():
    """Raise AssertionError listing every authoring problem; return (threads, questions) counts."""
    errs = []
    for tid in THREADS:
        try:
            _check_thread(tid)
        except AssertionError as exc:
            errs.append(f"[{tid}] {exc}")
    for fam in QS:
        for q in QS[fam]:
            try:
                _check_q(q)
            except AssertionError as exc:
                errs.append(f"[{q['thread']}/{q['kind']}] {q['prompt']!r}: {exc}")
        prompts = [q["prompt"].lower() for q in QS[fam]]
        assert len(set(prompts)) == len(prompts), f"{fam}: duplicate prompts"
        firsts: dict[str, list[str]] = {}
        for q in QS[fam]:
            firsts.setdefault(_first3(q["final"]), []).append(q["prompt"])
        crowded = {k: v for k, v in firsts.items() if len(v) > 2}
        if crowded:
            errs.append(f"{fam}: openers used more than twice: " + "; ".join(f"{k!r} x{len(v)}" for k, v in crowded.items()))
    for tid, th in THREADS.items():
        if not any(q["thread"] == tid for q in QS[th["fam"]]):
            errs.append(f"thread {tid} has no question")
    assert not errs, "\n".join(errs)
    return len(THREADS), sum(len(v) for v in QS.values())


# =====================================================================================================
# the families
# =====================================================================================================
def _digest_check(sc, refs, kind):
    """The condensed note must exist and carry what the answer states; that fact must not be verbatim in the request."""
    policy, _ev, _fin = H.run_scenario(sc.prompt, copy.deepcopy(sc.steps), sc.world, sc.when, profile=sc.profile,
                                       approve=getattr(sc, "approve", None))
    msgs = policy.requests[0]["messages"]
    sent = "\n".join(m["content"] if isinstance(m["content"], str) else str(m["content"])
                     for m in msgs[:-1] if m["role"] in ("user", "assistant"))
    notes = [m["content"] for m in msgs if m["role"] == "system" and isinstance(m.get("content"), str)
             and m["content"].startswith(DIGEST_HEADER)]
    if not notes:
        return False, "no 'Earlier in this conversation' note in the request"
    if kind != "rec_none":
        for nd in refs:
            if S._norm(nd) not in S._norm(notes[0]):
                return False, f"{nd!r} did not survive into the condensed note"
            if S._norm(nd) in S._norm(sent):
                return False, f"{nd!r} is still verbatim in the request"
    if not all(t["user"] in sent and t["assistant"] in sent for t in sc.history[-12:]):
        return False, "the host trimmed recent history"
    return True, ""


def _build(fam, rng):
    check_all()
    qs = list(QS[fam])
    qs = _interleave(qs, lambda q: q["kind"], random.Random(f"{fam}-order"))
    out, bad = [], []
    for q in qs:
        tid = q["thread"]
        th = THREADS[tid]
        ex = history_of(tid)
        owner = "\n".join(a for u, a in ex) + "\n" + q["final"]
        names = S._names("\n".join(u + "\n" + a for u, a in ex) + "\n" + q["final"], owner)
        protect = ({t.lower() for r in (*q["ref"], *q["ctx"]) for t in S.TOK.findall(r)}
                   | {t.lower() for t in S.TOK.findall(q["absent"] or "")} | set(names)
                   | {n[1:] for n in names})        # _hurried only swaps letters inside a lowercase run, so protect it too
        hist = [turn(S._surface(tid, u, names, protect), a) for u, a in ex]
        prompt = S._surface(tid, q["prompt"], names, protect, prompt=True)
        slips = [(t, w) for o, t in zip([u for u, _ in ex] + [q["prompt"]], [h["user"] for h in hist] + [prompt])
                 for w in S._cap_slips(o, t, owner)]
        assert not slips, f"[{tid}] capitalised common word in a user turn: {slips[:3]}"
        now, w = setup(rng)
        w.history = hist
        sc = Scenario(fam, prompt, now, w, [Final(q["final"])],
                      f"{fam} ({q['kind']}): {len(hist)} earlier exchanges, answered from the conversation or "
                      f"general knowledge, no tool")
        sc.history = hist
        sc.approve = None
        sc.expect_cards = []
        sc.must_see = []
        sc.thread = tid
        sc.qkind = q["kind"]
        if q["kind"] in RECALL_KINDS:
            sc.recall_refs = tuple(q["ref"])
        elif q["kind"] == "rec_none":
            sc.recall_absent = q["absent"]
            sc.recall_ok = tuple(q["ok"])
        ok, why = F._check(sc)
        if ok and th["mode"] == "digest":
            ok, why = _digest_check(sc, q["ref"], q["kind"])
        elif ok:
            policy, _e, _f = H.run_scenario(sc.prompt, copy.deepcopy(sc.steps), sc.world, sc.when,
                                            profile=sc.profile, approve=None)
            msgs = policy.requests[0]["messages"]
            if any(m["role"] == "system" and isinstance(m.get("content"), str) and m["content"].startswith(DIGEST_HEADER)
                   for m in msgs):
                ok, why = False, "the host condensed a chat meant to stay verbatim"
        if not ok:
            bad.append(f"[{tid}/{q['kind']}] {prompt!r}: {why}")
            continue
        out.append(sc)
    assert not bad, "dry-run failures:\n" + "\n".join(bad)
    return out


def fam_recall_dev_r4(rng):
    return _build(RECALL, rng)


def fam_session_style_dev(rng):
    return _build(STYLE, rng)


FAMILIES_R4 = {RECALL: fam_recall_dev_r4, STYLE: fam_session_style_dev} if ENABLED else {}


if __name__ == "__main__":
    print(check_all())
