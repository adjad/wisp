"""Round-4 family ``session_chat_r4``: casual conversation over a real seeded history.

Why. r3's multi-turn chat was the one chat category it lost (11-13): it invented context the user never gave
("that's the second time this year I've read about it"), repeated one curt line, stalled on a casual
factual follow-up ("I can't look up prices right now") and narrated the memory tool. In production an earlier
Ask turn is stored as ONLY the user text and the final assistant text (service/main.py), so a fact an earlier
reply or the user STATED can be answered from the conversation with no tool call, and a fact that never came up
is said plainly not to have come up.

Each scenario seeds 3-10 earlier exchanges (``scen_r3_followup.turn``, the real session store) and the target
turn is a single no-tool reply. Three sub-skills:

1. recall (~45%): the last message asks about, or builds on, something said 2-9 exchanges back, by the USER or
   by the ASSISTANT; the final states it exactly as it appears in the history. A few ask about something never
   said: the final says it did not come up and does not invent it.
2. building on context (~35%): continue the topic naturally, refer back to earlier specifics, do not restart,
   do not repeat an earlier reply, invent nothing about the user. Hard emotional threads (a loss, stress, a
   breakup) run 5-8 turns: warmth first, specific to what was said, no generic one-liners, at most one gentle
   question, never the same sentence twice in a session.
3. mid-conversation factual questions (~20%): a casual factual follow-up is answered from general knowledge,
   not stalled; every number or name the history does not hold is whitelisted per scenario (``ok``).

Round-4 review: 12 long threads (``TL``: 8 exchanges of base-length replies (about 60 words and up), 4,000-4,800 characters of history) join the 43
short ones, and every thread feeds at most CAP=3 scenarios, so no conversation yields more than three rows (54 distinct
conversations). The proper-case register now capitalises only names that the assistant never writes lowercase, and
``fam_session_chat_r4`` asserts that no common word got a mid-sentence capital in a user turn.

Voice is the r4 chat voice: 2-4 sentences, median about 250-350 characters, one concrete touch, a natural
follow-up on about 40%, no emoji, no name-addressing, no ritual closers, no machinery.

``_check_q`` enforces the mechanical rules at build time; the host dry-run (scen_r3_followup._check) proves each
scenario runs as scripted and the host leaves the final alone.
"""
from __future__ import annotations

import re

from harness import Final
from scen import Scenario, setup
from scen_r3_chat import BANNED_OPENERS, CLOSERS, EMOJI, PERSONA, _first3
import scen_r3_followup as F
from service.model_only import asks_personal_data, capability_question
from scen_r3_followup import turn

FAMILY = "session_chat_r4"
WRITE_ALLOW_R4 = {FAMILY: frozenset()}

THREADS: dict[str, list[tuple[str, str]]] = {}
QS: list[dict] = []


def T(name, *ex):
    assert name not in THREADS, name
    THREADS[name] = [(u, a) for u, a in ex]


def Q(thread, kind, prompt, final, *, ref=(), absent=None, ok=(), cut=None):
    """kind: rec_user | rec_asst | rec_both | rec_none | build | emo | fact | close."""
    QS.append(dict(thread=thread, kind=kind, prompt=prompt, final=final, ref=tuple(ref), absent=absent,
                   ok=tuple(o.lower() for o in ok), cut=cut))


# =====================================================================================================
# threads and the questions asked at their end (or at an earlier cut)
# =====================================================================================================

# ------------------------------------------------------------------ niece's party
T("niece",
  ("my niece's birthday is coming up and i have no idea what to do for it",
   "A kid's party works best around one clear idea, so a theme helps a lot. What is she into right now, and how old is she turning?"),
  ("ivy is turning 7 and she is completely obsessed with dinosaurs",
   "Seven and dinosaur-obsessed is a lovely combination. You could build the whole party around a fossil dig, with plaster eggs and toy bones buried in a tub of sand. Kids that age take digging very seriously."),
  ("ooh a fossil dig, love it. party is saturday at cedar park, about 12 kids",
   "A park suits it well, since the mess stays outside. With 12 kids, two small dig stations will beat one big one, so nobody waits long with a brush in their hand. A few picnic blankets can be the base camp."),
  ("good call. what about cake",
   "A dinosaur cake does not have to be fancy: a loaf cake with green frosting and a few plastic dinosaurs stepping through it looks great. Does Ivy have a favorite flavor, or is that a family negotiation?"),
  ("she likes lemon, her mom wants chocolate lol",
   "Lemon wins on theme, since it goes nicely with a green frosting swamp. A chocolate cupcake on the side would keep her mom happy too."))
Q("niece", "rec_asst", "what was the activity you suggested again?",
  "A fossil dig: plaster eggs and toy bones buried in a tub of sand, with brushes for the kids to uncover them. Splitting the 12 kids across two stations keeps the line short. Real brushes make it feel like proper archaeology, which kids that age love. Are the toy bones sorted yet, or still on the hunt?",
  ref=("fossil dig", "12"))
Q("niece", "rec_none", "what's ivy's favorite color btw",
  "You haven't mentioned her favorite color. All I know is that she loves dinosaurs and lemon cake. If you want a color scheme for the party, green and sand would suit the fossil dig nicely. Is there a color she always grabs first?",
  absent="favorite color", ref=("lemon", "dinosaurs"))
Q("niece", "build", "do goodie bags make sense too?",
  "Small ones are a nice touch, and they can carry the theme: a toy dinosaur, a sticker sheet and a paper bag with a stamped footprint on it. Since everyone will already have been digging, a little brush makes a fun keepsake. Three or four small things is plenty.",
  ref=("dinosaur", "digging"))
Q("niece", "build", "what if it rains saturday",
  "A rainy day does not have to cancel the dig. Moving it under a pavilion or into a garage works fine, and a tarp over the sand tub keeps the fossils dry. You could even tell the kids it is an expedition in bad weather, which makes it more exciting than the sunny version.",
  ref=("dig", "sand"), cut=3)

# ------------------------------------------------------------------ portugal trip
T("portugal",
  ("thinking about a trip to portugal in may with my sister dani",
   "May is a lovely time for it, warm but not yet crowded. How many days do you have, and are you more after cities or coast?"),
  ("9 days. we want food and a bit of beach, not too many museums",
   "Nine days is a comfortable length for Lisbon and Porto with a little coast in between. Both are walkable and very good for eating, and neither demands a museum marathon."),
  ("ok lisbon first then porto. any food i should not miss",
   "In Lisbon, the custard tarts at Pastéis de Belém are the classic, and it is worth going early before the line grows. Porto has the francesinha, a gloriously heavy sandwich with melted cheese and a spicy sauce. Share one between you and a nap will follow."),
  ("lol noted. is a day trip from lisbon worth it?",
   "Yes, Sintra is the usual choice: a hilltop town with a colorful palace and misty forest paths, about 40 minutes by train. Start early, because the palace gets busy by late morning. Dani might like it if she enjoys a bit of fairy-tale architecture."),
  ("she will love that. we are flying in on the 3rd",
   "The 3rd gives you a calm start, and landing in Lisbon means you can spend the first evening just wandering and eating. Keep that first day light and the rest of the trip will feel much easier."))
Q("portugal", "rec_user", "remind me, lisbon or porto first",
  "Lisbon first, then Porto. It is a sensible order, since you can finish the trip with the francesinha and a slow last evening. Landing in Lisbon on the 3rd also means the first evening can be a slow wander and a long dinner.",
  ref=("Lisbon", "Porto"))
Q("portugal", "rec_none", "which airline are we flying?",
  "We have not got to the airline yet. What we have so far is Lisbon first, then Porto, 9 days in May, and landing on the 3rd. Once a flight is picked, the hotels you plan to book this weekend are the next step.",
  absent="airline", ref=("Lisbon", "9 days", "3rd"))
Q("portugal", "fact", "do people tip in restaurants there?",
  "Tipping is appreciated but not expected. Leaving a few coins or rounding up the bill is common, and a bigger tip is only for really good service. Service is not usually added automatically, so check the bill if you are not sure.",
  ok=())
Q("portugal", "build", "dani wants a rooftop dinner on our first night in lisbon, is that too ambitious after a flight",
  "It is a lovely idea if you keep it easy: a late dinner somewhere close to your hotel in Lisbon, so you are not crossing the city while tired. A rooftop is especially nice at sunset in May. Booking a table for a bit later than you think you need leaves room for delays at the airport.",
  ref=("Lisbon", "May"))

# ------------------------------------------------------------------ dinner party menu
T("menu",
  ("hosting dinner for 6 on friday and i'm panicking about the menu",
   "Six is a friendly number, and Friday gives you time. Is anyone coming with dietary needs, and do you want to cook a lot on the day or prepare ahead?"),
  ("noor is vegetarian, and i want to do as much ahead as possible",
   "Then build a menu that is vegetarian from start to finish, so Noor is not eating a side dish while everyone else has the main. Soups, braises and tarts all hold up well when made ahead."),
  ("ok so starter ideas?",
   "A lentil soup with lemon and a swirl of yogurt is warming, cheap and gets better overnight. Make it Thursday and heat it up on Friday, and the first course is done before anyone arrives."),
  ("yes lentil soup. main?",
   "A mushroom stroganoff over buttered noodles is rich enough to feel like an occasion, and it reheats well. Add a green salad with a sharp dressing to cut through the cream."),
  ("love it. dessert would be nice to be easy",
   "A lemon tart is simple if you buy a good shell, and the filling can be made a day before. A bowl of whipped cream on the side is all it needs."))
Q("menu", "rec_both", "remind me what we decided for the menu",
  "Lentil soup with lemon to start, mushroom stroganoff over buttered noodles with a green salad for the main, bread with salted butter, and a lemon tart for dessert. It is vegetarian all the way through, so Noor can eat everything. The soup is the only thing for Thursday, and the stroganoff and tart come together on Friday.",
  ref=("lentil soup", "stroganoff", "lemon tart"))
Q("menu", "rec_user", "wait, who's the vegetarian",
  "Noor. That is why the whole menu is vegetarian, so nobody at the table has a different plate. Cooking one menu for everyone also means no second pan on the stove on Friday.",
  ref=("Noor",))
Q("menu", "fact", "how far ahead can i make the soup without it being weird",
  "Lentil soup keeps well in the fridge for about three days, and it often tastes better after a night. Make it on Thursday and it will be well within that range. If you want to go further ahead, freeze it and thaw it in the fridge the day before.",
  ok=())
Q("menu", "build", "what do you pour with all this, i know nothing about wine",
  "With the mushroom stroganoff, a soft, earthy light red such as Pinot Noir is a good match. A crisp white is a safe backup, and it sits well with the lentil soup and the lemon tart too. A good sparkling water with lemon on the table takes care of anyone who is not drinking.",
  ok=("Pinot", "Noir"), ref=("mushroom stroganoff", "lemon tart"))

# ------------------------------------------------------------------ movie recs
T("films",
  ("i just watched arrival and i can't stop thinking about it",
   "That film does stay with you, because the ending recasts everything before it. What got you most: the language puzzle or the emotional turn?"),
  ("the emotional part honestly. i want more sci-fi like that, quiet and sad",
   "Then Contact is a good place to start, a patient film about listening for a signal and what it costs to believe in it. Ex Machina is the colder choice, small and tense, with only a few characters."),
  ("i've seen ex machina already, loved it",
   "Then you have the stranger half covered. Annihilation is the next step in the same direction, beautiful and unsettling, though it is much less quiet than Arrival."),
  ("hm annihilation is a bit scary for me. contact then",
   "Contact it is. It is long and unhurried, and Jodie Foster carries it beautifully. Give it a quiet evening, because it asks for patience."),
  ("ok. any older one in that mood?",
   "Solaris, the 1972 version by Tarkovsky, is the slow, melancholy classic of the genre. It is very long and dreamlike, so it suits a night with no plans. Moon, from 2009, is shorter and more direct if you want something gentler."),
  ("hm that's a lot. i'll save solaris for a long weekend",
   "Saving it for a long weekend is wise, since it rewards a clear head and no interruptions. For a single evening, Moon is the better fit: one lone astronaut, a quiet base and a very good performance."))
Q("films", "rec_asst", "you recommended a movie earlier, what was it again?",
  "Contact was the one you picked, a patient film about listening for a signal. Before that I mentioned Ex Machina, which you had already seen, and Annihilation, which you thought was a bit scary. Solaris and Moon came later as older options. Which of them are you leaning toward?",
  ref=("Contact", "Ex Machina", "Annihilation"))
Q("films", "rec_user", "i said one of them was too scary for me, which",
  "Annihilation. You said it was a bit scary, so we went with Contact instead. It is beautiful to look at, but the tension is exactly what you wanted to leave out. Moon would be the gentler option if you want another in the same mood.",
  ref=("Annihilation", "Contact"))
Q("films", "rec_none", "what streaming service was it that we talked about?",
  "We haven't settled on a streaming service yet. We have only talked about the films, Arrival, Ex Machina, Contact and the older ones. Tell me where you watch and we can plan the order around it. Contact is the one you chose, so that can go first, with Moon as the short backup for a weeknight.",
  absent="streaming", ref=("Arrival", "Contact"))
Q("films", "build", "i think i'll do contact tomorrow night, any homework before it?",
  "No homework needed, since Contact stands alone. It does help to be in a patient mood, because it takes its time and rewards it. Since you loved Arrival, the pacing should feel familiar. Dim lights and no phone nearby suit a film that lives on its silences.",
  ref=("Contact", "Arrival"))
Q("films", "fact", "is solaris actually based on a book",
  "Yes, a novel by the Polish writer Stanisław Lem, published in 1961. Lem was said to dislike the Tarkovsky film because it focused more on the human drama than on the strangeness of the planet. There is also a 2002 remake with a more compact story. Do you want to read the novel first, or see the film?",
  ok=("Polish", "Stanisław", "Lem", "1961", "Tarkovsky", "2002"))

# ------------------------------------------------------------------ reading slump
T("reading",
  ("i've been in a reading slump for months",
   "A slump usually means the next book has to be easy to start rather than impressive. What was the last thing you read and loved?"),
  ("the overstory. took me forever but i loved it",
   "That is a big, generous book, so a long recovery afterward makes sense. What did you love most about it, the trees or the people?"),
  ("the trees. i never thought i'd care about trees this much",
   "Books like that can change how you walk through a park. For the same feeling in a shorter shape, try Braiding Sweetgrass: essays about plants, with a gentle, curious voice."),
  ("i might try it. but also something light. a novel i can finish in a weekend",
   "Piranesi is short, strange and warm, a mystery set inside an endless house of statues and tides. It can be read in a couple of sittings, and it rewards a slow, curious mood."),
  ("ok piranesi sounds good. my library has it",
   "Then you are set. Borrow it on a quiet evening and give yourself a rule that you only have to read ten pages, since the slump usually breaks once the first chapter lands."))
Q("reading", "rec_user", "what was the name of that book i mentioned loving?",
  "The Overstory. You said it took you forever but you loved it, mostly for the trees. Braiding Sweetgrass is the closest follow-up if you want more of that feeling. Do you want to read that after Piranesi, or sooner?",
  ref=("The Overstory", "trees"))
Q("reading", "rec_asst", "and the nonfiction one about plants?",
  "Braiding Sweetgrass, a book of essays about plants with a gentle, curious voice. It pairs well with The Overstory, since both change how you look at a park. Reading the essays in small pieces suits a slump, since you can stop after any one of them. Do you want to read it after Piranesi?",
  ref=("Braiding Sweetgrass", "Overstory"))
Q("reading", "fact", "who wrote the overstory, did we say?",
  "Richard Powers wrote The Overstory. The author did not come up earlier, only the trees and how long it took you, but his other novels are just as big and full of ideas. Do you plan to read Piranesi first?",
  ok=("Richard", "Powers"), ref=("Overstory", "Piranesi"))
Q("reading", "build", "think an audiobook would work instead? i'm kind of tired of reading",
  "Listening counts as reading, and it is a good way around a slump. Piranesi is short enough to work well in audio. Since you loved The Overstory, a narrator on a walk might suit the trees too. Do you listen on walks, or mostly at home?",
  ref=("Piranesi", "Overstory"))
Q("reading", "fact", "how long does a normal novel take to listen to",
  "Most novels run between 8 and 12 hours as audiobooks, depending on the length and the narrator. A short one can be closer to 6, and a big one like The Overstory takes much longer. At the usual speed, an hour a day gets you through a typical novel in under two weeks.",
  ok=("8", "12", "6"))

# ------------------------------------------------------------------ first 10k
T("tenk",
  ("i signed up for a 10k in 8 weeks and i've barely run since school",
   "Eight weeks is plenty for a first 10k if you build up gently. How far can you run comfortably today, and have you had any aches?"),
  ("like 2 miles before i'm dying. left knee gets achy on downhills",
   "Two miles is a real starting point. An achy knee on downhills is common when the legs are not used to the load, so keep the first weeks mostly flat and shorten your stride on slopes. If it ever turns sharp rather than achy, rest and get it checked."),
  ("ok. my friend teo wants to run with me on tuesdays",
   "A weekly run with Teo is a big help, since a plan you share with someone is much harder to skip. Make Tuesday the easy, chatty run and save the longer one for the weekend."),
  ("what should the first couple weeks look like",
   "Try run-walk intervals: run 2 minutes, walk 1, for about 20 minutes, three days a week. It feels almost too easy, which is the point. Add a minute to the run portion each week as it starts to feel comfortable."),
  ("that sounds doable. i'll do saturday as the long one",
   "Saturday works well because it leaves a rest day before it. Keep the long run slower than you think you should; if you can still talk in short sentences, the pace is right."))
Q("tenk", "rec_user", "how far was it you said i can run before dying lol",
  "About 2 miles, with your left knee getting achy on downhills. That is a good base for the run-walk plan, which builds in small steps. Keeping the first weeks flat and gentle is the best protection for that knee. Does your usual route have many hills?",
  ref=("2 miles", "left knee"))
Q("tenk", "rec_none", "is the race on a saturday or sunday?",
  "You have not said which day it is, only that it is in 8 weeks. It is worth knowing, because your long run is best on the same weekday as the race. Once you know, you can slot the long runs onto that day each week. Do you know yet?",
  absent="sunday", ref=("8 weeks",))
Q("tenk", "fact", "is stretching before a run even worth it",
  "Easy movement works better than static stretching beforehand: a few minutes of brisk walking, leg swings and marching in place get the muscles ready. Save the longer holds for after the run, when everything is warm. That also gives you a pleasant cool-down with Teo on Tuesdays. Do you usually warm up before the Tuesday runs?",
  ref=("Teo",))
Q("tenk", "build", "my knee felt weird on thursday, is that the downhill thing again",
  "It could be, since your left knee gets achy on downhills. Rest it for a day or two, keep the next runs flat and short, and then ease back into the run-walk plan. If it feels sharp or swollen rather than achy, that is worth having a professional look at.",
  ref=("left knee", "downhills", "run-walk"))

# ------------------------------------------------------------------ sourdough starter
T("starter",
  ("i started a sourdough starter 5 days ago and nothing is happening",
   "Five days is often the quiet middle of the process, because the early bubbles can fade before the real culture takes over. How warm is your kitchen, and how often are you feeding it?"),
  ("kitchen is cold, like 64F. i feed it once a day, flour and water",
   "The cold is probably the main brake. Starters like a spot around 75F, so try the oven with just the light on. Feed it equal parts flour and water by weight."),
  ("i named it gerald btw",
   "Gerald is a good name for something that needs patience. Naming a starter makes it harder to neglect, which is half the battle."),
  ("lol. how much flour exactly",
   "Keep it simple: 50 grams of starter, 50 grams of flour and 50 grams of water each day, after tossing the rest. Stir it well and cover it loosely so it can breathe."),
  ("ok did that. the light-on-oven trick is smart",
   "It works because the bulb gives off just enough steady warmth. Put a note on the oven door so nobody preheats with Gerald inside."))
Q("starter", "rec_user", "what temperature was the kitchen again",
  "You said about 64F, which is a bit cool for a starter. The oven with the light on gets Gerald closer to the 75F range it likes. Starters are patient, so a slow start in the cold is normal. Did the warmer spot change anything yet?",
  ref=("64F", "75F", "Gerald"))
Q("starter", "rec_asst", "how much flour and water was it again",
  "Each day it is 50 grams of the starter you keep, plus 50 grams of flour and 50 grams of water, and you discard the rest. A loose cover lets Gerald breathe. A regular time each day, even a short one, is what keeps it lively.",
  ref=("50 grams", "Gerald"))
Q("starter", "fact", "how long until it's ready to bake with",
  "Most starters need one to two weeks of regular feeding to become reliable, and a cool kitchen can stretch that. It is ready when it doubles within 4 to 8 hours of a feeding and smells pleasantly tangy. Gerald is on the early side, so a few more days is normal.",
  ok=("4", "8"), ref=("Gerald",))
Q("starter", "build", "gerald has a few bubbles today!!",
  "That is the first sign of life, and it fits with the warmer spot. Keep feeding the same 50 grams of flour and water each day and let it keep climbing. A rubber band on the jar marks the level, so you can see exactly how much it rises. Do the bubbles reach the bottom of the jar, or only the surface?",
  ref=("50 grams",))

# ------------------------------------------------------------------ job interview
T("interview",
  ("i have an interview on wednesday at 2pm and i'm nervous",
   "Nerves usually mean you care about it, and a couple of days is enough to prepare well. What is the role, and which part worries you most?"),
  ("junior designer at a studio called northfield. i freeze on the tell me about yourself question",
   "Treat that one as a short story rather than a résumé. Say where you are now, one or two things that led you here, and why this studio makes sense, in about a minute."),
  ("that makes sense. my weakness answer is always bad",
   "Pick a real weakness that you are actively working on, and describe the fix. For a designer, taking feedback too personally early on and learning to ask for critique sooner sounds honest without being alarming."),
  ("ok i like that. what about asking them questions at the end",
   "Ask something that shows you have thought about the work: how the team gives feedback, what a first project might be, or how the studio decides what to take on. Skip pay and perks in a first round."),
  ("good. also i should bring a portfolio right? paper or laptop",
   "Bring the laptop with a short, polished selection of three or four projects, and a printed one-page backup in case the screen fails. Be ready to talk through your decisions more than the final visuals."),
  ("ok. what should i wear",
   "For a design studio, aim for tidy and a little personal: dark jeans or trousers, a good shirt or knit, and one detail that looks like you. Comfortable shoes matter more than they seem, since nerves and tight shoes do not mix."))
Q("interview", "rec_user", "i forgot, which time was the interview",
  "Wednesday at 2pm, at Northfield. That leaves time to rehearse the one-minute story about yourself a few more times. Looking over the studio's recent projects the night before gives you something real to mention.",
  ref=("Wednesday", "2pm", "Northfield"))
Q("interview", "rec_asst", "how many projects was the plan to bring",
  "Three or four, on the laptop, plus a printed one-page backup in case the screen fails. The talk is mostly about your decisions rather than the final visuals. Pick projects that show different skills, so the three or four do not feel repetitive. Do you have those few picked out?",
  ref=("three or four", "backup"))
Q("interview", "rec_none", "who's the interviewer, was that mentioned?",
  "You have not mentioned who it is, only the studio, Northfield, and the Wednesday 2pm slot. It is worth asking the recruiter for names ahead of time so you can look up their work. It also shows you are taking the interview seriously. Do you have a contact there?",
  absent="interviewer", ref=("Northfield", "Wednesday"))
Q("interview", "fact", "is it ok to ask about salary in a first interview",
  "It is usually better to wait until they bring it up or until a later round, unless the posting asked for your expectations. If they ask for a range first, give a considered answer rather than dodging. In the first round, keep the focus on the work and the team. Is that something you were thinking of raising?",
  ok=())
Q("interview", "build", "so the order is intro, then portfolio, then the questions?",
  "That order works well. Open with the one-minute story about where you are and why Northfield, walk through three or four projects, and finish with your questions about feedback and the first project. It leaves you ending on something forward-looking.",
  ref=("Northfield", "three or four"))

# ------------------------------------------------------------------ grief: grandfather
T("grandpa",
  ("my grandpa died on tuesday",
   "That is a heavy thing to carry, and I am glad you said it out loud. How are you holding up today?"),
  ("i'm okay i guess. i keep forgetting and reaching for my phone to call him on sundays",
   "That reaching is part of how much the habit meant, and it may keep happening for a while. A Sunday call is such a small, steady thing to lose. What did the two of you usually talk about?"),
  ("nothing really. his garden, the peach tree, baseball. he made peach jam every august",
   "Those ordinary subjects often hold the most love. A man who made peach jam every August sounds like someone whose kitchen smelled wonderful at the end of summer."),
  ("it did. the funeral is friday and my mom asked me to say a few words",
   "That is a big thing to be asked while you are grieving. You do not need it to be polished, since one or two true small stories, like the Sunday calls or the August jam, will say more than a long speech."),
  ("i don't know if i can get through it without crying",
   "Crying while you speak about him would be natural, and nobody there will mind. Writing it down and reading from the page gives you something to hold onto, and a pause to breathe is allowed."),
  ("ok. maybe i'll start with the jam",
   "That is a warm place to begin. You could say how the house smelled every August, and what he would say while stirring the pot."))
Q("grandpa", "emo", "i think i can write it tonight",
  "Tonight is a good time, while the jam and the Sunday calls are still close. Write it the way you would tell it to a friend, then trim it to a page. If a line makes you stop, that is probably the one worth keeping. Tomorrow's version can be softer and shorter, and that is allowed.",
  ref=("jam", "Sunday"))
Q("grandpa", "emo", "idk why im even telling you this",
  "Sometimes it is easier to say it to someone with no stake in how you should feel. The Sunday calls and the peach jam already tell me a lot about him. You can tell it in any order, and none of it has to make sense yet. What did he sound like when he talked about the tree?",
  ref=("Sunday", "peach jam"), cut=3)
Q("grandpa", "rec_asst", "what did you say about crying, i want to remember it",
  "That it would be natural, and that no one in the room would mind. A written page gives you something to hold, and stopping to breathe is allowed. Putting a small reminder at the top of the page, like breathe, can help in the moment.",
  ref=("page", "breathe"))

# ------------------------------------------------------------------ monstera
T("monstera",
  ("my monstera has two yellow leaves and i'm freaking out",
   "One or two yellow leaves at the bottom is usually the plant's way of saying it got too much water, or that it is shedding an old leaf. How often do you water it, and how much light does it get?"),
  ("every sunday, a good soak. it's by a north window",
   "A good soak every week can be a lot for a plant in a north window, where the light is gentle and the soil dries slowly. Press a finger two inches into the soil and only water when it feels dry."),
  ("oh. the soil is kind of wet right now actually",
   "Then skip this Sunday and let it dry out. Make sure the pot has a drainage hole, and empty the saucer so the roots are not sitting in water."),
  ("the pot has no hole. it's a cute ceramic one",
   "Cute but risky, since the water has nowhere to go. Keep the plant in its plastic nursery pot and drop that inside the ceramic one, then lift it out to water and let it drain in the sink."),
  ("ok that's easy. should i cut the yellow leaves off",
   "Yes, once a leaf is fully yellow it will not turn green again, so trim it near the stem with clean scissors. The plant can then spend its energy on the healthy ones."))
Q("monstera", "rec_user", "which window was the plant by again?",
  "A north window, where the light is gentle and the soil dries slowly. That is part of why the weekly soak was too much. Plants in low light grow slowly, so patience with the soil matters more than extra water.",
  ref=("north window",))
Q("monstera", "rec_asst", "what was the trick for the pot with no hole",
  "Keep the plant in its plastic nursery pot and set that inside the ceramic one. At watering time, lift it out, let it drain in the sink and put it back. That way you keep the cute pot and spare the roots. A saucer under the inner pot will catch drips on the way back in.",
  ref=("nursery pot",))
Q("monstera", "rec_none", "what kind of fertilizer were we using?",
  "We haven't mentioned fertilizer so far. We talked about the Sunday soak, the north window and the pot with no drainage hole. Feeding is rarely the cause when leaves turn yellow, so the watering and the pot are the better places to start.",
  absent="fertilizer", ref=("Sunday", "north window"))
Q("monstera", "fact", "how often should a monstera really get watered",
  "Roughly every one to two weeks, but the plant should decide rather than the calendar. Water when the top two inches of soil feel dry, which happens less often in winter and in a north window like yours. A good soak followed by full drainage beats small sips.",
  ref=("north window",))
Q("monstera", "build", "there's a tiny new leaf unrolling at the top, good sign?",
  "New growth is a very good sign. It means the plant is not in real trouble, even with the yellow leaves lower down. Keep the weekly soak on hold until the soil dries, and let the new leaf unfurl in that north window. Check the soil again in a week before deciding to water.",
  ref=("north window", "yellow"))

# ------------------------------------------------------------------ guitar
T("guitar",
  ("started learning guitar last week and my fingers hurt",
   "Sore fingertips are almost a rite of passage, and they do toughen up. How long are you practicing each day?"),
  ("like 15 minutes. i can do a g chord and an em kind of",
   "Fifteen minutes a day is the right amount to start, because short and regular beats long and rare. G and Em together are enough for a surprising number of songs."),
  ("really? which songs",
   "Knockin' on Heaven's Door is built on G and a couple of other easy chords, and Horse with No Name needs only Em and one other. Both are slow enough to play along with while your fingers learn."),
  ("knocking on heavens door yes! my dad loves that song",
   "That is a nice reason to learn it. Playing it for him one day would be a great goal to aim at, even if the first version is shaky."),
  ("is a year enough to play it properly",
   "For that song, a few weeks of steady practice is enough to play it recognizably. A year gives you a wide range of songs and a lot more confidence switching between chords."))
Q("guitar", "rec_asst", "what was the other song you said, the one with only two chords",
  "Horse with No Name, which needs only Em and one other chord. You already have Em, so the second shape is the only new thing to learn. Knockin' on Heaven's Door is the one with G and a couple of other easy chords. Both are slow and friendly, which suits fingers that are still learning.",
  ref=("Horse with No Name", "Em"))
Q("guitar", "rec_none", "which guitar did we say to buy?",
  "We haven't picked a guitar yet. So far we have talked about your sore fingertips, the 15 minutes a day, the G and Em chords and Knockin' on Heaven's Door. Is there one you are considering, or are you using one you already have?",
  absent="buy", ref=("15 minutes", "Knockin' on Heaven's Door"))
Q("guitar", "fact", "how long until fingertips stop hurting",
  "Usually two to four weeks of regular practice, as the skin on the fingertips thickens and the soreness fades. Short daily sessions work better than one long one, since the skin needs rest between rounds. Your 15 minutes a day suit that well.",
  ref=("15 minutes",))
Q("guitar", "build", "played g to em without stopping today!",
  "Switching smoothly between chords is the hardest part for beginners, so this is a real step. It means Knockin' on Heaven's Door is closer than you think. Try slowing the song right down and strumming once per chord, so your dad's favorite comes out steady.",
  ref=("Knockin' on Heaven's Door", "dad"))

# ------------------------------------------------------------------ gift for dad
T("dadgift",
  ("need a gift for my dad's 60th. he's impossible to buy for",
   "Sixtieth birthdays call for something with a bit of meaning, and hard-to-buy-for people usually have a hobby you can build on. What does he spend his weekends on?"),
  ("fishing mostly. and he's been building a bird house in the garage",
   "Fishing and woodworking give you good options. Something that connects the two, like a handmade tackle box or a set of quality chisels, would show you noticed what he enjoys."),
  ("budget is around $80. my brother kenji will split it with me",
   "With the two of you sharing $80, you could get a leather-bound fishing journal and a good pen, or a decent hand plane. Two people pooling for one nice thing usually beats two small things."),
  ("the journal is cute. what would he write in it",
   "Dates, spots, water conditions and what worked: a fishing journal turns into a record of good days. He could note the weather and the fly he used next to each catch."),
  ("he'd love that. should we add something small",
   "A good pen to go with it makes the journal feel finished. A waterproof one is practical for a man who spends his time by a river."))
Q("dadgift", "rec_user", "how much was the budget again",
  "Around $80, and Kenji is splitting it with you. That fits a leather-bound journal plus a decent pen, but a hand plane would take most of it. Splitting it also means neither of you has to stretch. Do you want to settle the journal first?",
  ref=("$80", "Kenji"))
Q("dadgift", "rec_asst", "what was the idea for the journal thing",
  "A leather-bound fishing journal, where he could write dates, spots, water conditions and what worked. Over time it turns into a record of good days. A waterproof pen alongside it would finish the gift off. He could also tuck a photo of his best catch inside the cover.",
  ref=("fishing journal", "waterproof"))
Q("dadgift", "rec_none", "what's his favorite fishing spot?",
  "We haven't talked about his favorite spot, only that he fishes a lot and builds things in the garage. If you know it, you could have its name written inside the cover of the journal as a personal touch. Do you know where he usually goes?",
  absent="favorite", ref=("journal", "garage"))
Q("dadgift", "fact", "is a hand plane a good tool for someone who's just starting woodworking",
  "It is not the easiest first purchase, since a hand plane needs sharpening and setting up before it cuts well. For someone already building a bird house it could be a lovely gift, though a decent one would take most of an $80 budget.",
  ref=("bird house", "$80"))
Q("dadgift", "build", "kenji says he'd rather get the plane. thoughts",
  "Both ideas work, and you can meet in the middle. The plane suits the garage projects, while the journal goes with the fishing. If $80 will not stretch to a good plane, the journal and pen are the safer fit. Which of the two would he use first, do you think?",
  ref=("journal", "$80"))

# ------------------------------------------------------------------ camping
T("camping",
  ("planning a camping trip to joshua tree in october with 4 friends",
   "October is a lovely month for the desert, with warm days and cool nights. Is anyone in the group experienced, and are you car camping or hiking in?"),
  ("car camping. rae is driving, she's done it before. nobody has a stove tho",
   "Car camping makes everything easier, and Rae's experience helps. A cheap two-burner stove or a single-burner canister stove would cover coffee and dinners, and a group of five can share one well."),
  ("ok good. what else do we usually forget",
   "Headlamps, a lighter, and layers. The desert is hot at midday and cold at night, so a warm jacket and a hat matter more than people expect. Bring more water than you think you need."),
  ("how much water",
   "Plan on about a gallon per person per day, and add some for cooking and washing up. With five people over a weekend, a big refillable jug in the car saves you a lot of small bottles."),
  ("perfect. we leave friday morning",
   "A Friday morning start gives you time to set up camp in daylight, which is the best time to find flat ground. Keep the first evening simple so the setup stays easy."))
Q("camping", "rec_user", "wait, did you say rae was driving?",
  "Rae is, and she has done it before. That helps the whole group, since she will know the little things like where to park and how early to leave. Having one confident person to lean on makes the logistics feel lighter. Is there a second driver in case it is a long day?",
  ref=("Rae",))
Q("camping", "rec_asst", "how much water per person did you say",
  "About a gallon per person per day, plus extra for cooking and washing up. A big refillable jug in the car works better than lots of small bottles when there are five of you. Water jugs also double as a quick wash for hands and dishes.",
  ref=("gallon",))
Q("camping", "rec_none", "which campsite did we book?",
  "We haven't decided on a campsite yet, only that you are heading to Joshua Tree in October with 4 friends and leaving on Friday morning. Booking is worth sorting soon, since desert campsites can fill up on busy weekends. Have you looked at what is available?",
  absent="campsite", ref=("Joshua", "Friday"))
Q("camping", "fact", "how cold does it get at night out there in october",
  "It can dip into the 40s Fahrenheit, which is a big swing from a warm day. A warm jacket, a hat and a good sleeping bag are what you will want after dark. The cold arrives quickly once the sun is down. Do you have a sleeping bag meant for cold nights?",
  ok=("40s", "Fahrenheit"), ref=("jacket",))
Q("camping", "build", "forgot to ask, what should we eat the first night",
  "Something that needs one pan and little cleanup, since you will be setting up camp on Friday. A big pot of chili, or pasta with jarred sauce and sausage, feeds five easily on one stove. Chop the vegetables at home to save time in the dark.",
  ref=("Friday", "stove"))

# ------------------------------------------------------------------ maid of honor speech
T("speech",
  ("my best friend hana is getting married in june and i'm the maid of honor. i have to give a speech",
   "That is a big honor, and a speech from a best friend is often the part people remember. Do you have a story in mind, or are you starting from nothing?"),
  ("i have this story about us getting lost in prague with her luggage",
   "A shared adventure with a mishap is perfect material, and a lost suitcase gives it a natural arc. Open with the moment things went wrong, then show how she handled it, because that tells the room who she is."),
  ("she just laughed the whole time, even when the bag was gone for two days",
   "That is the heart of your speech: someone who laughs when her luggage disappears is a person you want in your life. Let that line carry the toast, and link it to her marriage."),
  ("her fiance is marcus, i barely know him tbh",
   "Keep the part about Marcus short and honest. One sincere line about how happy she is around him says what you need without pretending to know more than you do."),
  ("how long should it be",
   "Around three minutes is plenty, which is roughly 400 words. Short and warm beats long and rambling, and a toast that leaves people wanting a bit more is a good one."),
  ("good. i'll draft it this weekend",
   "A weekend draft is a good plan. Write it all out first without worrying about the length, then trim, since most speeches improve in the cutting."))
Q("speech", "rec_user", "who's the groom again, going blank",
  "Marcus. You said you barely know him, so one sincere line about how happy Hana is around him is plenty. It is a short, honest line that still honors both of them. A quick chat with Hana about what she loves in him will give you something real to say.",
  ref=("Marcus",))
Q("speech", "rec_none", "what's the venue?",
  "You haven't told me the venue yet. All I know is that the wedding is in June and your story is about the lost luggage in Prague. Knowing the room can help, since a big hall asks for a slower pace than a backyard. A quick question to Hana about the guest count would tell you how to pace it.",
  absent="venue", ref=("June", "Prague"))
Q("speech", "fact", "is it ok to tell an embarrassing story in a toast",
  "A mildly embarrassing story is fine if it ends up showing how much you love the person. Avoid anything about exes, money or things she would not want her new in-laws to hear. The lost luggage story works because she comes out of it looking wonderful. Does Hana know the Prague story is coming?",
  ref=("luggage",))
Q("speech", "build", "wrote the first line: she laughs when the bag disappears",
  "A line like that is a great opening, because it shows her whole character in one image and promises a story. Following it with the two days in Prague gives the room a reason to lean in. Then you can turn toward Marcus in the last lines.",
  ref=("Prague", "Marcus"))

# ------------------------------------------------------------------ saving for a laptop
T("laptop",
  ("i want to save 2400 dollars for a laptop by december",
   "That is a clear goal, and a deadline helps. How much can you set aside each month, and where is the money going right now?"),
  ("about 300 a month. it's just in checking right now",
   "At 300 a month, you will need around eight months, so check how many months are left before December. Moving it out of checking into a separate savings account makes it harder to spend by accident."),
  ("yeah i keep dipping into it for food delivery",
   "That is a very common leak. Setting a weekly delivery limit, or buying a couple of easy groceries to cover the nights you are tired, usually saves more than people expect."),
  ("ugh true. ok i'll open a separate account",
   "Good plan. Name the account after the laptop if your bank allows it, and set up an automatic transfer right after payday so the 300 leaves before you can spend it."),
  ("what if i buy a refurbished one instead",
   "A refurbished laptop from the manufacturer or a reputable seller can save a good chunk of money and usually comes with a warranty. It might bring the goal down enough to reach it earlier."))
Q("laptop", "rec_asst", "what did you say to do about the food delivery thing",
  "A weekly delivery limit, plus a couple of easy groceries at home for the nights you are tired. It is a small change that tends to free up more money than people expect. Planning for a tired night in advance is usually what makes the difference.",
  ref=("weekly delivery limit",))
Q("laptop", "rec_none", "which bank did we pick?",
  "We haven't settled on a bank yet. We talked about opening a separate account with an automatic transfer after payday, but not where. If you are comparing, a credit union or an online bank is worth a look, since fees and rates matter on a goal like this.",
  absent="credit union", ref=("separate account",))
Q("laptop", "fact", "what's a high yield savings account",
  "It is a savings account that pays a higher interest rate than a regular one, usually offered by online banks. Your money stays accessible, and the interest is a small bonus on top of what you put in. For a goal like this, it is a good home for the 300 a month.",
  ref=("300",))
Q("laptop", "build", "so 8 months means i'd start in april or something",
  "Yes, April works. Eight months of 300 lands on the 2400 right as December starts, so starting then fits the goal. If you begin a bit later, a refurbished laptop gives you some slack. Setting up the automatic transfer first makes the start date matter less.",
  ref=("300", "2400"))

# ------------------------------------------------------------------ five-year-old's bedtime
T("bedtime",
  ("my son theo is 5 and bedtime has become a nightmare",
   "Five is a tricky age for bedtime, since imagination and independence are both growing. What does the evening look like at the moment?"),
  ("bath at 7, then he wants three more stories, then water, then he's up again",
   "A child buying more time with one small request at a time is very common at this age. A fixed routine with a clear end, like two books and one drink, tends to help more than arguing each night."),
  ("he cries if i say no more books",
   "Crying at a limit is normal at five and usually fades once the limit stays the same. Try telling him the plan before it starts, so the two books are something agreed rather than a surprise ending."),
  ("what about the water thing",
   "Put a small cup of water by his bed before lights out, so the water request has already been answered. It removes one of the reasons to call you back."),
  ("smart. he does like choosing things",
   "Then let him choose in safe ways: which two books, which pajamas, which night-light. Choices give him a feeling of control without stretching the bedtime."))
Q("bedtime", "rec_user", "remind me how old theo is",
  "Theo is 5. That is the age when a firm, friendly routine matters most, and when choosing between two options feels like a big deal to him. Giving him a say in small things, like the pajamas, makes the big limits easier to accept. Has he settled into the two books yet?",
  ref=("Theo is 5",))
Q("bedtime", "rec_asst", "what was the plan for the books, two or three",
  "Two books and one drink, then lights out. Telling Theo the plan before the routine starts makes it feel agreed rather than imposed. Praising him when he sticks to it works better than repeating the rule. Even a quiet well done at the door counts.",
  ref=("two books", "Theo"))
Q("bedtime", "rec_none", "what's his favorite book?",
  "We haven't talked about a favorite book yet. All I know is that he loves asking for more stories and likes choosing things, so letting him pick which of the two books to read might be a good start. Does he have a few that he keeps coming back to?",
  absent="favorite", ref=("stories", "choosing"))
Q("bedtime", "fact", "how much sleep does a 5 year old need",
  "Most children around 5 need about 10 to 13 hours of sleep across 24 hours. With the bath at 7 and two books afterward, he can be asleep early enough to get that. Is the lights-out time roughly the same each night?",
  ok=("10", "13", "24"), ref=("bath",))
Q("bedtime", "build", "tonight he accepted two books without crying!!",
  "Two books without tears is a real win, and a sign the plan is landing. Keep it the same again tomorrow, since repeating it is what makes it stick, and let him pick which two books. Praising the specific thing he did, like staying calm at lights out, helps it happen again.",
  ref=("two books",))

# ------------------------------------------------------------------ new camera
T("camera",
  ("bought a fujifilm x-t30 and i have no idea what i'm doing",
   "A good camera and plenty of curiosity are all you need. Do you want to shoot people, places or everyday things?"),
  ("landscapes mostly, i live near a lake",
   "A lake is a wonderful subject, because water and sky change all the time. For landscapes, try aperture priority mode with a middle aperture like f/8, which keeps most of the scene sharp."),
  ("f/8 got it. when's the best light",
   "Golden hour, the hour after sunrise and before sunset, when the light is warm and low. Shadows get long and the water picks up color."),
  ("i'm not a morning person lol",
   "Then evenings are your friend. Arrive 30 minutes before sunset, because the best color often comes just after the sun disappears."),
  ("ok nice. do i need a tripod",
   "A small tripod helps when the light gets low, because the camera needs a longer exposure and hands cannot hold that still. Even a cheap one will improve your sunset shots."))
Q("camera", "rec_user", "what was the camera, i keep forgetting",
  "A Fujifilm X-T30. It is a nice camera for landscapes, with a small body that is easy to carry around a lake. The small size also means it will not feel like a chore to bring along on a walk. Have you taken it out near the water yet?",
  ref=("Fujifilm X-T30",))
Q("camera", "rec_asst", "what aperture was that you said",
  "f/8, in aperture priority mode, which keeps most of the scene sharp. It is a safe middle setting for a landscape like a lake. In aperture priority the camera chooses the shutter speed, so you only have one decision to make. Are you shooting at dusk this week?",
  ref=("f/8", "aperture priority"))
Q("camera", "fact", "what's the difference between aperture priority and manual",
  "In aperture priority you choose the aperture, such as f/8, and the camera picks the shutter speed to match. In manual you set both yourself. Aperture priority is the easier way to learn, since the camera handles the exposure while you focus on the picture.",
  ref=("f/8",))
Q("camera", "build", "tried sunset at the lake and it came out all blurry",
  "Blur at sunset usually means the shutter speed dropped as the light faded and the camera shook a little. That is where a tripod earns its place, or any steady surface. Keep aperture priority at f/8 and let the camera rest on it. Did you have anything to steady the camera on?",
  ref=("tripod", "f/8"))

# ------------------------------------------------------------------ exam stress
T("exam",
  ("organic chem midterm is thursday at 9am and i feel like i know nothing",
   "That feeling is stressful, and it often shows up right before an exam even when you know more than you think. What part feels shakiest?"),
  ("mechanisms. i can't remember any of the steps",
   "Mechanisms feel impossible when you try to memorize them as lists. They are stories about electrons moving toward the positive side, and once you ask where the electrons want to go, the steps start to follow."),
  ("i've been studying 6 hours today and my head is fried",
   "Six hours is a long stretch, and tired brains hold less. Stopping now might be the most useful thing you do tonight, because sleep is when the day's studying settles."),
  ("but then i'll fall behind",
   "Rest and studying are not opposites here. Twenty minutes of walking or a shower, then one last pass over the mechanisms you find hardest, will give you more than another hour of blur."),
  ("ok. i also can't sleep before big exams, my mind races",
   "That racing is a very common pre-exam pattern. Writing down the three things you are most afraid of forgetting, so they are out of your head and on paper, often quiets it."),
  ("ok i'll try that",
   "Good. Keep the list by the bed, glance at it once in the morning and then let it go. The mechanisms will still be there, and a rested brain finds them easier."))
Q("exam", "emo", "thanks, i think i'm gonna stop for tonight",
  "Stopping after 6 hours is a wise call. Take a walk or a shower first, write your three mechanism worries on paper, and then sleep. Your brain will do more with the steps overnight than you can force into it now. A rested brain at 9am on Thursday will thank you for it.",
  ref=("6 hours", "mechanism"))
Q("exam", "rec_asst", "what was the thing about writing three things down",
  "To write down the three things you are most afraid of forgetting, so they sit on paper instead of racing around your head. Then glance at the list once in the morning. It sounds simple, but getting worries out of your head and onto paper is surprisingly calming. Which three would be on your list?",
  ref=("three things",))
Q("exam", "emo", "idk if i can do this",
  "You have studied 6 hours today, and that is not the work of someone who knows nothing. Being this tired makes everything feel shakier than it is, mechanisms included. What if you take one small break now and look at the first mechanism again afterward?",
  ref=("6 hours", "mechanisms"), cut=3)

# ------------------------------------------------------------------ lonely after a move
T("lonely",
  ("moved to denver 8 weeks ago for work and i haven't really made any friends",
   "Eight weeks is still early, but loneliness in a new city can feel heavy anyway. What does the week look like outside of work?"),
  ("i work from home so honestly i see no one. only my coworker maya on slack lol",
   "That is a very quiet setup for meeting people. Maya on Slack is at least a start, and a small, regular thing tends to work better than a big social leap."),
  ("there's a trivia night at a bar near me on thursdays",
   "A weekly trivia night is a good one: the same faces return, and the game gives you something to talk about without any pressure. Even going alone is easy, since teams often need an extra person."),
  ("it just feels like everyone else already has their people",
   "Everyone looks settled when you are on the outside, but most of them were new once too. Having no one around during the day says little about you and a lot about the setup."),
  ("i'm scared of showing up alone though",
   "That fear is real and very common, and it usually looks bigger beforehand than it feels in the room. You could arrive early, when the staff are friendly and the place is quiet, and ask where a solo person might join a team."),
  ("maybe this thursday",
   "This Thursday is a good target. If it feels awful after an hour, you are allowed to leave, and you will still have done the hard part."))
Q("lonely", "emo", "wish me luck",
  "Good luck on Thursday. Going alone to a trivia night in a city you are still learning takes courage, and the room is usually friendlier than it looks from outside. Walking in a little early will do most of the work. Even one conversation counts as a good start.",
  ref=("trivia night", "Thursday"))
Q("lonely", "rec_user", "which city was the move to",
  "Denver, eight weeks ago, for work. That is still very new, and most people take a few months to feel at home somewhere. You have had time to see what you miss and not enough to build a new routine. What has been the best part of it so far?",
  ref=("Denver",))
Q("lonely", "emo", "yeah i guess it's just hard being new",
  "It is hard, especially when you work from home and the days go by without anyone noticing you are there. Eight weeks of that would wear on most people, and it says nothing bad about you. The Thursday trivia night is exactly the kind of recurring thing that can change it.",
  ref=("work from home", "trivia night"), cut=4)
Q("lonely", "rec_asst", "what was the early arrival tip",
  "To get there early, when the staff are friendly and the place is quiet, and ask where a solo person could join a team. Walking in before the crowd also gives you time to settle in. Which part feels hardest, the walking in or the waiting?",
  ref=("early", "solo person"))

# ------------------------------------------------------------------ argument with a sister
T("sister",
  ("i had a fight with my sister dalia about mom's birthday dinner",
   "Family arguments around a birthday sting extra, because the day is supposed to be happy. What happened?"),
  ("she said i never help plan anything. i said she only shows up to take credit",
   "Those are both the kind of sentences that come out when people are tired and hurt. Underneath, it sounds like each of you feels unseen by the other."),
  ("yeah. i did book the restaurant last year though",
   "That counts, and it makes sense that her comment landed hard. It is possible she carries a lot of planning you do not see, the same way you carry parts she does not see."),
  ("maybe. she's been stressed with the new baby",
   "A new baby changes everything, including patience. That may be part of why a small disagreement grew so quickly."),
  ("i feel bad. should i text her",
   "A short, warm message tends to work better than a long explanation. Something that owns your part and reaches toward her, like saying you are sorry it got heated and that you want the dinner to be good for your mom."),
  ("ok i'll say sorry it got heated",
   "That is a good starting line. You do not have to settle who was right, only open the door."))
Q("sister", "emo", "she wrote back just ok thanks. is that bad",
  "A short reply from someone with a new baby is not necessarily cold, because she may be too tired for more. It is an opening, and the dinner for your mom gives you both a reason to talk again. Give it a day or two before trying again, and keep it light when you do.",
  ref=("new baby", "mom"))
Q("sister", "rec_user", "what was it she said that stung",
  "She said you never help plan anything. You mentioned booking the restaurant last year, which is fair to remember, though it may help to hear what she has been carrying too. Naming the hurt first, then the part you can own, tends to cool things down.",
  ref=("never help plan anything", "restaurant"))
Q("sister", "emo", "i don't even know why im so angry",
  "Anger often sits on top of hurt, and being told you never help plan anything, when you booked the restaurant last year, would hurt most people. A new baby in the family adds tension too, even when everyone loves each other. It makes sense that this has stayed with you.",
  ref=("never help plan anything", "new baby"), cut=4)

# ------------------------------------------------------------------ breakup
T("breakup",
  ("eli and i broke up last week",
   "That is a hard week to be in, even when the breakup is the right call. How are you doing, honestly?"),
  ("numb mostly. then i cry at random things, like the cereal he liked at the store",
   "Numbness mixed with sudden waves is a very normal way for grief to move. The cereal is one of those small objects that holds a whole relationship inside it."),
  ("my friend bea is staying with me this weekend which helps",
   "Bea sounds like exactly the right person to have nearby. Having someone in the room, even in silence, takes the edge off a weekend like this."),
  ("i kept his hoodie. should i give it back",
   "There is no rush, and you do not have to decide this week. If seeing it hurts, put it in a bag out of sight and decide later, when the feelings are quieter."),
  ("ok. i just keep thinking i should have done something different",
   "Almost everyone thinks that after a breakup, and it tends to be loudest in the first weeks. Looking for the thing you could have changed is the mind's way of feeling in control of something that hurt."),
  ("yeah",
   "You do not have to solve it tonight. Having Bea there this weekend is enough of a plan for now."))
Q("breakup", "emo", "bea left today and the apartment is so quiet",
  "The quiet after Bea leaves is hard, especially after a weekend of having someone close. Tonight you could put the hoodie in a bag out of sight, make something simple to eat and let yourself be tired. The first quiet evenings are usually the roughest, and they do get lighter. Is there a show or a song that feels like company tonight?",
  ref=("Bea", "hoodie"))
Q("breakup", "rec_asst", "what did you say to do with the hoodie",
  "To leave it for now: put it in a bag out of sight if seeing it hurts, and decide later when the feelings are quieter. Nothing about it has to be settled this week. A closet shelf or a box under the bed can be that out-of-sight place. Is there a spot for it already?",
  ref=("bag out of sight",))
Q("breakup", "emo", "is it weird that i'm not crying about the actual breakup",
  "Not at all. Grief rarely follows the obvious path, and the cereal at the store can hit harder than the breakup itself. The numb stretches and the sudden waves are often two sides of the same thing. There is no schedule for it, and the quiet moments often show up later.",
  ref=("numb", "cereal"), cut=3)

# ------------------------------------------------------------------ failed driving test
T("driving",
  ("failed my driving test today. second time",
   "That is disappointing, and it can feel even worse the second time around. Do you know yet what went wrong?"),
  ("parallel parking. i hit a cone",
   "Parallel parking is the part that undoes many people, and a cone is a very small thing next to a whole drive that went well. Was the rest of the test okay?"),
  ("yeah the rest was fine, examiner said i drove smoothly",
   "That tells you the hard parts are already there, since smooth driving is what examiners look for. The cone is one skill to polish, not a verdict on you."),
  ("my brother says i'm just bad at tests",
   "Being nervous in a test is different from being a bad driver, and nerves make small precise moves harder. A few sessions in an empty lot can make that move feel boring, which is exactly what you want."),
  ("the retake is in 3 weeks. my uncle raj has a quiet lot near his house",
   "That is a great setup. Three weeks of short sessions with Raj in the lot and a few cones from a store can turn parallel parking from scary to automatic."),
  ("ok good. i'll tell him i have a plan",
   "Telling him the plan is a good idea, since it lets him cheer you on instead of doubting. Keep the sessions short and regular, and stop each one on a good park so you leave feeling capable."))
Q("driving", "emo", "what if i hit the cone again",
  "It could happen, and it would not undo the three weeks of lot practice with Raj or the smooth drive the examiner noticed. It is less likely than it feels tonight, because doing the parallel park twenty times in a row with a few cones makes the move automatic.",
  ref=("Raj", "parallel park"))
Q("driving", "rec_user", "what was it that got hit again",
  "A cone, during the parallel parking. Everything else, including the smooth driving the examiner mentioned, went well. That one move is small next to everything the examiner liked.",
  ref=("cone", "parallel parking"))
Q("driving", "emo", "feels like everyone passes first time but me",
  "Plenty of people pass on the second or third try, even if nobody mentions it. You drove smoothly enough for the examiner to say so, and one cone during the parallel parking is a small, fixable thing. It makes sense that today still stings.",
  ref=("drove smoothly", "parallel parking"), cut=3)

# ------------------------------------------------------------------ burnout
T("burnout",
  ("i haven't had a day off in 12 days. i work at a restaurant called ferro's",
   "Twelve days without a break would wear anyone down. What does a normal day look like for you there?"),
  ("doubles most days. my hands ache and i snapped at a coworker today",
   "Aching hands and a short fuse are what exhaustion looks like, so the snap says more about the schedule than about you. Did you get a chance to talk to your coworker afterward?"),
  ("i apologized. she was nice about it. but i'm so tired",
   "Apologizing right away was a kind thing to do, and her answer says she understands. Being this tired needs more than an apology to others, it needs real rest."),
  ("my manager asked me to cover saturday too",
   "That would make 13 days, and it is fair to ask whether you can say no this time. You could say you are worn out and need Saturday to recover, and offer a different day next week."),
  ("i'm scared he'll say no and cut my hours",
   "That fear makes sense, since the work pays your rent. Still, a worn-out employee who gets a rest day usually works better than one who is pushed until a mistake happens."),
  ("i'll think about it",
   "That is fine, and you do not have to decide tonight. Let your hands rest first, then see how the idea feels in the morning."))
Q("burnout", "emo", "i think i will ask him for saturday off",
  "Asking plainly is a good way to do it: you are worn out after 12 days, and you can offer to cover another day next week. That way he hears an offer to cooperate rather than a refusal. Whatever he says, you will have spoken up for yourself, which counts. Have you thought about which other day you could offer?",
  ref=("12 days", "next week"))
Q("burnout", "rec_user", "how many days straight now, was it 12",
  "12 days, yes, and your manager wants Saturday too, which would make 13. That is a long run for anyone, and your hands are already showing it. Even one day of real rest would help them recover.",
  ref=("12 days", "Saturday"))
Q("burnout", "rec_asst", "how did you suggest asking about saturday",
  "By being plain about it: tell him you are worn out and need that day to recover, and offer to cover a different day next week. It puts the focus on keeping you able to work. Saying it once in the mirror before you speak makes the real conversation easier.",
  ref=("worn out", "different day"))
Q("burnout", "emo", "idk why im crying, its not even a big deal",
  "Crying after 12 days without a rest is not a small reaction to a small thing, it is your body asking for a break. You were kind enough to apologize to your coworker, and you are still carrying a lot. It makes sense that it spills out.",
  ref=("12 days", "apologize"), cut=3)

# ------------------------------------------------------------------ cat at the vet
T("pepper",
  ("my cat pepper is at the vet overnight. her kidneys",
   "That is frightening, and an overnight stay shows she is getting careful attention. How are you feeling right now?"),
  ("terrified. she's 14. i keep refreshing my phone",
   "Fourteen years is a long companionship, so of course the waiting is hard. Refreshing the phone is something many people do when there is nothing else they can do."),
  ("the vet said they'd call by 9 tomorrow morning",
   "Then the call is about the morning, and tonight is about getting through the hours. Having her watched overnight is the careful kind of care she needs."),
  ("i can't sleep",
   "That is understandable. It may help to put the phone in another room with the volume up, so you will hear the call but will not be tempted to check it every minute."),
  ("she sleeps on my pillow. the bed feels empty",
   "A pillow that still carries her shape and smell must make the quiet very loud tonight. You could keep her blanket nearby, and picture her warm and watched over in her own little corner at the vet."))
Q("pepper", "emo", "what if the call is bad news",
  "Then you will face it with the vet's guidance, and you will not have to decide anything alone. For tonight, the call is still hours away, and Pepper is being watched through the night. It is okay to be afraid of the call and still hope for good news.",
  ref=("Pepper", "night"))
Q("pepper", "rec_user", "what time did they say they'd call",
  "By 9 tomorrow morning. Until then the phone can sit in the other room with the volume up, so you will hear it without checking every minute. Waiting is the hardest part, and the night will pass faster if you have something small to do with your hands.",
  ref=("9 tomorrow morning",))
Q("pepper", "rec_asst", "what did you say about the phone",
  "To put it in another room with the volume up, so you will hear the call without being tempted to check it every minute. Keeping it just out of reach also stops the screen from keeping you awake. A quiet room and the blanket can do the rest.",
  ref=("another room", "volume up"))
Q("pepper", "emo", "i just wanna hold her",
  "Wanting to hold her after 14 years of having her close is completely natural. Pepper is being watched through the night, and by morning there will be news. For now, something she would recognize, like her blanket, can sit beside you.",
  ref=("Pepper", "14"), cut=3)

# ------------------------------------------------------------------ laid off
T("laidoff",
  ("i got laid off on monday. 4 years as a designer and they emailed me at 8am",
   "That is a rough way to hear it, and four years is a real chunk of your life to lose in one email. How have you been since Monday?"),
  ("angry mostly. then embarrassed, which makes no sense",
   "Anger and embarrassment both make sense: the first because it was handled badly, the second because the mind blames itself when a job goes. A layoff says something about the company's budget, not about your work."),
  ("i haven't told my parents yet",
   "That is a lot to hold alone. Telling them does not have to mean a long conversation, just a sentence about what happened and a plan for how you are handling it."),
  ("my dad will freak out about money",
   "He may worry, and that is often how fear shows up in parents. Having a few facts ready, like savings, severance and next steps, usually calms the conversation."),
  ("i do have 3 months of savings and 6 weeks of severance",
   "That is real breathing room, and it changes how the conversation can go. You can give him the numbers and a calm plan for the next few weeks."),
  ("yeah. i'll call him tomorrow",
   "A call tomorrow gives you tonight to settle your thoughts. Lead with the plan before the worry, and he may take it better than you expect."),
  ("ok. any other advice before i call",
   "Choose a time when you are not rushed, and have your numbers written down in front of you. If the call gets heavy, it is fine to pause and say you will talk more tomorrow."))
Q("laidoff", "emo", "what's the first sentence going to be",
  "Start with the fact, then the cushion, then the plan: you were laid off on Monday, you have 3 months of savings plus 6 weeks of severance, and you will work out the next steps from there. Say it calmly and he will likely follow your lead. Then let him respond before you answer. Do you want to try saying it out loud once?",
  ref=("3 months", "6 weeks"), cut=6)
Q("laidoff", "rec_user", "what were the savings numbers",
  "3 months of savings plus 6 weeks of severance. That is real breathing room, and it is worth putting those numbers in front of your dad when you call. Being specific will make the conversation concrete and keep your own worry smaller.",
  ref=("3 months", "6 weeks"))
Q("laidoff", "rec_asst", "what did you say to lead with on the call to dad",
  "To lead with the plan before the worry. Open with what you do have, the savings and the severance, and he may take the news more calmly than you expect. Saying it plainly out loud once tonight will make tomorrow's call feel familiar.",
  ref=("plan before the worry",))
Q("laidoff", "emo", "he's gonna be so disappointed",
  "Parents often worry more than they judge, and a dad who freaks out about money mostly wants you to be safe. You can show him you are steady by calling with the facts rather than the fear. Four years of design work is not something to be disappointed in.",
  ref=("dad", "money"), cut=4)

# ------------------------------------------------------------------ moving day
T("moving",
  ("moving apartments on the 28th and i haven't packed a thing",
   "Packing is easier when you start with the rooms you use least. Do you have boxes yet, and is anyone helping on moving day?"),
  ("no boxes. my friend luis has a truck though. third floor, no elevator",
   "Luis with a truck is a big win, and a third-floor walk-up means every box counts twice. Ask supermarkets and liquor stores for sturdy boxes, and keep each one small enough to carry up the stairs."),
  ("ok what do i pack first",
   "Start with things you will not miss: books, off-season clothes, decorations and spare linens. Label each box by room and write a few contents on the side, so unpacking is less of a puzzle."),
  ("any trick for the first night in the new place",
   "Pack a first-night box with sheets, a towel, toiletries, a phone charger, a kettle and a mug. It lets you sleep and have a hot drink without opening twenty boxes."),
  ("ok. starting with the books tonight",
   "Books are a good place to start, and a small box keeps them manageable. Pack the heaviest ones at the bottom and fill the gaps with towels so nothing shifts."))
Q("moving", "rec_asst", "what was in that first night box",
  "Sheets, a towel, toiletries, a phone charger, a kettle and a mug. That covers sleeping, a shower and a hot drink before you open another box. Put it by the front door so it is the first thing you carry in. Is there a favorite mug you want to go in it?",
  ref=("first-night box".replace("first-night box", "kettle and a mug"),))
Q("moving", "fact", "how many boxes does a one bedroom usually take",
  "A one-bedroom usually takes somewhere between 40 and 60 boxes, depending on how many books and kitchen things there are. It helps to count by room as you pack. A few extra small boxes are worth having for the books, since books get heavy fast.",
  ok=("40", "60"), ref=("books",))
Q("moving", "build", "found 15 boxes at the grocery store!",
  "Fifteen sturdy ones is a solid start, and small grocery boxes are well suited to heavy things like books. Keeping each one light will make the walk-up easier. Luis can load the truck with the heavy ones first. A roll of tape and a marker will make the packing go faster.",
  ref=("Luis", "walk-up"))

# ------------------------------------------------------------------ learning spanish
T("spanish",
  ("been learning spanish for 3 months, duolingo streak is 41 days",
   "A 41-day streak is real consistency, and three months is enough to feel the language starting to click. What are you learning it for?"),
  ("my girlfriend's family is from mexico city, i want to talk to her abuela",
   "That is a lovely reason, and it will keep you going when the grammar gets stubborn. Abuela will probably be thrilled by even a few clumsy sentences."),
  ("i understand a lot when i read but freeze when i speak",
   "Reading comes first for almost everyone. The way through is speaking out loud, even to yourself, a few minutes a day, so your mouth catches up with your head."),
  ("like talking to myself in the shower lol",
   "Honestly, the shower is a great practice room. Narrate what you are doing in short sentences, and your mouth gets used to the sounds without anyone listening."),
  ("ok. and shows? i heard watching helps",
   "Watching helps a lot, especially with Spanish subtitles instead of English. A familiar show you already know in English keeps the story easy to follow."))
Q("spanish", "fact", "how long does it take to get conversational in a language",
  "It varies a lot, but for most English speakers learning Spanish, a basic conversation is realistic within 6 to 12 months of steady practice, and ease takes longer. Speaking out loud speeds it up more than anything. At 3 months in, you are already ahead of most people.",
  ok=("6", "12"), ref=("3 months",))
Q("spanish", "build", "had a 2 minute chat with abuela on a video call!!",
  "That is wonderful news. Two minutes of real conversation is worth weeks of exercises, and abuela will remember it. The shower practice is clearly paying off, so keep going. How did it feel to hear her answer?",
  ref=("shower", "abuela"))

# ------------------------------------------------------------------ pour-over coffee
T("coffee",
  ("just got a v60 and my coffee tastes sour",
   "Sour coffee usually means under-extraction, which is easy to fix. What grind setting and water temperature are you using?"),
  ("grinder is on 20 and the water's just off the kettle",
   "Water just off the boil is good. Try grinding finer, a few clicks at a time, because a finer grind slows the water and pulls out more sweetness."),
  ("how much coffee and water",
   "A good starting ratio is 1 part coffee to 16 parts water. For one big mug, that is around 20 grams of coffee and 320 grams of water."),
  ("how long should it take",
   "Aim for about three minutes in total, including the bloom. If it drains much faster, grind finer again."),
  ("what's the bloom",
   "Pour just enough water to wet all the grounds, about twice their weight, and wait 30 to 45 seconds. It lets trapped gas escape, so the rest of the water flows evenly."))
Q("coffee", "fact", "does the water temperature matter that much",
  "A little, yes. Water around 195 to 205 degrees Fahrenheit extracts well, and a kettle just off the boil lands close to that. Cooler water tends to leave coffee sour, so your kettle habit is a good one. Letting the kettle sit a few seconds after boiling brings it into that range.",
  ok=("195", "205", "Fahrenheit"), ref=("kettle",))
Q("coffee", "build", "went 4 clicks finer and it's so much sweeter!",
  "Grind size makes a huge difference, and you found it quickly. The sourness was the coffee not getting enough time with the water, and a finer grind slows the water down. If it starts tasting bitter later, go back a click or two.",
  ref=("sour", "grind"))

# ------------------------------------------------------------------ bouldering
T("climbing",
  ("started bouldering this week, rented shoes, absolutely hooked",
   "The first week of climbing is a special feeling, because every problem feels like a puzzle. Where do you climb?"),
  ("a gym called summit wall. my forearms are dead",
   "Forearms are the first thing to give out, because gripping is new for them. Rest days and a gentle warm-up will help them adapt."),
  ("what's a v0",
   "V0 is the easiest grade in the V-scale used for bouldering. Most beginners spend their first weeks on V0 and V1 and move up as technique improves."),
  ("should i buy shoes",
   "Rent for a month or two, then buy once you know you'll stay. A snug but not painful fit is what you want, and the gym staff can help you try some."),
  ("ok. i also keep falling off at the top. why",
   "Often it is because the last move is the one where you tire or rush. Slow down on the last two moves, and make sure your feet are placed before you reach."))
Q("climbing", "rec_user", "what's the gym called again",
  "Summit Wall. The staff there can help you try shoes when you are ready, and your forearms will adapt the more you go.",
  ref=("Summit Wall",))
Q("climbing", "rec_asst", "when was the shoe buying supposed to happen, you said",
  "After a month or two of renting, once you know you will stay. Look for a snug but not painful fit, and ask the staff at the gym to help you try some. Trying on a few pairs in person matters more than the brand. A tight fit that does not hurt at first will stretch a little with use.",
  ref=("month or two",))
Q("climbing", "fact", "how many days a week should a beginner climb",
  "Two or three days a week is plenty to start, with a rest day between sessions. Fingers and forearms need time to adapt, and rest is when they get stronger. More days can come after a few months. Listening to your forearms is the best guide for adding a day.",
  ref=("forearms",))
Q("climbing", "build", "sent my first v1 today",
  "Moving up from V0 so quickly is a real mark of progress, and a good one to celebrate. Your forearms did the work, so give them their rest days and let the next grade wait a few sessions. Slowing down on the last two moves, the way we talked about, is what tends to turn a near miss into a send.",
  ref=("V0", "forearms"))

# ------------------------------------------------------------------ chain rule
T("calculus",
  ("calc exam friday and i don't get the chain rule",
   "The chain rule feels slippery at first, but it is just a way to differentiate things inside other things. What example is confusing you?"),
  ("like d/dx of sin(x^2)",
   "Think of it as an onion: the outer layer is sine, the inner layer is x squared. Take the derivative of the outer layer, keep the inside as it is, then multiply by the derivative of the inside."),
  ("so cos(x^2) times 2x",
   "Exactly, and you got there on your own. Outer derivative cos of the inside, times the derivative of the inside, 2x."),
  ("ok what about e^(3x)",
   "The outer layer is e to the something, and its derivative is itself. Keep the 3x inside and multiply by the derivative of the inside, which is 3, to get 3e^(3x)."),
  ("i think i get it now",
   "Good to hear. Try a couple of mixed ones tonight, like ln of x cubed plus one, to check that the layers feel natural."))
Q("calculus", "rec_asst", "what was the onion thing",
  "Picture the function as an onion with layers: sine is the outer one and x squared is the inner one. Differentiate the outer layer, leave the inside alone, then multiply by the derivative of the inside. That order is the whole trick, and it works for any nested function.",
  ref=("onion", "outer"))
Q("calculus", "fact", "what's the product rule",
  "It is the rule for the derivative of two functions multiplied together: the derivative of the first times the second, plus the first times the derivative of the second. It often turns up alongside the chain rule, so it is worth practicing both before Friday. Is the exam covering both rules?",
  ref=("chain rule", "Friday"))
Q("calculus", "build", "tried ln(x^3 + 1) like you said and got 3x^2 over x^3 + 1",
  "That is exactly right. The outer layer is ln, whose derivative is one over the inside, and then you multiplied by the derivative of the inside, 3x squared. You clearly have the onion method working. Do you want to try a product rule problem next?",
  ref=("onion",))

# ------------------------------------------------------------------ tomatoes
T("tomatoes",
  ("planted tomatoes in may, cherokee purples, and they're finally flowering",
   "Flowers mean fruit is coming, and Cherokee Purples are worth the wait. How are they supported, and how often do you water?"),
  ("just a stake each. i water every other day",
   "A single stake works early on, but heirloom tomatoes get heavy, so plan to tie the main stem as it grows. Watering every other day is fine if you soak deeply, and mulch will help the soil hold moisture."),
  ("some leaves at the bottom have yellow spots",
   "Spots on the lowest leaves are often early blight or simply old age. Remove them, avoid wetting the foliage, and water at the base early in the day."),
  ("ok. when will they be ready",
   "Cherokee Purples usually ripen about 80 days after transplanting, when they turn a deep dusky rose and the green near the stem fades. They taste best picked slightly soft and eaten the same day."),
  ("a neighbor says i should prune the suckers",
   "She is right that pruning suckers, the little shoots between the stem and the branches, helps airflow and keeps the plant focused on fruit. Pinch them off while they are small."))
Q("tomatoes", "rec_asst", "how long till ripe, you said a number",
  "About 80 days after transplanting, and they are ready when they turn a deep dusky rose. Picking them a touch soft and eating them the same day gives the best taste. Color is the best sign, more than the date on the calendar.",
  ref=("80 days",))
Q("tomatoes", "fact", "what are suckers exactly",
  "Suckers are the small shoots that grow in the joint between the main stem and a branch. If left alone, each one can grow into a full stem that competes for energy. Pinching them off while they are small is easy with your fingers. Do you see any on the plants yet?",
  ref=("suckers",))
Q("tomatoes", "build", "the neighbor gave me a taller stake. should we swap it in",
  "A taller stake is a good idea for Cherokee Purples, since they grow tall and heavy. Push it in a few inches from the main stem, taking care of the roots, and tie the stem loosely as it grows. The original stake can stay for a side branch.",
  ref=("stake", "Cherokee"))

# ------------------------------------------------------------------ extra questions on earlier threads
Q("niece", "fact", "how long does plaster take to dry for the eggs",
  "Plaster usually sets in about 20 to 30 minutes, but it needs a day or two to dry fully before kids dig it up. Make the plaster eggs a couple of days before Saturday, and let them sit somewhere dry. They break open more cleanly when they have hardened.",
  ok=("20", "30"), ref=("plaster eggs", "Saturday"))
Q("tenk", "build", "teo is sick so i'm running alone on tuesday, same plan?",
  "Same plan, just on your own: run 2 minutes, walk 1, for about 20 minutes. Keep it easy, since Tuesday is the chatty run, and save the effort for Saturday. Your left knee will like a flat route. How is the knee feeling these days?",
  ref=("Tuesday", "left knee"))
Q("camera", "rec_none", "which lens were we using?",
  "We have not mentioned the lens. We have talked about the Fujifilm X-T30, landscapes by the lake, f/8 and golden hour. If you tell me what came with it, I can say what it is good for. Landscapes by the lake do not need anything special to begin with.",
  absent="lens", ref=("Fujifilm X-T30", "f/8"))
Q("guitar", "build", "ok which one first, the knockin one or the other",
  "Start with Knockin' on Heaven's Door, since it is the one your dad loves, and its slow pace gives your fingers time to change. Horse with No Name is the easy backup when you want a quick win. Either way, a few minutes a day will get you moving.",
  ref=("Knockin' on Heaven's Door", "Horse with No Name"))
Q("portugal", "fact", "is porto walkable or do we need a car",
  "Porto is very walkable in the center, though the streets are steep, so comfortable shoes matter. Trams, the metro and taxis cover the rest, and a car is more trouble than help in the old town. A car is only useful for trips out to the countryside.",
  ref=("Porto",))
Q("menu", "rec_none", "did we pick a dessert wine?",
  "We haven't picked a dessert wine yet. The lemon tart is the dessert, and a small glass of something sparkling would suit it if you want a pairing. Sparkling lemonade would suit anyone skipping alcohol. Is anyone at the table not drinking?",
  absent="dessert wine", ref=("lemon tart",))

# ------------------------------------------------------------------ surprise dinner
T("surprise",
  ("want to plan a surprise birthday dinner for my partner maren",
   "A surprise dinner is a lovely idea, and the details are what make it feel personal. What kind of food does Maren love, and how many people are you inviting?"),
  ("just the two of us plus maren's best friend priya. maren loves thai food",
   "A table for three at a good Thai place keeps the evening relaxed. Booking a corner table and telling the staff it is a birthday is a nice touch, and a small dessert with a candle is often easy to arrange."),
  ("booked a place called basil & bloom for friday at 7:30",
   "A good pick, and 7:30 gives everyone time to arrive without rushing. Asking Priya to bring Maren there on a believable excuse is the usual way to keep the surprise intact."),
  ("priya says she'll tell maren we're just grabbing a quick bite",
   "A quick bite is a perfect cover story, and it sets up the surprise nicely. Maybe have Priya text you when they are five minutes away, so you can be seated and ready."))
Q("surprise", "rec_user", "i forgot what time the table is",
  "7:30 on Friday at Basil & Bloom, for the three of you. That gives Priya time to bring Maren over without rushing, and the quick-bite story still holds. Arriving a few minutes early will give you time to explain the plan to the staff. Did you ask the restaurant for the corner table?",
  ref=("7:30", "Basil & Bloom"))
Q("surprise", "build", "should the cake come from there or a bakery",
  "Most restaurants can bring out a small dessert with a candle, so a separate cake is only worth it if Maren has a favorite bakery. Ask Basil & Bloom what they can do when you confirm the booking. For a table of three, keeping it simple suits the evening.",
  ref=("Basil & Bloom", "Maren"))
Q("surprise", "fact", "how far ahead do you need to book a friday table",
  "For a Friday evening, a few days ahead is usually safe, and a week is better for popular places. Yours is already booked, so a quick call the day before to confirm is a good habit. Mention Priya's name too, in case she arrives first.",
  ref=("Priya",))

# ------------------------------------------------------------------ learning chess
T("chess",
  ("i've started learning chess and keep losing in like ten moves",
   "Early losses are part of the learning curve, and they usually come from a handful of habits that are easy to fix. Do you know any openings yet, or are you improvising?"),
  ("just improvising. my friend told me to learn the italian game",
   "The Italian Game is a good first opening: you develop your knight and bishop quickly, aim at the weak f7 square and castle early. It teaches the basic principles at the same time."),
  ("ok what are the first moves",
   "As White: e4, then knight to f3, then bishop to c4. Black usually answers with e5 and knight to c6, and you will reach the main line of the opening."),
  ("cool. what does castling do",
   "Castling tucks your king behind a wall of pawns and brings a rook toward the center in a single move. Doing it early keeps the king safe while your pieces come out."),
  ("my friend's name is kofi btw, he beats me every time",
   "Playing a stronger friend like Kofi is one of the quickest ways to improve, even if the scores sting. Ask him to tell you one thing you could have done differently after each game."))
Q("chess", "rec_asst", "what opening did you tell me to learn",
  "The Italian Game, which starts with e4, knight to f3 and bishop to c4. It gets your pieces out early and puts the king on a safe path to castling, so it teaches good habits while you learn. Have you tried it against Kofi yet?",
  ref=("Italian Game",))
Q("chess", "fact", "what's a gambit",
  "A gambit is an opening where you give up a pawn on purpose to get faster development or a stronger attack. The best known is the King's Gambit, and a sharp relative of the Italian Game is called the Evans Gambit. Beginners usually do better learning solid openings first. Do you want to try one against Kofi?",
  ok=("King", "Evans", "Gambit"), ref=("Italian Game",))
Q("chess", "build", "castled early and lost a pawn but the king felt safe, is that ok",
  "A safe king is worth more than a single pawn at your level, so castling early was a sound call. What is worth checking is whether the pawn was lost to a trick or simply left loose, say with a bishop or knight undefended. Going through it with Kofi afterward is one of the quickest ways to improve.",
  ref=("castling", "bishop"))

# ------------------------------------------------------------------ bike commute
T("bike",
  ("started cycling to work, 9 km each way",
   "Nine kilometers each way is a solid commute, and riding it regularly builds fitness quickly. How are you finding it so far?"),
  ("good but the route has one nasty hill and i show up sweaty",
   "A sweaty arrival is the classic commuter problem. Ride the hill in an easier gear, slow down for the last few minutes, and carry a spare shirt or wipes if the office allows it."),
  ("it rained on tuesday and i got soaked",
   "Rain is part of cycling, and the right gear makes it easy to live with. A light waterproof jacket, mudguards and waterproof pants for the worst days will keep you far drier."),
  ("ok i'll get mudguards. also the chain squeaks",
   "A squeaky chain is asking for lubricant. Wipe it with a rag, add a drop of bike chain oil to each link, then wipe off the excess so it does not attract dirt."))
Q("bike", "rec_user", "what's the distance of the commute, i forget",
  "9 km each way, with one nasty hill on the route. That is a solid commute, and easing off for the last few minutes should help you arrive less sweaty.",
  ref=("9 km", "hill"))
Q("bike", "fact", "how often should the chain get oiled",
  "The chain wants oil roughly every 200 to 300 kilometers, or whenever it starts to squeak or after riding in the rain. Wipe off the old dirt first, then add a little oil. With a commute like yours, that works out to about every couple of weeks.",
  ok=("200", "300"), ref=("chain", "commute"))
Q("bike", "build", "got the mudguards and the first rainy ride was so much drier",
  "Mudguards make a huge difference, and your back and shoes stay clean as well. Pair them with the waterproof jacket for the worst days and the rain stops being a reason to skip the ride. Is the squeak in the chain gone too?",
  ref=("waterproof jacket", "chain"))

# ------------------------------------------------------------------ banana bread
T("bananabread",
  ("baking banana bread for a coworker's farewell on friday",
   "Banana bread is a warm way to say goodbye. Do you have a recipe, and are there any allergies at the office to think about?"),
  ("recipe is fine. nuts are a no, dev is allergic",
   "Then leave the nuts out and mention that on a little card, so everyone knows it is safe for Dev. Chocolate chips or a sprinkle of cinnamon sugar on top add texture without nuts."),
  ("ooh chocolate chips. how ripe should the bananas be",
   "Very ripe, with plenty of brown spots or even mostly black skin. The riper they are, the sweeter and moister the bread, and you will need about three medium bananas for a standard loaf."),
  ("mine are still yellow",
   "Then they need a few days. To speed things up, bake them whole on a tray at a low heat until the skins turn black, and let them cool before mashing."))
Q("bananabread", "rec_user", "who can't have nuts again",
  "Dev. Leaving the nuts out and putting a little card by the bread makes it safe and obvious for everyone. The chocolate chips will keep it from feeling plain.",
  ref=("Dev", "nuts"))
Q("bananabread", "fact", "can banana bread be frozen",
  "Yes, it freezes very well. Wrap it tightly once it has cooled, and it keeps for about three months. For Friday, you could bake it a few days ahead, freeze it, and thaw it on the counter the night before. Do you have a freezer bag big enough?",
  ref=("Friday",))
Q("bananabread", "build", "baked the bananas in the oven like you said, skins are black now",
  "Perfect, they are ready to mash. Let them cool first, then stir in the chocolate chips and leave out the nuts, since Dev is allergic. The bread will taste sweeter and moister than it would have with yellow ones. Do you have the chocolate chips ready?",
  ref=("Dev", "chocolate chips"))

# ------------------------------------------------------------------ lighthouse story
T("lighthouse",
  ("trying to write a short story about a lighthouse keeper named odile",
   "A lighthouse keeper is a wonderful setup, because the isolation and the sea do a lot of work for you. What is the problem that disturbs her routine?"),
  ("a ship that shouldn't be there appears one night with no lights",
   "That is a strong hook: a ghostly arrival gives Odile something to investigate and a reason to leave her tower. Keep the first description of the ship short and strange, and let readers fill in the rest."),
  ("i don't know how it ends",
   "Try asking what the ship wants from her. Endings often come from the answer: it could be a debt, a warning or an invitation, and each leads somewhere different."),
  ("maybe it's an invitation to leave the lighthouse",
   "That fits her character well, since a keeper who never leaves has something real to choose. A quiet ending where she decides to step aboard or to stay would both feel earned."))
Q("lighthouse", "rec_asst", "what question did you tell me to ask about the ship",
  "To ask what the ship wants from her. Whether the answer is a debt, a warning or an invitation, it can steer the whole ending, and you chose the invitation. Does that ending still feel right to you?",
  ref=("what the ship wants",))
Q("lighthouse", "fact", "what's a good length for a short story",
  "Most short stories fall between 1,000 and 7,500 words, and a tight one of 2,000 to 4,000 suits a single event like Odile's. Staying near the lower end keeps the focus on the ship and her choice. How long do you picture yours running?",
  ok=("1", "000", "7", "500", "2", "4"), ref=("Odile", "ship"))
Q("lighthouse", "build", "i think she steps aboard but we never see where it goes",
  "An open ending like that leaves readers wondering about the ship long after the last line. Odile choosing to leave the lighthouse is the payoff for all her years of staying. Closing on the sound of the lamp still turning behind her would be a lovely final image.",
  ref=("Odile", "lighthouse"))

# ------------------------------------------------------------------ adult piano
T("piano",
  ("adult beginner on piano, just got a keyboard last month",
   "A keyboard is a perfect start, and learning as an adult means you can choose music you actually love. What would you like to be able to play?"),
  ("moonlight sonata first movement. i know, ambitious",
   "Ambitious but doable in a simplified form. The opening is slow and repetitive, which makes it friendlier than it sounds, and many easy arrangements keep the mood while simplifying the hands."),
  ("is it ok if i don't read music well yet",
   "Yes, you can learn notes gradually while you play. Start with the right hand melody, label the keys lightly, and build up your reading with simple pieces alongside it."),
  ("ok. my teacher is a friend called mona who comes on sundays",
   "Having Mona guide you on Sundays gives your practice a weekly goal. Ask her which easy arrangement fits your hands, and what to practice between lessons."))
Q("piano", "rec_asst", "what did you say about the start of the sonata",
  "That the opening is slow and repetitive, which makes it friendlier than it sounds. A simplified arrangement keeps the mood and eases the work for your hands. Playing it slowly at first lets the pattern settle into your fingers. Have you tried the first few bars yet?",
  ref=("slow and repetitive",))
Q("piano", "fact", "how long does an easy version of a piece like that take to learn",
  "Often a few weeks to a couple of months of regular practice, depending on how much time you give it each day. Splitting it into short sections and practicing each hand separately speeds things up. A keyboard helps, since you can play quietly at any hour.",
  ref=("keyboard",))
Q("piano", "build", "mona says i should play the left hand alone first",
  "That is sound advice, because the left hand is the slow bass line of Moonlight Sonata, so it is a gentle way to learn the notes and keep steady time. Slow, even notes matter more than speed. Once it feels easy, adding the right hand will be much less of a juggle. How does the left hand feel so far?",
  ref=("Moonlight Sonata",))

# ------------------------------------------------------------------ study abroad
T("seoul",
  ("i got into a study abroad program in seoul for spring semester",
   "That is wonderful news, and spring is a lovely time to be in Seoul. What are you most looking forward to, and what are you nervous about?"),
  ("excited about the food. nervous about not knowing korean",
   "Plenty of exchange students arrive knowing very little, and a few weeks of basic phrases will carry you through daily life. Learning to read the alphabet, hangul, is quick and makes signs and menus much friendlier."),
  ("how long does hangul take",
   "Many people can read it in a weekend and feel comfortable in a couple of weeks. It was designed to be learned quickly, with letters that follow the shapes of the mouth."),
  ("cool. my roommate there is called minseo and we've already texted",
   "A friendly roommate makes arriving much easier. You could ask Minseo to show you a favorite local spot during your first week."))
Q("seoul", "rec_user", "wait which city is the program in",
  "Seoul, for the spring semester. You said the food is what you are most excited about, and your roommate Minseo is already texting you. Food is a great way into a new city, so the first week can be eating-led. Have you thought about what you will try first?",
  ref=("Seoul", "Minseo"))
Q("seoul", "fact", "is it expensive to eat out in seoul",
  "Everyday meals in Seoul can be quite affordable, especially at small local restaurants, where a bowl of stew or noodles is often inexpensive. Restaurants in busy tourist areas cost more. Cooking at home with market ingredients saves the most.",
  ref=("Seoul",))
Q("seoul", "build", "minseo says there's a night market near campus",
  "A night market near campus is a perfect first outing for someone excited about the food. You could ask Minseo which stalls she likes best, and try one new dish each time you go. It is a friendly way to practice reading hangul on the menus too.",
  ref=("Minseo", "food"))

# ------------------------------------------------------------------ hawk
T("hawk",
  ("saw a big hawk on a fence post this morning, couldn't stop staring",
   "Hawks perched in plain sight are a gift, because they often sit still long enough to watch. Did you notice any markings, like a red tail or a streaked belly?"),
  ("red tail i think. and a pale chest with dark streaks",
   "A red-tailed hawk is a good match, and it is the most common large hawk in much of North America. The pale chest with a band of dark streaks across the belly is a typical sign."),
  ("how fast do hawks fly",
   "A red-tailed hawk usually soars at around 20 to 40 miles per hour, and dives faster when hunting. The fastest bird of all, the peregrine falcon, can exceed 200 miles per hour in a dive."),
  ("wow. i want to start birdwatching properly",
   "A pair of binoculars and a free bird identification app are all you need. Keep a small notebook of what you see, because writing it down makes you notice more."))
Q("hawk", "rec_asst", "how fast was the falcon one",
  "The peregrine falcon can exceed 200 miles per hour in a dive. The red-tailed hawk you saw is slower, usually soaring at 20 to 40 miles per hour and diving faster when it hunts. Do you want to watch for the hawk again tomorrow?",
  ref=("peregrine falcon", "200 miles per hour"))
Q("hawk", "fact", "do hawks come back to the same perch",
  "Often yes. Hawks tend to use favorite perches with a good view of hunting ground, so the same fence post may be worth checking at the same time tomorrow. Early morning is usually the best time to look. Do you pass that fence post every morning?",
  ref=("fence post",))
Q("hawk", "build", "bought binoculars, 8x42, what can i expect to see",
  "Eight times magnification with big lenses is a classic choice for birdwatching, bright and steady enough for a hawk on a fence post. You will see the streaks on its chest and the red tail in sharp detail. Keep your notebook ready for the first one you spot.",
  ref=("fence post", "notebook"))

# ------------------------------------------------------------------ desk back pain
T("desk",
  ("my lower back hurts after long days at the desk",
   "Long hours seated can strain the lower back, and a few small changes usually help a lot. How long do you sit at a stretch, and what kind of chair do you use?"),
  ("like 3 hours straight. just a kitchen chair",
   "Three hours without a break is a long time for any back. Standing up every 30 to 45 minutes and a cushion for the lower curve of your spine will make a kitchen chair far more comfortable."),
  ("i could try standing while i work",
   "Standing for part of the day helps if you alternate: stand for a while, sit for a while, and avoid either extreme. A stack of books under your laptop on a counter works as a cheap trial."),
  ("good idea. what stretches",
   "Cat-cow on hands and knees, a gentle child's pose, and a seated twist all ease the lower back. Hold each for about 30 seconds and breathe slowly, without forcing it."))
Q("desk", "rec_user", "how long was the sitting stretch",
  "3 hours straight, in a kitchen chair, which is a lot for any back. Standing up every 30 to 45 minutes would break that into pieces your back can handle. Short breaks to stand and stretch also keep you sharper than pushing through.",
  ref=("3 hours", "kitchen chair"))
Q("desk", "fact", "how much does a decent office chair cost",
  "A supportive office chair usually runs from about 150 dollars at the low end to several hundred for a well-known ergonomic model. Secondhand ones from office clearances are often a bargain. A cushion for the lower back is a cheaper first step than replacing the kitchen chair.",
  ok=("150",), ref=("kitchen chair",))
Q("desk", "build", "tried the book stack standing thing for an hour, feet hurt",
  "Sore feet are common at first, since they are not used to it. Alternate between standing and sitting, wear shoes with some cushion, and stand on a folded towel if the floor is hard. The book stack did its job as a cheap trial, and the 3 hours of sitting can shrink gradually.",
  ref=("book", "3 hours"))

# ------------------------------------------------------------------ board game night
T("games",
  ("hosting board game night for 6 people on saturday",
   "Six is a fun number for games, and Saturday gives you time to plan. Do you want something quick and silly, or a longer strategy game?"),
  ("mix. catan and wingspan are on the shelf but some haven't played",
   "For a mixed group, start with something that takes five minutes to explain, then move to Catan or Wingspan once everyone is warmed up. Wingspan is gentler, and Catan is more competitive."),
  ("catan is for up to 4 though",
   "Right, the base game of Catan seats three or four, so with six people you can split into two tables or choose a game that fits the whole group. Games like Codenames or Just One handle six easily."),
  ("love codenames. maybe catan for 4 and a quick two-player game for the rest, then swap",
   "That works well. Two groups can swap when a game finishes, so nobody sits out for long. A snack table between the two keeps the mood relaxed."))
Q("games", "rec_user", "how many people are coming",
  "6 people on Saturday. Catan seats 4, so the other two will want a table of their own, or a quick two-player game, until the first round ends. Swapping players then lets everyone try both.",
  ref=("6 people", "Saturday"))
Q("games", "fact", "how long does a game of catan take",
  "A game of Catan usually takes 60 to 90 minutes with four players, and longer if everyone is new to it. Explaining the rules adds another 10 to 15 minutes. If the evening is short, set a time limit or play to fewer points. How long is your evening?",
  ok=("60", "90", "10", "15"), ref=("Catan",))
Q("games", "build", "everyone loved codenames, what's next",
  "A good sign. Just One is the natural follow-up, because it has the same quick, social rhythm and works for six. Wingspan is the quieter option for a later night, when you want something gentle. Which night are you planning it for?",
  ref=("Just One", "Wingspan"))

# ------------------------------------------------------------------ longer sessions
# A few threads run on for three more exchanges, so some scenarios recall a detail six to eight exchanges back
# (production keeps roughly 12k characters of history on an Ask step, well above these).
def EXTEND(name, *ex):
    THREADS[name].extend((u, a) for u, a in ex)


EXTEND("niece",
       ("she also wants to invite her cousin leo, is that ok with 12 kids",
        "More kids just means another small dig station, which is easy to add. Leo and Ivy could run the first dig together while the others watch, which gives the shy ones a moment to warm up."),
       ("ha ok. i need to send invites soon",
        "A simple card with a dinosaur footprint on it and the details on the back is plenty. Put the Saturday date, the time and Cedar Park on it, and ask for a reply a few days before."),
       ("love it, will do tonight",
        "Tonight is a good moment for it. Keep the wording short and warm, and leave a little room on the back for a contact number."))
EXTEND("portugal",
       ("can we do a food tour in porto",
        "A guided food tour is a good way to learn a city's flavors in a few hours. Look for a small-group walking one that includes the francesinha, so Dani gets to taste the famous sandwich with some local company."),
       ("yes. what about getting between the two cities",
        "The train between Lisbon and Porto takes about three hours and runs through pleasant countryside. Book a seat ahead for busy days, and pick a morning departure so the afternoon is free in Porto."),
       ("perfect. i'll book hotels this weekend",
        "A good plan, since May fills up faster than people expect. Choose somewhere central in each city, close to a metro or tram stop, so you can walk to most things."))
EXTEND("menu",
       ("should i do bread",
        "A loaf of good bread with salted butter is enough, and it will go well with the lentil soup. Buy it the same afternoon so it stays fresh."),
       ("what do i do while guests arrive",
        "Set out a bowl of olives and a jug of water with lemon by the door, so people have something in hand right away. The soup can wait warm on a low heat, which frees you to chat."),
       ("ok i feel much better now",
        "Good. Writing the plan on one page helps too, with the soup on Thursday, the stroganoff and tart on Friday, and a short list of what to set out. You will have an easy evening."))
EXTEND("reading",
       ("i picked up piranesi from the library today",
        "Good. Read the first chapter slowly, because the house and its statues are the whole world and the details pay off later. A quiet evening is the best time to begin."),
       ("a friend recommended a podcast about trees too",
        "A tree podcast pairs well with the book you loved. Listening on a walk gives you the feeling of a forest even when the weather is bad."),
       ("ha, i might do both",
        "A mix of a novel and something to listen to keeps the habit alive. The slump tends to loosen once reading starts to feel like fun again."))
EXTEND("tenk",
       ("teo says he's done a 10k before",
        "Then Teo can be your pacing guide on Tuesdays. Ask him how he handled the last two miles, since that is where most first-timers need a plan."),
       ("also what shoes",
        "Go to a running shop and try a few pairs, ideally in the late afternoon when your feet are slightly swollen. A pair that feels comfortable right away is better than one you hope will break in."),
       ("ok i'll go thursday",
        "Thursday works, and it leaves two days to test them before the long run. Wear the socks you plan to run in."))
EXTEND("camping",
       ("rae is bringing a big tent, so that's covered",
        "Good news. A tarp under the tent keeps the floor clean, and a mat inside makes the ground feel softer."),
       ("what about food for saturday",
        "Make Saturday simple: eggs and tortillas for breakfast, sandwiches for a trail lunch and something grilled or one-pan for dinner. Pack it in labeled bins so nobody digs through the whole car."),
       ("perfect, sounds good",
        "It will be a good weekend. Check the forecast the night before and pack one extra layer for each person, since the desert surprises people."))
EXTEND("dadgift",
       ("kenji wants to write the card",
        "A handwritten card from both of you makes the gift more personal. A short line about a good fishing day with him will mean more than anything polished."),
       ("good. where do we buy the journal",
        "A stationery shop or a good outdoor store usually has leather-bound journals, and a small local maker could add his name in gold on the cover. Order early enough that the engraving does not rush you."),
       ("ok i'll look this weekend",
        "A weekend is plenty of time to compare a few options. Check the paper quality, since good paper holds ink better and lasts longer."))
EXTEND("speech",
       ("should i practice in front of someone",
        "Reading it to one trusted listener is very useful, especially someone who knows Hana. They can tell you which lines make them smile and which ones drag."),
       ("ok i'll read it to my mom",
        "Your mom sounds like the right audience. Time yourself too, and aim to land near the three minutes with a small pause after the funniest line."),
       ("thanks, feeling better about it",
        "Good. You have a story, a structure and a plan to practice, which is most of the work. The rest is polish, and it gets easier each time you read it aloud."))

# ------------------------------------------------------------------ short acknowledgements
# A plain thanks, a goodbye or a "wish me luck" at the end of a long chat gets one or two warm sentences tied to
# the conversation, no question and no padding.
Q("niece", "close", "thanks, this was really helpful",
  "Glad it came together. A fossil dig, two stations for the 12 kids and a lemon cake is a good plan for Saturday.",
  ref=("fossil dig", "lemon"))
Q("exam", "close", "ok thanks, wish me luck",
  "Good luck on Thursday. You know more than it feels like tonight, and the mechanisms will make more sense after sleep.",
  ref=("Thursday", "mechanisms"))
Q("driving", "close", "ok thanks, talk later",
  "Good luck with the lot sessions with Raj. Ending each one on a good park will make the retake feel familiar.",
  ref=("Raj", "retake"))
Q("laptop", "close", "cool, i'll set it up tonight",
  "Tonight is a good time. Open the separate account first, then point the automatic 300 at it right after payday.",
  ref=("separate account", "300"))
Q("bike", "close", "ok that's all i needed, thanks",
  "Glad that sorted it. Mudguards, a drop of oil on the chain and an easy gear up the hill should make the 9 km much nicer.",
  ref=("mudguards", "chain"))
Q("breakup", "close", "thanks for listening",
  "It makes sense to say all of this out loud. The hoodie, the cereal and the quiet can all wait until you are ready.",
  ref=("hoodie", "cereal"))
Q("camping", "close", "alright, going to go pack",
  "Have a good weekend in Joshua Tree. An extra layer each and the labeled food bins are the two things to check before you leave.",
  ref=("Joshua Tree", "extra layer"))
Q("speech", "close", "ok, off to rehearse. fingers crossed",
  "Good luck. The Prague story already has what it needs, so tell it slowly and pause after the funniest line.",
  ref=("Prague",))
Q("tenk", "close", "great, i'll go find a pair this week",
  "Enjoy the shoe shopping on Thursday. With Teo on Tuesdays, the long runs will be in good company.",
  ref=("Thursday", "Teo"))
Q("lighthouse", "close", "ok i'm gonna go write the ending",
  "Go for it. Odile stepping aboard is a strong place to end, and never seeing where the ship goes keeps the mystery alive.",
  ref=("Odile", "ship"))
Q("menu", "close", "ok thanks, i'm off to shop",
  "Enjoy the shopping. Start with the lentils and lemons, since the soup is the first thing to make on Thursday.",
  ref=("lentil", "Thursday"))
Q("piano", "close", "cool, thanks, off to practice",
  "Enjoy it. Ask Mona on Sunday which easy arrangement fits your hands, and keep the first sessions slow.",
  ref=("Mona", "Sunday"))


# ======================================================================================================
# LONG THREADS (round 4 review): 8-9 exchanges with base-length replies (about 60 words and up), 4,500-7,000 characters
# of history. Questions at the end ask about the first exchanges (a fact the person stated, a figure the
# assistant gave), something never said, or build on the thread. Each thread feeds at most three rows.
# ======================================================================================================
LONG_THREADS: set[str] = set()


def TL(name, *ex):
    T(name, *ex)
    LONG_THREADS.add(name)


# ------------------------------------------------------------------ backyard beds
TL("beds",
   ("i finally got my landlord's ok to build raised beds along the back fence, two of them, 4 by 8 feet, and the soil here is heavy clay",
    "Raised beds are the right answer for clay, because you build the good soil on top of the bad instead of fighting it. Aim for about 10 to 12 inches of depth so roots have room, and leave a path of at least two feet between the beds so you can kneel and reach. Put them where they get six or more hours of sun, which along a fence usually means thinking about which side the shade falls on. Loosen the clay underneath with a fork before you set the frames down, so water can still drain downward. Two beds that size are a sensible start for one person."),
   ("cedar looks nice but it's pricey, what else is ok",
    "Untreated pine or fir works well and costs about half as much, and in a garden it usually lasts five to seven years before it needs replacing. If you want something longer lasting, galvanized corrugated metal frames are popular and tidy, though they warm up in strong sun. Avoid old railroad ties and anything labeled as pressure treated before 2004, because older treatments contained chemicals you would rather keep out of vegetables. A layer of cardboard on the ground inside the frame will smother grass and weeds before you fill the bed."),
   ("pine it is. now filling them, i do not want to buy forty bags of soil",
    "You do not need to. A bulk delivery from a landscape supply yard is much cheaper than bags. Each bed is 4 by 8 feet and a foot deep, which is 32 cubic feet, so the two together need about 64 cubic feet, or roughly 2.4 cubic yards. A common recipe is about 60 percent topsoil and 30 percent compost, with the rest made up of something airy like coconut coir or fine bark. You can save money by filling the bottom third with logs, sticks and fall leaves, which rot down slowly and feed the bed from underneath."),
   ("2.4 yards, ok. it's late march, what can i plant first",
    "Late March is perfect for the cool-season crops that actually prefer chilly soil. Peas, spinach, lettuce, radishes and kale can all go in as soon as the ground is workable, and a few can handle a light frost. Radishes are a nice first win, because they sprout in days and are ready in about four weeks. Hold back on tomatoes, peppers and basil until after your last frost, which in most places is around mid May. Cover young seedlings with an old bedsheet on cold nights, and the early crops will be harvested just as the summer ones go in."),
   ("my neighbor swears slugs wreck everything in this neighborhood",
    "Slugs love young leaves and damp evenings, so they are worth planning for rather than fearing. Water in the morning so the surface is dry by night, and keep the beds free of boards and piles where they hide. A shallow dish of beer sunk into the soil catches a surprising number, and a ring of copper tape around a bed makes them reluctant to cross. Going out an hour after dark with a flashlight and a cup of soapy water is unglamorous but very effective for the first week. Wide, clean paths between beds also give them fewer places to travel."),
   ("lol i'll try the beer. my daughter wants sunflowers, can they share a bed with the rest",
    "They can, as long as they stand at the north end so they do not shade everything else. Sunflowers grow tall quickly and make a sturdy pole for pole beans, which is a lovely pairing, and kids enjoy watching them climb. Give them a hole about an inch deep and a few inches apart, and thin the seedlings to one every foot. Your daughter could measure them each week with a tape measure and keep a little chart, which turns the bed into a science project as well."),
   ("she'll love the chart. should i put a compost bin back there too",
    "A compost bin by the back fence is a good idea if you keep it a little way from the beds and from the neighbors' windows. A simple pallet bin or a pre-made tumbler works, and the trick is a mix of greens, like kitchen scraps and grass clippings, and browns, like dry leaves and shredded paper, in roughly equal parts. Keep it as damp as a wrung-out sponge and turn it every week or two. In a few months you will have dark crumbly compost to top up the beds, and your clay soil will slowly get better too."),
   ("ok i think i have a plan. building day is the first saturday in april",
    "That gives you a few weeks to order the lumber, book the soil delivery for the Friday before and gather what you need. On the day, assemble the frames in place, check that they are level, line the bottom with cardboard and then fill in layers so you are not carrying anything twice. Plant the peas and radishes that same afternoon if you can, since the first seeds in the ground make it feel real. Take a photo of the empty beds before you start, and one at the end of summer, so you can see how far they come."))
Q("beds", "rec_user", "how big were the beds supposed to be",
  "Two beds, each 4 by 8 feet, along the back fence. Together they work out to about 64 cubic feet of soil at a foot deep. Do you have the frames cut yet, or are you waiting for the lumber?",
  ref=("4 by 8",))
Q("beds", "rec_asst", "how much soil did you work out for them again",
  "About 2.4 cubic yards in total, which is roughly 64 cubic feet for the two beds. A bulk delivery from a landscape supply yard costs far less than bags, and the logs and leaves in the bottom third cut the amount you need to buy.",
  ref=("2.4 cubic yards", "64 cubic feet"))
Q("beds", "rec_none", "what did you say the name of that garden center was",
  "I haven't settled on a garden center in this chat. I only suggested a landscape supply yard for the soil delivery, and the delivery date is the Friday before your first Saturday in April. If you have a couple near you, which one is closest?",
  absent="garden center", ref=("landscape supply yard", "Saturday in April"))

# ------------------------------------------------------------------ anniversary trip
TL("anniv",
   ("our tenth anniversary is in october and i want to surprise my husband jonas with a trip, budget around $2,000 total",
    "Ten years is worth marking, and $2,000 goes a long way if you pick a place that does not need a flight. A long weekend somewhere with a bit of character, such as a mountain town, a coastal inn or a small city with good food, will feel more special than a big trip squeezed into a tight budget. Think about what Jonas loves most, whether that is hiking, cooking, music or doing absolutely nothing. Surprises work best when you control the details but let him pack for a clue, like a jacket or swim trunks. Do you know yet how many nights you could get away?"),
   ("4 nights. he loves mountains and good beer",
    "Then Asheville in North Carolina is a strong candidate. It sits in the Blue Ridge Mountains, with trails and waterfalls close to town, and it has more breweries per person than almost anywhere in the country. In October the leaves are at their best, and you can spend the days hiking and the evenings tasting. A small inn or a cabin just outside town gives you quiet mornings, and the town itself is walkable enough that you do not need to drive after a flight of beer."),
   ("asheville sounds perfect. how much would it cost roughly",
    "For four nights, a reasonable estimate is about $1,700 to $1,900 if you drive. Lodging in October is the biggest piece, around $200 a night for a nice inn, so about $800. Meals and drinks for two come to roughly $120 a day, so another $480. Gas depends on the distance, perhaps $100 to $150 for the round trip, and a couple of activities such as a parkway drive, a brewery tour and one special dinner can take another $250. That leaves a little room for a small gift or a bottle of something to open on the last night."),
   ("we'd be driving from nashville so that's about 4 hours",
    "That is a gentle drive, and it means you can leave after breakfast and arrive in time for a late lunch. The road through the Smoky Mountains is beautiful in October, so allow an extra hour or two for pull-offs, particularly if the colors are at peak. Fill the tank before you get to the mountains, and download your music beforehand since the signal can be patchy. If Jonas drives, you can keep the destination secret until the last stretch by telling him you are going somewhere just for lunch."),
   ("ha, sneaky. what should we do the first night",
    "Keep the first evening light. After the drive, a walk around the downtown with a stop at one of the smaller breweries, followed by a relaxed dinner, is a lovely way to settle in. Book the dinner for a bit later than you would normally eat, so you are not rushing from the car. Save the big hike for the second morning when you are rested, and pick a trail with a view at the end, since Jonas will probably want a photo to remember it by."),
   ("good plan. what about something small to give him at dinner",
    "A small, personal gift will mean more than something expensive. A framed photo from your wedding week, a handwritten page with ten things you love about the last ten years, or a bottle of beer from the brewery where you had your first date would all work. Slip it next to his plate at the dinner on the first night, and let him read it quietly. If you want to add one tangible thing, a pair of good hiking socks makes a funny and useful companion."),
   ("i love the ten things idea. i'll write it on the drive down in the passenger seat",
    "Writing it in the car is charming, though you might want to draft a few lines beforehand so the notes are not a scramble on the road. Aim for specific memories, like the time he fixed your bike in the rain, rather than general compliments, because details are what make people tear up. A good pen and a thick card prevent smudges when the road bumps. Leave a space at the end for a line about the next ten years."),
   ("ok last thing, how do i keep him from guessing before we go",
    "Share as little as you can, and plant a harmless false trail, such as telling him you are planning a dinner on that Friday evening. Pack his bag yourself at the last minute, or hand him a packing list that only says layers and comfortable shoes. Ask a friend to cover any calendar conflicts so his work week is clear without questions. On the morning, give him the first clue only when the car is already running, and enjoy his face."))
Q("anniv", "rec_user", "what was the budget for the whole thing again",
  "You said about $2,000 total for the trip, and the four-night Asheville plan I sketched came in around $1,700 to $1,900 if you drive. That leaves a small buffer for a gift or a bottle to open on the last night.",
  ref=("$2,000",))
Q("anniv", "rec_asst", "how much did you figure for the inn per night",
  "I estimated about $200 a night for a nice inn in October, which comes to around $800 for the four nights. Meals and drinks at roughly $120 a day make up most of the rest. Would a cabin outside town be cheaper than an inn in your plan?",
  ref=("$200", "$800"))
Q("anniv", "build", "can you remind me what the surprise plan is, start to finish",
  "You drive from Nashville to Asheville for four nights, leaving after breakfast, with Jonas thinking it is a lunch trip until the last stretch. The first evening is a walk, a small brewery and a late dinner, where you give him the ten things you love about the last ten years. The big hike waits until the second morning.",
  ref=("Asheville", "ten things"))

# ------------------------------------------------------------------ thesis
TL("thesis",
   ("i'm in the third year of my phd in sociology and i can't get my literature review to hang together, my advisor Dr. Abara wants a draft by december 3rd",
    "A literature review stops feeling like a pile when it starts answering a question instead of summarizing papers. Before you write another page, try to say in two sentences what gap in the research your study fills. Then group the sources by the argument they make, not by author or date, and let each section move toward that gap. A draft by December 3rd is realistic if you spend the next two weeks outlining and the following four writing. Do you already have a pile of sources, or are you still reading?"),
   ("pile. like 90 papers and a spreadsheet i don't trust",
    "Ninety papers is a lot, and many of them probably do not need a full paragraph each. Make a simple table with one row per paper and three short columns: the main claim, the method and how it connects to your question. Mark each one as essential, supporting or background. Usually fewer than a third are essential, and those carry the narrative. Everything else can be cited in a sentence or a footnote. Rebuilding the spreadsheet this way is slow for a day or two, but it replaces a vague worry with a concrete map."),
   ("ok that makes sense. how do i find the actual argument though",
    "Look for the disagreements. Read your essential papers and ask where two of them would argue if they met, such as whether neighborhood effects come from resources or from social ties. Those conflicts are the skeleton of a good review. Write one paragraph for each major disagreement, starting with the question, then laying out each side and ending with what is still unresolved. Your own study then slots into the unresolved part. If you cannot find any disagreement, the sources are probably too similar, and it is worth adding a few from a neighboring field."),
   ("there's definitely a fight between the social ties people and the resources people",
    "That is a great skeleton, and it already gives you the title for your central section. Present the social ties camp first, with their strongest evidence, then the resources camp, and be fair to both, because committees are quick to spot a straw man. After that, write a short section on what both overlook, which is where your study comes in. A good signpost sentence between sections, such as, these accounts diverge on the role of money, helps readers follow the thread."),
   ("my advisor wants me to also cover the qualitative studies, which i've been ignoring",
    "Qualitative work is often where the explanation lives, so it can strengthen your argument rather than clutter it. Add a small column for those studies in your table and note what mechanism each one describes. Many will fall into either the social ties side or the resources side, and a few might show a third pattern that belongs in your section on what both camps overlook. You do not need to treat them as a separate section unless Dr. Abara asks for one. A brief paragraph that weaves the quotes into the debate is usually enough."),
   ("good, i'll weave it in. i keep getting stuck writing the first paragraph",
    "The first paragraph is usually the hardest, because it has to promise everything. A trick that helps is to write it last, or to write a rough placeholder now and plan to rewrite it once the sections exist. Start with the middle, where the argument is clear to you, and let the introduction follow. If you must begin at the top, write one sentence about the problem, one about the debate and one about your contribution, and then stop. An ugly paragraph that exists can be edited, and an empty page cannot."),
   ("true. what should my schedule look like between now and the 3rd",
    "Work backward. Spend the first week finishing the table and a one-page outline, the next two weeks drafting the main debate sections, and the following week on the sections about qualitative work and the gap. Leave the last days for the introduction, a read-aloud pass and a clean reference list. Send Dr. Abara the outline early, perhaps at the end of the first week, so any disagreement about structure surfaces while there is still time. Protect two or three fixed writing sessions each week rather than waiting for big blocks."),
   ("ok. honestly i'm scared she'll hate it",
    "That fear is extremely common at this stage, and it usually says more about how much you care than about the draft. A review that arrives on time and shows a clear argument is already a good draft, even if it needs revising. Advisors expect to push back, and December 3rd is for starting that conversation, not for finishing the chapter. When you send it, a short note naming the two things you most want her to react to will keep the feedback focused. The first reaction is rarely as harsh as the one you imagine."))
Q("thesis", "rec_user", "what date are we aiming for with the draft",
  "December 3rd, for Dr. Abara. The plan we sketched has the outline done in the first week, the main debate sections in the next two, and the introduction left for the last days. Is the table finished yet?",
  ref=("December 3rd", "Dr. Abara"))
Q("thesis", "rec_asst", "what three columns did you say to put in the paper table",
  "The main claim, the method, and how the paper connects to your question. I also suggested marking each paper as essential, supporting or background, since usually fewer than a third are essential. It turns the spreadsheet into a map.",
  ref=("main claim", "essential"))
Q("thesis", "emo", "i think i just needed to hear it's normal to feel this way",
  "It is normal, and it says nothing bad about your work. You have ninety papers read, a real disagreement between the social ties people and the resources people, and a plan that ends well before December 3rd. Being scared of Dr. Abara's reaction is part of caring about the chapter, and it tends to shrink once the outline exists. Which part of the plan feels most doable this week?",
  ref=("ninety papers", "Dr. Abara"))

# ------------------------------------------------------------------ kitchen reno
TL("reno",
   ("we're redoing our kitchen and the contractor, Luis, quoted 14,500 for new cabinets and counters. does that sound fair?",
    "It can be, but a lot depends on the size of the kitchen and the materials. For a typical mid-size kitchen, $14,500 for cabinets and counters is in a sensible range if it includes demolition, installation and a mid-grade countertop such as quartz or butcher block. Ask Luis to break the quote into lines: cabinets, counters, labor, haul-away and any plumbing or electrical work. Comparing the numbers in categories is easier than judging a single total. Also check that the quote states a start date, a finish date and what happens if costs change."),
   ("he says 6 weeks. it includes counters and installation but not the sink or faucet",
    "Six weeks is reasonable for custom or ordered cabinets, as lead time on those can be three to four weeks by itself. Not including the sink and faucet is common, so budget a few hundred dollars for a decent set, or more if you want an apron-front sink. Ask whether the quote covers disconnecting and reconnecting the plumbing, since sometimes that is billed as a separate fee. It is also worth asking who orders the materials, because delays in delivery are the usual cause of a project dragging past its estimate."),
   ("good point. we're thinking white shaker cabinets and a butcher block counter",
    "White shaker cabinets are a safe choice that will look good for many years, and butcher block adds warmth. The main thing to know is that wood counters need care: seal them with food-safe mineral oil when they are installed and reapply it every month or so at the start. Keep standing water away from the sink edges, since that is where wood is most likely to stain. If you cook a lot, a section of stone near the stove can protect the wood from hot pans while keeping the butcher block elsewhere."),
   ("hmm, stone near the stove is interesting. does that look weird?",
    "Not if you do it deliberately. Mixed materials are common in current kitchens, and a stretch of stone or quartz around the range often looks intentional, especially if the color picks up something in the cabinets or backsplash. A white or soft gray stone against white shaker doors and warm wood reads as calm and balanced. Ask Luis to show samples in your kitchen light, because colors change a lot between the showroom and your own window."),
   ("ok. what about lighting, the kitchen is pretty dark",
    "Lighting may do more for the room than the cabinets. Aim for three layers: ceiling lights for general light, under-cabinet strips to light the counters where you work, and a pendant or two over any island or table for warmth. Under-cabinet lights are inexpensive and make a huge difference when you chop vegetables. Choose bulbs around 2700 to 3000 kelvin so the white cabinets look warm instead of clinical, and put the main lights on a dimmer."),
   ("good. my wife wants an island but the room is narrow, like 10 feet wide",
    "In a 10 foot wide room, a full island is tight. You want about three feet of clearance on each side so two people can pass, and that leaves very little for the island itself. A slim rolling cart or a narrow peninsula attached to one wall can give extra counter space without blocking the walkway. Tape the outline on the floor and live with it for a couple of days before deciding, as you will quickly feel whether it works."),
   ("great idea with the tape. what should we ask luis before signing",
    "Ask for a written schedule with milestones, a payment plan tied to progress instead of a big upfront deposit, proof of license and insurance, and the name of the person who will be on site each day. Ask what happens if the cabinets arrive damaged, and who is responsible for cleaning. Request a short list of two or three recent customers you can call. A good contractor welcomes questions, and the answers will tell you whether the $14,500 is a fair deal."),
   ("thanks. i feel more prepared now",
    "You should. You have a quote to break into lines, a six week timeline to pin down, a countertop plan and a way to test the island before spending anything. Write the questions for Luis in one note so you can go through them in order at the next meeting. Take photos of the kitchen before work starts, and keep all the messages about changes in one thread, which protects both of you if something is misunderstood later."))
Q("reno", "rec_user", "what was the quote from the contractor",
  "Luis quoted $14,500 for new cabinets and counters, with about six weeks of work. It does not cover the sink or faucet, so budget a few hundred dollars for those on top.",
  ref=("Luis", "$14,500"))
Q("reno", "rec_asst", "how much clearance did you say you want around an island",
  "About three feet on each side, so two people can pass comfortably. In a room 10 feet wide, that leaves very little room for a full island, which is why I suggested a slim cart or a narrow peninsula, and taping the outline on the floor first.",
  ref=("three feet", "10 feet"))
Q("reno", "build", "so what's the order of things to do before we sign",
  "Break Luis's $14,500 quote into lines for cabinets, counters, labor and plumbing, then ask about the start and finish dates and who orders the materials. Check his license and insurance, and ask for a payment plan tied to progress. Tape the island outline on the floor too, so you and your wife can decide with real space in front of you.",
  ref=("$14,500", "island"))

# ------------------------------------------------------------------ puppy
TL("juno",
   ("we got a puppy last weekend, a beagle mix named Juno, she's about 10 weeks old and she cries all night in her crate",
    "Congratulations on Juno. Crying in the crate during the first week or two is almost universal, because she has just left her mother and her littermates and the night is a big new place. Put the crate next to your bed so she can hear and smell you, and tuck in a soft blanket that you have slept in. Take her out for a final bathroom break right before bed, and plan on one or two quick trips outside during the night, since a 10 week old puppy cannot hold her bladder for eight hours. Most puppies settle within about ten days."),
   ("ok. should i ignore the crying or go to her",
    "A middle path works best. If she is whining, wait a minute or two to see if she settles by herself, because puppies often cry briefly while falling asleep. If the crying becomes frantic or she has been asleep a few hours, take her out calmly for a bathroom trip, with no play or chat, and put her straight back in. This teaches her that the night is for sleeping, and that you do come when she really needs something. A warm water bottle wrapped in a towel can add comfort."),
   ("she keeps having accidents inside too, even after she's been out",
    "That is normal at her age, and it is nearly always about timing rather than stubbornness. Take Juno out every one to two hours while she is awake, right after meals and naps, and straight after play. Go to the same spot each time and give a quick treat the second she finishes outside. If you catch an accident mid-act, interrupt gently and carry her out, and never scold afterwards because she will not connect it. Clean the spot with an enzyme cleaner, which removes the smell that would otherwise bring her back."),
   ("makes sense. when does she get her vaccines",
    "Puppies usually get their first round at 6 to 8 weeks, with boosters every three to four weeks until about 16 weeks, so Juno is probably due for her next one soon. Ask your vet to review the schedule at the first visit. Until she has finished the series, avoid dog parks and places where unknown dogs have been, but do carry her around the neighborhood and let her see people and sounds, because the early weeks are an important window for confidence."),
   ("i booked the vet for thursday at 4. what should i ask them",
    "Bring a list. Good questions include what food and amount are right for a beagle mix, when to start flea, tick and heartworm prevention, when to spay, and what to do about chewing and teething. Ask what signs mean you should call straight away, such as vomiting or refusing food. Bring a stool sample if you can, since many vets want one, and a note of what she has eaten so far. A few treats in your pocket will help Juno make friends with the clinic."),
   ("great. she also chews everything, my shoes especially",
    "Chewing is how puppies explore and soothe sore gums, so the aim is to redirect rather than stop it. Keep shoes out of reach, and give her plenty of safe things that are more interesting, like rubber toys you can freeze, rope toys and a stuffed chew. When you catch her on a shoe, swap it for a toy and praise her for taking it. Puppy-proof the room, tire her out with a walk and short training sessions, and the chewing should ease as her adult teeth come in at around five or six months."),
   ("ha, ok. any tips for when she's alone while i work",
    "Build up gradually. Start by leaving her for a few minutes with a stuffed chew toy, then extend the time over several days, coming back before she gets upset. Keep your departures and returns calm and unremarkable. A tired puppy copes better, so a walk and play before you start work helps. For a 10 week old, no more than two or three hours alone is fair, so arrange a midday break for the bathroom. A radio or a recording of quiet talk can make the house feel less empty."),
   ("this is all really helpful. i just want her to feel safe",
    "She will, and what you are already doing is the heart of it. The crate by your bed, the calm night trips outside, the treat after a successful bathroom break, and the slow introduction to being alone all tell Juno that her world is predictable and kind. Beagles are social and affectionate dogs, so as the weeks go by she will probably start following you from room to room. Be patient with the setbacks, because they are part of the normal path."))
Q("juno", "rec_user", "what breed is she again and how old",
  "Juno is a beagle mix, about 10 weeks old. That is young enough that she cannot hold her bladder for a full night, so the trips outside are normal for now.",
  ref=("beagle mix", "10 weeks"))
Q("juno", "rec_user", "what time did she have the vet appointment, just checking it matches",
  "You booked it for Thursday at 4. Bring a list of questions, a stool sample if you can and a few treats so Juno makes friends with the clinic. Is the chewing the main thing on your list?",
  ref=("Thursday at 4",))
Q("juno", "build", "what was the idea for the chewing again",
  "Redirect it rather than stop it: keep your shoes out of reach, offer frozen rubber toys, rope toys and a stuffed chew, and swap a toy in whenever Juno grabs a shoe, with praise when she takes it. It should ease once her adult teeth come in at around five or six months.",
  ref=("shoes", "adult teeth"))

# ------------------------------------------------------------------ ceramic shop
TL("shop",
   ("i make ceramic mugs as a hobby and my friends say i should sell them. i'm thinking of opening an etsy shop called Clay & Tide",
    "That is a lovely name, and friends who ask for more are a good sign. An Etsy shop is a low-risk way to test the idea, since you can start small and see what sells. Before you list anything, decide on a consistent look: a few glaze colors, a style of handle and a shape that makes your mugs recognizable. A set of six to ten pieces is a good opening collection. Do you have a way to photograph them in good daylight yet?"),
   ("not really, i've just used my phone on the kitchen table",
    "Your phone is enough if you control the light. Place the mug near a window on a plain background, such as a white board or a piece of linen, and shoot in the morning when the light is soft. Take a front view, a view of the handle, the inside and one with a hand holding it for scale. A close-up of the glaze texture sells ceramics because buyers cannot touch them. Keep the same background for every photo so the shop looks cohesive."),
   ("ok. how do i price them? i was thinking $28",
    "Add up your costs first: clay, glaze, firing, packing materials, Etsy fees of roughly 10 percent, and shipping if you offer free shipping. Then value your time, even at a modest hourly rate, because mugs take hours across throwing, trimming, glazing and firing. A handmade mug often sells for $30 to $45, so $28 is on the low side for a one of a kind piece. Starting around $34 leaves room for a sale or a discount for a set. You can always adjust after the first few sales."),
   ("34 feels high but i see your point. shipping worries me, they'll break",
    "Fragile items need a layered approach. Wrap each mug in a few turns of bubble wrap, put it in a box with at least two inches of padding on every side, and fill any gaps so it cannot shift. Shake the sealed box gently, and if you hear movement, add padding. Use a sturdy double wall box, mark it as fragile and consider tracking with insurance for orders above $50. Many sellers include a handwritten note about care, which also makes buyers feel looked after."),
   ("good tips. how many mugs should i make before launching",
    "Aim for about twelve to fifteen finished pieces, in at least three glaze colors, so the shop does not look empty and you can see which colors attract attention. Do not wait to make fifty. Etsy shoppers like to see a shop with a few items and fresh listings added regularly, and you will learn faster from real orders than from making more in the dark. Photograph and list a few each week after launch."),
   ("ok. what should I write in the descriptions",
    "Be specific and warm. Say the size in ounces and inches, the type of clay and glaze, whether it is microwave and dishwasher safe, and that each piece is made by hand so small differences are part of the charm. Add a sentence or two about how it feels to use, such as the weight of the handle or the way the glaze pools. Keep the first line strong, since that is what shows in search. A short story about how you started can go in your shop About section."),
   ("love it. what about the name, is Clay & Tide ok to use",
    "It is distinctive and easy to remember. Before committing, search for it on Etsy and on Google to see if another seller or business already uses something close, and check whether the matching Instagram handle is free. You can also search the trademark database for similar names in crafts and home goods. If everything is clear, you can reserve the shop name for free and start building the brand."),
   ("thanks, i'm excited and a little terrified",
    "Both feelings fit, and neither means you should wait. Excitement tells you the shop matters to you, and nerves usually settle once the first listing is live. Set a small goal, such as launching with twelve mugs, and a gentle deadline, so the idea does not drift. Tell a couple of friends the date and ask them to be your first buyers and reviewers. Starting small leaves you free to learn."))
Q("shop", "rec_user", "what name was the shop going to have",
  "Clay & Tide. It is distinctive and easy to remember, and the one thing left is checking that nobody on Etsy or Instagram already uses something close.",
  ref=("Clay & Tide",))
Q("shop", "rec_asst", "what price did you think the mugs should start at",
  "Around $34, which leaves room for a discount on a set. Handmade mugs often sell for $30 to $45, and $28 would be low once you count the clay, firing, packing and Etsy fees.",
  ref=("$34", "$28"))
Q("shop", "fact", "random thing, how long does glaze firing usually take",
  "A typical glaze firing takes around 8 to 12 hours to reach temperature, and the kiln then needs most of a day to cool before it is safe to open. Studios often fire overnight and unload the next afternoon. Opening too early can crack the pieces. Do you fire in your own kiln or at a studio?",
  ok=("8", "12"), ref=("glaze",))

# ------------------------------------------------------------------ oregon coast
TL("coast",
   ("my brother Eli and i are driving the oregon coast in august, 5 days in his old camper van. where should we stop?",
    "That is a great trip, and the coast is made for slow driving. A classic pattern is to start in Astoria in the north, with its old harbor and the nearby beaches at Cannon Beach, and work south through Tillamook, Newport and Florence before ending near Bandon. With five days, you can spend one night at each of four or five places without feeling rushed. In August the days are long and the weather is mild, though the mornings can be foggy. Book your campsites ahead, since the state parks fill up months early."),
   ("good. eli wants to hike and i want to eat seafood",
    "That balances nicely along this route. Hike at Ecola State Park near Cannon Beach for sea stacks and big views, and at Cape Perpetua near Yachats, where the trails drop to tide pools and a thundering blowhole called Thor's Well. For seafood, Newport has working docks and small places serving crab and chowder straight from the boats. In Tillamook you can also add a cheese factory stop, which Eli will probably enjoy as a snack break. Eat your biggest meal at lunch, when the fish is freshest and the lines are shorter."),
   ("what about the van, it's a 1994 and not the most reliable",
    "A quick check before you leave is worth the money. Have a mechanic look at the brakes, tires, belts, coolant and oil, and carry a spare tire, a jack and jumper cables. The coast highway has plenty of climbs and curves, so go easy on the brakes on the downhill sections and use a lower gear. Fill the tank whenever it drops to half, since some stretches have a long gap between stations. A roadside assistance plan is cheap insurance. Pack a paper map too, because phone signal fades on parts of the coast."),
   ("ok. we'll get it checked. how much should we budget for gas",
    "For a trip of about 350 miles of highway and side trips, plus some extra driving between beaches and campgrounds, plan on 500 miles in total. An older van might get 15 miles to the gallon, so that is around 33 gallons. At roughly $4.50 a gallon on the coast, you are looking at about $150 for fuel. Add a margin for detours and you can set the gas budget at $200 for the whole trip."),
   ("not bad. what about camping spots, which parks are best",
    "Fort Stevens near Astoria has great sites with beach access and bike trails. Further down, Beverly Beach near Newport is popular and close to the water, and Jessie M. Honeyman near Florence sits among giant sand dunes beside a lake. Cape Lookout is a lovely stop too, though it books out quickly. Reservations open about six months in advance on the state park website, so check dates early. If a site is unavailable, private campgrounds nearby usually have space, even in August."),
   ("sand dunes sounds fun. can we sandboard or something",
    "Yes, near Florence the Oregon Dunes National Recreation Area has some of the tallest coastal dunes in North America, and a few shops rent sandboards by the hour. Wear sunscreen and bring plenty of water, as the dunes are exposed. Climbing is the hard part, so pick a dune that is not too steep for the first run, and sit or lie on the board for your first few slides. Eli will probably want to try the tallest one at least once."),
   ("ha yeah. what should we pack for the weather",
    "Layers are the answer. Even in August, the coast can be cool and windy, so bring a fleece, a rain jacket and a warm hat for the evenings, along with shorts for the sunny hours. Pack sturdy shoes for hiking and sandals for the beach, and a couple of towels that dry fast. A small stove, a cooler and a lantern make the van feel like home. Do not forget sunscreen, sunglasses and a flashlight, since campgrounds get dark."),
   ("perfect. i think we've got a real trip planned",
    "You do. Five nights between Astoria and Bandon, hikes at Ecola and Cape Perpetua, seafood in Newport, the dunes near Florence, and the van checked before you leave. A relaxed pace and a small buffer in your budget will let you enjoy the unplanned stops, which are often the best part. Take a notebook and a camera so you and Eli can look back on it later."))
Q("coast", "rec_user", "who is coming on the trip and what's the vehicle",
  "Your brother Eli, in his old 1994 camper van. A quick check from a mechanic before you leave would be wise, especially the brakes and tires for the coast's long downhill stretches.",
  ref=("Eli", "1994"))
Q("coast", "rec_asst", "which trail was it with the blowhole",
  "Cape Perpetua near Yachats, where the trails drop to tide pools and a blowhole called Thor's Well. The other hike I mentioned was Ecola State Park, near Cannon Beach, for the sea stacks. Which of the two does Eli want more?",
  ref=("Cape Perpetua", "Thor's Well"))
Q("coast", "build", "how much did we say for gas",
  "About $200 for the whole trip. I worked it out as roughly 500 miles in the van at around 15 miles to the gallon, with $4.50 a gallon, and then added a margin for detours.",
  ref=("$200", "15 miles"))

# ------------------------------------------------------------------ salary negotiation
TL("offer",
   ("i got a job offer from Brightwater Health at $82,000 and the recruiter, Tessa, wants an answer by friday. i was hoping for around 90",
    "Congratulations on the offer. The fact that Tessa gave you a deadline does not mean the $82,000 is final: most first offers leave some room, and asking politely rarely costs you the job. Before you reply, work out your reasons for $90,000, such as the market rate for your role, your experience and any competing interest. It helps to know your walk-away number too. You have until Friday, so you can take a day or two to prepare rather than answering on the spot. Do you know what similar roles pay in your city?"),
   ("a few listings say 85 to 95 for the same title",
    "That is useful evidence, and it makes your request easy to justify. You could say that you are excited about the role and, based on the market range of $85,000 to $95,000 for similar positions, you were hoping for something closer to $90,000. Anchoring your ask in the data keeps it professional instead of personal. Keep it short, warm and specific, and avoid apologizing for asking. Offer to discuss it on a call, since tone carries better by voice, and many recruiters can adjust more in a conversation than over email."),
   ("what if she says 82 is the best they can do",
    "Then you have other levers. Ask about a signing bonus, an earlier salary review at six months instead of twelve, extra vacation days, a professional development budget or flexibility on remote days. Companies often have more freedom on these than on base pay. You can phrase it as, if the base is fixed, I would love to explore other ways to close the gap. Whatever she offers, ask for it in writing before you accept, so the details are clear."),
   ("ok. should i say my current salary if she asks",
    "You do not have to, and in several states recruiters are not allowed to ask. If she does, you can redirect politely by saying you would prefer to focus on the value of this role and the market range you have seen. Sharing your current number tends to anchor the new offer around it, which can hurt you if you are underpaid now. If she presses, repeat your target range calmly rather than giving in to the question."),
   ("good to know. can you help me draft the email?",
    "Here is a draft you can adapt. Thank you so much for the offer, Tessa. I am really excited about the role and the team at Brightwater Health. Based on my research on similar roles, which range from $85,000 to $95,000, and the experience I would bring, I was hoping we could discuss a base salary closer to $90,000. I would be glad to talk through it by phone whenever suits you. Thank you again for your time. Edit it so it sounds like you, and read it aloud once before sending."),
   ("that's nice. what if she takes a few days to reply, can i still say yes by friday",
    "If the deadline is a hard one, ask whether it can move while she checks with the team. A short note such as, I want to give this a thoughtful answer, could you confirm by Thursday so I can respond by Friday, is reasonable. If she cannot get back to you in time, you can still accept the base salary and ask for the other items afterwards, though you lose some leverage. It is far better to ask for a short extension than to accept in a hurry and feel unsure."),
   ("makes sense. i'm nervous, i've never negotiated before",
    "Almost nobody feels comfortable the first time. It might help to remember that negotiation is a normal part of hiring, and Tessa will have heard many requests like yours. Practice the key sentence aloud a few times, or with a friend, so the words feel familiar. Write the ask in one line on a card by the phone. If you stay friendly, specific and calm, it will go well, even if the answer is a partial yes."),
   ("ok i'll send the email tomorrow morning",
    "Tomorrow morning gives Tessa the whole day to respond before Friday. Read it once more tonight, check the numbers and the spelling of her name, and send it when you feel rested rather than rushed. If she calls instead of writing, you have your sentence and your range ready. Whatever she says, thank her for her time. Good luck, and I hope it ends in a number you feel good about."))
Q("offer", "rec_user", "how much was the first offer and who was the recruiter",
  "Brightwater Health offered $82,000, and the recruiter is Tessa, who wants an answer by Friday. You were hoping for around $90,000, with similar listings at $85,000 to $95,000.",
  ref=("$82,000", "Tessa"))
Q("offer", "rec_asst", "what other things could i ask for if the base can't move",
  "A signing bonus, an earlier salary review at six months, extra vacation days, a professional development budget or more remote days. Companies often have more room on those than on base pay, and it is worth getting any of them in writing. Which one would matter most to you?",
  ref=("signing bonus", "six months"))
Q("offer", "rec_none", "did you say anything about the benefits package",
  "I haven't talked about the benefits at all. We went through the $82,000 base, the market range of $85,000 to $95,000 and the other things you could ask for, but nothing about insurance or retirement. If you want, we can list what to compare in the benefits before Friday.",
  absent="insurance", ref=("$82,000", "$85,000"))

# ------------------------------------------------------------------ community choir
TL("choir",
   ("i'm auditioning for a community choir on thursday night, i'm an alto, and i haven't sung in front of anyone since high school",
    "That is a brave and lovely thing to do. Community choirs are usually warm places, and an audition is mostly about checking that you can hold your part and match pitch. Altos are valued, since many people can sing the melody and fewer can hold a harmony line steadily. Between now and Thursday, sing a little each day, starting with a gentle warm-up like humming scales and lip trills. Drink water, get enough sleep and avoid clearing your throat. Do you know yet what they will ask you to sing?"),
   ("they said to prepare one song of my choice and do some sight reading",
    "For the song, choose something you love and can sing comfortably in the middle of your range, rather than a showpiece. A simple folk song, a hymn or a gentle pop tune works well. It should be about a minute or two long and finish on a note you can hold. For sight reading, practice by singing simple melodies from a hymnbook on the notes only, slowly, using a piano app to check pitch. Do not worry about perfection. They want to see how you handle new material and recover from a mistake."),
   ("i was thinking Scarborough Fair, it sits well for me",
    "Scarborough Fair is a very good pick. It has a gentle, lyrical tune, sits comfortably in an alto range and shows your tone without needing big leaps. Sing it unaccompanied at first, so you can be sure of your pitch, then try it with a phone drone note if the choir asks for a starting pitch. Pay attention to the phrasing, taking a relaxed breath at the end of each line. Try singing it standing up, as you will on Thursday."),
   ("ok. what do i do about nerves, my hands shake when i sing in front of people",
    "Shaking hands are very common, and they say your body is getting ready, not that you will fail. Slow, deep breathing helps right before you start: breathe in for four counts, out for six, a few times. Let your hands rest at your sides, or hold the sheet of lyrics lightly. Remember that the people listening want you to do well. A small ritual helps, such as drinking water and rolling your shoulders in the waiting room. Focus on the story of the song instead of how you sound."),
   ("good tips. should i wear anything specific",
    "Wear something comfortable that lets you breathe fully and move your arms, with nothing tight around the waist or chest. Choose clothes you feel good in, because confidence travels in the voice, and avoid heavy scarves or jewelry that might rustle near a microphone or a piano. Shoes you can stand in for twenty minutes are a good idea. Bring a bottle of water, a pencil for the music and a copy of your song in case they want to see the words."),
   ("will they ask me to sing in a group during the audition",
    "Some choirs do, to hear how you blend, while others audition one person at a time. If it is a group, the director will most likely have you sing your part along with a few other voices, and what they are listening for is whether you listen to each other and stay on pitch. Watch the director, breathe with the group and sing a little softer than you think you should. If they ask you to sing a part you do not know, it is fine to say so and try your best."),
   ("ok. what if i mess up the sight reading",
    "Then keep going. Directors know sight reading under pressure is hard, and they care about how you recover far more than the first note. If you lose your place, find the next beat and rejoin. It is also fine to ask for a second try or for a moment to look at the music first. Look for the key signature, the tricky intervals and the rhythm before you begin. Many auditions are about potential and attitude, and a friendly face matters."),
   ("thanks. i feel a bit more ready",
    "I am glad. You have a song that suits your voice, a warm-up routine, a plan for the nerves and a calm answer if the sight reading wobbles. On Thursday, arrive a little early, breathe, and sing Scarborough Fair the way you do when no one is listening. Whatever the result, the audition is already a win, since you did something that scared you."))
Q("choir", "rec_user", "when is the audition, and which voice part is it",
  "The audition is on Thursday night, and you're an alto. Altos are valued in a choir, since fewer people can hold a harmony line steadily. How are the daily warm-ups going?",
  ref=("Thursday", "alto"))
Q("choir", "rec_asst", "what was the breathing pattern for nerves again",
  "In for four counts, out for six, a few times right before you start. Letting your hands rest at your sides, or holding the lyric sheet lightly, helps with the shaking too.",
  ref=("four counts", "six"))
Q("choir", "build", "can you sum up the plan for thursday",
  "Arrive a little early and bring water, a pencil and a copy of Scarborough Fair. Warm up with humming and lip trills, breathe in for four and out for six, and sing the song standing up. If the sight reading wobbles, keep going and rejoin on the next beat.",
  ref=("Scarborough Fair", "sight reading"))

# ------------------------------------------------------------------ freelance taxes
TL("taxes",
   ("i started freelancing as a graphic designer in april and made about $31,000 so far. i have no idea how taxes work for this",
    "That is a good income to have for your first year, and the main thing to understand is that no employer is withholding tax for you anymore. In the US, freelancers pay income tax plus self-employment tax of about 15.3 percent, which covers Social Security and Medicare. Because nothing is taken out automatically, you are expected to send estimated payments four times a year. A common habit is to set aside 25 to 30 percent of every payment in a separate savings account. Do you know whether you have been paid on a 1099 form?"),
   ("yes, two clients sent a 1099-NEC and a few small ones paid by venmo",
    "All of it counts as income, including the Venmo payments, even if you never received a form for them. Keep a simple running spreadsheet of every payment, with date, client and amount. Clients who paid you $600 or more should send a 1099-NEC by the end of January, and you should compare it with your own records. Smaller clients do not have to send one, but you still report that income yourself. Having a consistent record now will save you a lot of stress in April."),
   ("what can i deduct? i bought a laptop and pay for adobe",
    "Business expenses reduce your taxable income, and both of those qualify. The laptop can be deducted, either fully in the year you bought it under certain rules or spread over several years, and your Adobe subscription is a normal ordinary expense. Other common deductions include software, a portion of your internet and phone bill, a dedicated home office space, professional courses and fees, and mileage for client meetings. Keep receipts and note the business purpose of each. Personal use of a laptop reduces the deduction proportionally, so keep track."),
   ("what about the home office, my desk is in my bedroom",
    "To claim the home office deduction, the space must be used regularly and exclusively for work. A desk in a corner of your bedroom can qualify if it is a distinct area used only for your business, though a space you also use for sleeping or a TV usually will not. There is a simplified method that allows $5 per square foot up to 300 square feet, so a 100 square foot space would yield $500. Take a photo of the space and measure it. Be honest, as this is an area people tend to be asked about."),
   ("ok. when are the estimated payments due",
    "Quarterly estimated payments are due around April 15, June 15, September 15 and January 15 of the next year. Because you started in April, your first payment would likely be the June one for your earnings so far. You can pay online at the IRS website, using the direct pay option, and record each payment. If you pay at least 90 percent of the current year's tax through the quarterly payments, you generally avoid a penalty, and a tax professional can help you calculate it."),
   ("should i hire an accountant or just use software",
    "For your first year of freelancing, a one-time consultation with an accountant can be worth the cost, especially to set up the right habits, even if you file with software afterwards. Many tax programs handle a simple freelance return well, and they walk you through each question. Consider an accountant if you have multiple income sources, plan to claim the home office, or just want reassurance. A good accountant often saves their fee in deductions you might have missed."),
   ("how much should i put aside from the 31,000",
    "A simple estimate is 25 to 30 percent of your net earnings, which would be about $7,750 to $9,300 on $31,000 before deductions. Your actual bill is probably lower once you subtract expenses such as the laptop and software, but it is safer to over-save at first. Move the money into a separate savings account each time a client pays you, so it is not tempting to spend. If you end up with extra, it becomes a nice cushion."),
   ("ok that helps a lot. thanks",
    "You are welcome. You have a clear plan now: track every payment, keep your receipts, save about a quarter of each invoice, pay the quarterly estimates and think about a short session with an accountant. Set recurring reminders on your phone for the four payment dates so none of them sneaks up. A tidy record and a steady habit will make next April far calmer."))
Q("taxes", "rec_user", "how much had been earned so far in the first year",
  "About $31,000 since you started freelancing as a graphic designer in April. Setting aside 25 to 30 percent of that comes to roughly $7,750 to $9,300 before deductions.",
  ref=("$31,000", "April"))
Q("taxes", "rec_asst", "which method did you mention for the home office deduction",
  "The simplified method, which allows $5 per square foot up to 300 square feet. A 100 square foot space would give you $500. The space has to be used regularly and only for your business to count.",
  ref=("$5 per square foot", "300 square feet"))
Q("taxes", "rec_none", "which tax software did you recommend",
  "I didn't name a specific program. I only said that many tax programs handle a simple freelance return well, and that a one-time session with an accountant can be worth it in the first year. Do you want to compare a couple of options?",
  absent="turbotax", ref=("accountant",))

# ------------------------------------------------------------------ teenage son
TL("caleb",
   ("my 15 year old son Caleb has been glued to his phone and his grades dropped from B's to C's this semester. i took the phone away last week and it turned into a huge fight",
    "A fight like that must be exhausting for both of you, and it tells me the phone is carrying a lot of weight for Caleb, probably friendships as well as entertainment. Taking it away outright often creates a power struggle that hides the real problem. It may help to separate two questions: what is behind the drop in grades, and what agreement about the phone would be fair. A calm conversation when nobody is angry usually goes much better than one in the middle of a fight. Do you know whether anything changed for him at school this term?"),
   ("not sure. he's gotten quiet and he skipped basketball tryouts which he usually loves",
    "Skipping tryouts for something he loves is worth noticing. It could be burnout, a conflict with friends, worry about being cut or a low mood, and a phone can be a way to hide from any of those. You do not need to figure it out alone. Try inviting him to do something low pressure together, like driving to get food or walking the dog, where he does not have to look at you. Side by side conversations are often easier for teenagers. Say what you have seen with care, and then listen."),
   ("what do i say to start it?",
    "Keep it gentle and specific. Something like, I noticed you skipped tryouts and you have seemed quiet lately, and I am not angry, I just want to know how you are doing. Then leave a long pause, because teenagers often need a moment before they speak. Avoid asking why with an edge, since it can feel like an accusation. If he says nothing is wrong, you can say, okay, I am here when you want to talk, and leave it there. The door staying open matters more than getting an answer that night."),
   ("ok. and the phone? i don't want to just give it back as if nothing happened",
    "You do not have to pretend. A fair approach is to give the phone back with a clear agreement that you make together, such as phones charging outside the bedroom at night and a time each evening for homework without it. Letting Caleb help shape the rules makes it more likely he will stick to them. Explain that the aim is sleep and focus, not punishment. Agree on a check-in after two weeks to see how it is working and adjust."),
   ("he's going to hate the bedroom rule",
    "He probably will at first, and it is fine for him to be annoyed while the rule still holds. Teens need more sleep than they get, and a phone in the room is the biggest thief of it. You can soften it by buying him a cheap alarm clock so he does not need the phone to wake up, and by following the rule yourself with your own phone. When everyone in the house does the same, it feels less like a punishment."),
   ("good point, i'm on mine a lot at night too. what about his grades",
    "Tackle one thing at a time. Ask him which class feels hardest, and see if his teacher can offer a quick plan or extra help. A short homework routine, for example thirty to forty-five minutes at the same time each day, with the phone out of reach, tends to bring grades up within a few weeks. Praise effort and specifics, like finishing the assignment early, instead of only results. If the quiet mood continues, talk with his school counselor or doctor, because a dip in mood is worth checking."),
   ("do you think i should be worried about his mood",
    "It deserves attention but not panic. Pay attention to how long it lasts and whether it affects sleep, appetite, friendships and things he normally enjoys. A few weeks of withdrawal after a rough patch is common, while a mood that stays low for more than two weeks, or any talk of feeling hopeless, is a reason to contact his doctor or a counselor soon. You know him best, and trusting your instincts here is sensible. Asking for professional help is a normal step."),
   ("thank you. i guess i needed a plan other than yelling",
    "That is a good place to be. You have a calm opening for the conversation, a fair phone agreement, a gentle way to work on grades and a clear sign to watch for in his mood. Choose a moment this weekend that feels relaxed, and keep your first aim small: to let Caleb know you are on his side. Even if he says little, your steady tone will register more than you expect."))
Q("caleb", "rec_user", "how old is the boy and what did he skip",
  "Caleb is 15, and he skipped basketball tryouts, which he usually loves. That, along with the quiet mood and the drop from B's to C's, is why the conversation matters more than the phone rule.",
  ref=("15", "tryouts"))
Q("caleb", "rec_asst", "what did you suggest i say to open the conversation",
  "You could open with a gentle observation that he skipped tryouts and has seemed quiet, say you are not angry, and ask how he is doing. Then leave a long pause. Doing it side by side, on a drive or a walk with the dog, often makes it easier for him.",
  ref=("skipped tryouts", "long pause"))
Q("caleb", "emo", "i just feel like i'm failing him",
  "You are not failing him. A parent who notices the quiet mood, the skipped tryouts and the grades, and who is looking for a better plan than yelling, is paying attention in the way Caleb needs. The phone fight was one rough week in a long relationship. A calm talk this weekend will do more good than you expect. Which part of the plan feels hardest to start?",
  ref=("tryouts", "yelling"))

# ------------------------------------------------------------------ a friend gone quiet
TL("noor",
   ("my close friend Noor hasn't replied to my texts in three weeks and i don't know what i did",
    "Three weeks of silence from a close friend is painful, and it is natural to search your memory for a reason. Before assuming you did something, remember that people go quiet for many reasons that have nothing to do with the friend: stress, a hard stretch at work, family problems, low energy. It would help to look at the last conversation you had and see whether anything felt off, but try not to turn it into a trial of yourself. Was there anything unusual in the way things ended, or did she just stop answering?"),
   ("she just stopped. last thing was me sending a photo from our trip and she hearted it",
    "A heart on a happy trip photo sounds friendly, not angry, which is a gentle sign. People who are upset with someone usually stop engaging in a more obvious way. Something else may be going on in her life that is using up her energy. You could send a single, low pressure message that asks for nothing in return, such as, I was thinking of you today and hope you are doing okay, no need to reply. That way you show you care without making her feel she owes you an answer."),
   ("i did that two weeks ago. nothing",
    "That is hard, and I am sorry it has gone this way. It still does not mean you did something wrong. Some people withdraw when they are overwhelmed, and a message can feel like one more task. You have shown that you care, so the next step might be to wait and give her space, or to try a different channel, like a short card or asking a mutual friend whether she is okay. Checking on her wellbeing is different from pressing for an answer."),
   ("we do have a mutual friend, Dina. should i ask her",
    "Asking Dina to share how Noor is doing can be a kind step, as long as you keep it simple and avoid gossip. You might say, I have not heard from Noor in a while and I am a bit worried, do you know if she is okay. This puts her wellbeing first and does not ask Dina to reveal secrets. If Dina knows something difficult, she may tell you what is appropriate. If she does not know, you will at least know you are not the only one in the dark."),
   ("ok i'll ask dina tomorrow. i'm just so sad about it",
    "You would be. A friendship that mattered is going through a silence you cannot explain, and not knowing is its own kind of grief. It is fine to feel sad while you wait. Be gentle with yourself in the meantime, and keep up the things that steady you, like seeing other friends, getting outside and sleeping. You can care about Noor and still look after yourself while the situation clears."),
   ("what if she's just done with the friendship",
    "That is a fear many people have in this position, and it is possible, but it is only one of several explanations. If it turns out she does want to step back, it will hurt, and it will also give you an answer you can grieve and move through, instead of a question that stays open. For now there is no evidence for it, and the heart on your photo points the other way. Try not to settle on the worst case before you have heard from her."),
   ("true. i guess i should wait for dina's answer",
    "That seems sensible. Ask Dina tomorrow, keep the message light and then let a few days pass. In the meantime, you do not need to send any other message to Noor. If you hear that she is struggling, a short note offering support without any pressure would be kind. If you hear nothing, you can send one more message in a month or so, simply saying that the door is open."),
   ("thanks for letting me think it through",
    "Any time. You have done this thoughtfully: one gentle message already sent, a plan to check with Dina, and patience in between. Whatever you hear, your care for Noor shows in the way you are handling it. Be kind to yourself tonight, and try to do something small that you enjoy."))
Q("noor", "rec_user", "who was the friend that stopped replying and how long ago",
  "Noor, about three weeks now. Her last sign of contact was a heart on the photo you sent from your trip, which points to a busy or overwhelmed friend more than an angry one.",
  ref=("Noor", "three weeks"))
Q("noor", "rec_asst", "what did you suggest i say to dina",
  "Something simple like, I have not heard from Noor in a while and I am a bit worried, do you know if she is okay. It puts Noor's wellbeing first and doesn't ask Dina to share any secrets. Do you plan to message or call her tomorrow?",
  ref=("Dina", "worried"))
Q("noor", "build", "okay so what is the plan now, briefly",
  "Ask Dina tomorrow with that light, worried-but-kind message, then let a few days pass without sending Noor any more messages. If you hear she is struggling, a short note offering support would be kind, and if you hear nothing, you could try once more in about a month. Meanwhile, look after yourself.",
  ref=("Dina", "month"))

# ==== FRAMEWORK (keep last) ==========================================================================
LEAK = re.compile(r"\b(tool|tools|receipt|wisp|as an ai|mcp|api|schema|untrusted|json|web results|search results|"
                  r"i searched|i looked up|can't look up|cannot look up|don't have access|haven't checked|"
                  r"i've saved|i have saved|i've noted|i have noted|i'll remember|i will remember|sam)\b", re.I)
def _none_ack():
    """distill/choose.py's NONE_ACK, read from its source (choose.py cannot be imported here: it puts its own `gen` first on
    sys.path), so the scripted rec_none finals, the SFT picker and the GRPO reward judge a "did not come up" the same way."""
    import ast
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "ask-lora-r4", "distill", "choose.py")
    for node in ast.parse(open(path).read()).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "NONE_ACK" for t in node.targets):
            return eval(compile(ast.Expression(node.value), path, "eval"), {"re": re})
    raise RuntimeError(f"NONE_ACK not found in {path}")


NONE_ACK = _none_ack()
NOT_SAID = re.compile(r"hasn't|has not|haven't|have not|hadn't|didn't|did not|not come up|not mentioned|never|"
                      r"no mention|not said|not yet|isn't|wasn't|yet\b", re.I)
TOK = re.compile(r"[A-Za-zÀ-ɏ0-9]+(?:['’][A-Za-z]+)*")
MAX_HISTORY_CHARS = 9000


def _sents(text):
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def _norm(s):
    return " ".join(re.sub(r"[^a-z0-9À-ɏ ]", " ", s.lower()).split())


def _vocab(text):
    out = set()
    for t in TOK.findall(text):
        out.add(t.lower())
        out.add(re.sub(r"['’]s$", "", t.lower()))
        if t.lower().endswith("s"):
            out.add(t.lower()[:-1])
    return out


def _ents(text):
    """Digits-led tokens and capitalized tokens that are not the first word of a sentence or clause."""
    out = set()
    for sent in re.split(r"(?<=[.!?])\s+|:\s+", text):
        for i, t in enumerate(TOK.findall(sent)):
            base = re.sub(r"['’]s$", "", t)
            if base[0].isdigit():
                out.add(base.lower())
            elif i > 0 and base[0].isupper() and base.split("'")[0].split("’")[0] != "I":
                out.add(base.lower())
    return out


def _voice(text, who, long=False):
    low = text.lower()
    n = len(_sents(text))
    assert 1 <= n <= (12 if long else 5), f"{who}: sentences={n}: {text!r}"
    assert not any(low.startswith(b) for b in BANNED_OPENERS), f"{who}: banned opener: {text!r}"
    assert not CLOSERS.search(text), f"{who}: ritual closer: {text!r}"
    assert not PERSONA.search(text), f"{who}: invented persona: {text!r}"
    assert not LEAK.search(text), f"{who}: machinery/name word {LEAK.search(text).group(0)!r}: {text!r}"
    assert not EMOJI.search(text), f"{who}: emoji: {text!r}"
    assert not re.search(r"[*#\n—–]", text), f"{who}: markup or dash: {text!r}"
    assert text.count("?") <= 1, f"{who}: more than one question: {text!r}"


def _check_q(q):
    """Raise AssertionError if scenario q breaks a rule this family teaches."""
    ex = THREADS[q["thread"]]
    cut = q["cut"] or len(ex)
    hist = ex[:cut]
    n = len(hist)
    kind, prompt, final = q["kind"], q["prompt"], q["final"]
    assert 3 <= n <= 10, f"history of {n} exchanges"
    htxt = "\n".join(u + "\n" + a for u, a in hist)
    # The host re-prompts for a tool when the prompt asks for "my own current data" (model_only.asks_personal_data),
    # which a recall from the conversation is not: write the prompt so the host accepts the direct reply.
    assert not asks_personal_data(prompt), f"host would force a tool for this prompt: {prompt!r}"
    assert not capability_question(prompt), f"host reads this as a capability question: {prompt!r}"
    assert len(htxt) + len(prompt) < MAX_HISTORY_CHARS, f"history {len(htxt)} chars"
    hlow = htxt.lower()
    flow = final.lower()
    assert prompt.lower() not in {u.lower() for u, _ in hist}, "prompt repeats an earlier user turn"
    assert 60 <= len(final) <= 650, f"final length {len(final)}"
    _voice(final, "final")
    long_thread = q["thread"] in LONG_THREADS
    for k, (u, a) in enumerate(hist):
        _voice(a, f"history[{k}]", long=long_thread)
        assert len(a) >= (250 if long_thread else 90), f"history[{k}] too curt: {a!r}"
        assert not EMOJI.search(u), "emoji in a user turn"
    # references: each must be in both the history and the final, exactly as the history has it
    for r in q["ref"]:
        assert r.lower() in hlow, f"ref {r!r} not in history"
        assert r.lower() in flow, f"ref {r!r} not in final"
    if kind in ("rec_user", "rec_asst", "rec_both"):
        assert q["ref"], "recall needs a ref"
        p = q["ref"][0].lower()
        where_u = [i for i, (u, a) in enumerate(hist) if p in u.lower()]
        where_a = [i for i, (u, a) in enumerate(hist) if p in a.lower()]
        if kind in ("rec_user", "rec_both"):
            assert where_u, f"{p!r} never said by the user"
        if kind in ("rec_asst", "rec_both"):
            assert where_a, f"{p!r} never said by the assistant"
        idx = sorted(set(where_u + where_a))
        assert idx[0] >= n - 10, f"{p!r} first appears {n - idx[0]} exchanges back"
        assert idx[0] <= n - 2, f"{p!r} only in the last exchange"
    if kind == "rec_none":
        assert q["absent"] and q["absent"].lower() not in hlow, f"absent {q['absent']!r} is in the history"
        assert NOT_SAID.search(final), "rec_none final must say it did not come up"
        assert NONE_ACK.search(final), f"rec_none final must match distill/choose.py NONE_ACK: {final!r}"
    if kind in ("emo", "build"):
        assert q["ref"], f"{kind} needs a ref to an earlier specific"
    if kind == "emo":
        assert len(final) >= 150, "emotional final too short"
    if kind == "close":
        assert len(final) <= 200 and "?" not in final, "a closer is one or two short sentences, no question"
    # no invented person/place/number
    extra = _ents(final) - _vocab(htxt + "\n" + prompt)
    if kind not in ("fact", "build"):
        assert not q["ok"], "ok is for fact and build turns only"
    extra -= set(q["ok"])
    assert not extra, f"final names things absent from the history and prompt: {sorted(extra)}"
    # never repeat an earlier assistant sentence or opening
    seen = {}
    for k, (u, a) in enumerate(hist):
        for s in _sents(a):
            ns = _norm(s)
            if len(ns.split()) >= 3:
                assert ns not in seen, f"history sentence repeats (turns {seen[ns]} and {k}): {s!r}"
                seen[ns] = k
    for s in _sents(final):
        ns = _norm(s)
        assert ns not in seen, f"final repeats an earlier assistant sentence: {s!r}"
    fopen = " ".join(_norm(final).split()[:4])
    for k, (u, a) in enumerate(hist):
        assert fopen != " ".join(_norm(a).split()[:4]), f"final opens like assistant turn {k}"


def _check_family():
    errs = []
    for q in QS:
        try:
            _check_q(q)
        except AssertionError as exc:
            errs.append(f"[{q['thread']}/{q['kind']}] {q['prompt']!r}: {exc}")
    assert not errs, "\n".join(errs)
    prompts = [q["prompt"].lower() for q in QS]
    assert len(set(prompts)) == len(prompts), "duplicate prompts"
    firsts: dict[str, list[str]] = {}
    for q in QS:
        firsts.setdefault(_first3(q["final"]), []).append(q["prompt"])
    crowded = {k: v for k, v in firsts.items() if len(v) > 2}
    assert not crowded, "openers used more than twice: " + "; ".join(f"{k!r} x{len(v)} {v}" for k, v in crowded.items())
    lens = sorted(len(q["final"]) for q in QS)
    med = lens[len(lens) // 2]
    full = len(QS) >= 120   # the length and question-rate targets apply to the finished family
    assert not full or 220 <= med <= 360, f"median final length {med}"
    qn = sum(q["final"].rstrip().endswith("?") for q in QS)
    assert not full or 0.30 <= qn / len(QS) <= 0.50, f"end-questions {qn}/{len(QS)}"
    for t in THREADS:
        assert any(q["thread"] == t for q in QS), f"thread {t} unused"
    return len(QS), med, qn


# ---- surface form -------------------------------------------------------------------------------------
# Real Ask input is not all lowercase: a thread is typed in one register (casual lowercase, a proper capitalised
# and punctuated sentence, or hurried with dropped apostrophes and a slip), and a few questions get a polite or
# hesitant lead-in. The transform runs after every content check, on the user turns only.
_LEADS = ("hey, ", "quick question, ", "sorry, ", "okay so ")
_QWORDS = re.compile(r"^(what|which|who|when|where|why|how|is|are|do|does|did|can|could|should|was|were|will)\b", re.I)


def _h(*parts):
    import hashlib
    return int(hashlib.md5("|".join(parts).encode()).hexdigest(), 16)


def _register(thread):
    r = _h("register", thread) % 10
    return "casual" if r < 5 else "proper" if r < 8 else "hurried"


# Words that are never names, whatever their case in a sentence.
_NOT_NAMES = set("a an the that this those these in on at of to for and but or then so if as is it its i you we they he she there "
                 "here what when where who which how why not no yes good ok okay well now also just really very too one two "
                 "three first second last next tonight today tomorrow lemon sour".split())


def _names(text, owner=""):
    """Capitalised proper nouns of a thread: a token that is capitalised in the middle of a sentence and is never
    written lowercase in `owner` (the assistant's own text, which is correctly cased). Without that test a
    quoted opener, a list item or a title put 'A', 'That' or 'In' in the map, and the proper register then
    capitalised every occurrence in the user's turns."""
    lower = {re.sub(r"['’]s$", "", t).lower() for t in TOK.findall(owner or text) if t[0].islower()}
    out = {}
    for sent in re.split(r"(?<=[.!?])\s+|:\s+", text):
        for i, t in enumerate(TOK.findall(sent)):
            base = re.sub(r"['’]s$", "", t)
            if (i > 0 and base[0].isupper() and base != "I" and base.lower() not in lower
                    and base.lower() not in _NOT_NAMES):
                out[base.lower()] = base
    return out


def _proper(text, names, question=False):
    t = re.sub(r"\bi\b", "I", text)
    t = re.sub(r"\b([A-Za-z]+)\b", lambda m: names.get(m.group(1).lower(), m.group(1)), t)
    t = re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), t)
    if t[-1] not in ".!?":
        t += "?" if question else "."
    return t


def _hurried(text, protect):
    t = re.sub(r"\b(i|you|we|they|it|that|what|who|how|where|there|let|don|didn|isn|wasn|can|won)['’]"
               r"(m|ll|re|s|d|ve|t)\b", lambda m: m.group(1) + m.group(2), text)
    words = [w for w in re.findall(r"[a-z]{6,}", t) if w not in protect]
    if words and _h("typo", text) % 3 == 0:
        w = words[_h("w", text) % len(words)]
        k = 1 + _h("k", text) % (len(w) - 3)
        t = t.replace(w, w[:k] + w[k + 1] + w[k] + w[k + 2:], 1)
    return t


def _surface(thread, text, names, protect, *, prompt=False):
    reg = _register(thread)
    question = bool(_QWORDS.match(text))
    if prompt and question and _h("lead", thread, text) % 100 < 14:
        text = _LEADS[_h("lead2", text) % len(_LEADS)] + text
    if reg == "proper":
        return _proper(text, names, question)
    if reg == "hurried":
        return _hurried(text, protect)
    return text


_SLIP_SKIP = {"I"}


def _mid_caps(text):
    out = []
    for sent in re.split(r"(?<=[.!?])\s+|:\s+", text):
        for i, t in enumerate(TOK.findall(sent)):
            base = re.sub(r"['’]s$", "", t)
            if i > 0 and base not in _SLIP_SKIP and base[0].isupper():
                out.append(base)
    return out


def _cap_slips(orig, text, owner):
    """Mid-sentence capitalised words that the register added to a user turn although they are common words (the
    assistant writes them lowercase) and the person never capitalised them."""
    common = {t.lower() for t in TOK.findall(owner) if t[0].islower()} | _NOT_NAMES
    typed = {w.lower() for w in _mid_caps(orig)}
    return [w for w in _mid_caps(text) if w.lower() in common and w.lower() not in typed]


CAP = 3   # rows per conversation: a thread feeds at most this many scenarios


def _apply_cap():
    by: dict[str, list[dict]] = {}
    for q in QS:
        by.setdefault(q["thread"], []).append(q)
    keep: list[dict] = []
    for qs in by.values():
        picked: list[dict] = []

        def take(pred):
            for q in qs:
                if len(picked) < CAP and all(q is not p for p in picked) and pred(q):
                    picked.append(q)
                    return

        take(lambda q: q["kind"] in ("rec_user", "rec_both"))
        take(lambda q: q["kind"] in ("rec_asst", "rec_both", "rec_none"))
        take(lambda q: q["kind"] in ("build", "emo", "fact", "close"))
        for _ in range(CAP):
            take(lambda q: True)
        keep += picked
    kept = [q for q in QS if any(q is k for k in keep)]
    # Spread the kinds: consecutive scenarios rarely share a kind (the step thoughts come from a few templates per
    # kind, and build.fix_adjacent re-phrases a thought only when its neighbours differ). refs.py maps scenario k to
    # QS[k], so the order is fixed here, once, at import.
    import random
    from scen_r4_session_recall import _interleave
    QS[:] = _interleave(kept, lambda q: q["kind"], random.Random("session-chat-order"))


_apply_cap()


RECALL_KINDS = frozenset({"rec_user", "rec_asst", "rec_both"})   # = DIST/choose.py RECALL_KINDS


def fam_session_chat_r4(rng):
    _check_family()
    out, bad = [], []
    for q in QS:
        ex = THREADS[q["thread"]]
        base = ex[:q["cut"] or len(ex)]
        owner = "\n".join(a for u, a in base) + "\n" + q["final"]
        names = _names("\n".join(u + "\n" + a for u, a in base) + "\n" + q["final"], owner)
        protect = {t.lower() for r in q["ref"] for t in TOK.findall(r)} | set(names)
        hist = [turn(_surface(q["thread"], u, names, protect), a) for u, a in base]
        prompt = _surface(q["thread"], q["prompt"], names, protect, prompt=True)
        slips = [(t, w) for o, t in zip([u for u, _ in base] + [q["prompt"]], [h["user"] for h in hist] + [prompt])
                 for w in _cap_slips(o, t, owner)]
        assert not slips, f"[{q['thread']}] capitalised common word in a user turn: {slips[:3]}"
        now, w = setup(rng)
        w.history = hist
        sc = Scenario(FAMILY, prompt, now, w, [Final(q["final"])],
                      f"session chat ({q['kind']}): {len(hist)} earlier exchanges, answered from the conversation "
                      f"or general knowledge, no tool")
        sc.history = hist
        sc.approve = None
        sc.expect_cards = []
        sc.must_see = []
        # RL-SCOPE C6/C7: the thread is the split unit; the recall labels follow the question KIND exactly (a
        # non-empty `ref` is thread context on build/emo/fact/close/rec_none rows, never a recall target)
        sc.thread, sc.qkind = q["thread"], q["kind"]
        if q["kind"] in RECALL_KINDS:
            assert q["ref"], f"[{q['thread']}] recall question without a ref: {q['prompt']!r}"
            sc.recall_refs = tuple(q["ref"])
        elif q["kind"] == "rec_none":
            assert q["absent"], f"[{q['thread']}] never-said question without absent: {q['prompt']!r}"   # ok may be empty
            sc.recall_absent, sc.recall_ok = q["absent"], tuple(q["ok"])
        ok, why = F._check(sc)
        if not ok:
            bad.append(f"[{q['thread']}/{q['kind']}] {prompt!r}: {why}")
            continue
        out.append(sc)
    assert not bad, "dry-run failures:\n" + "\n".join(bad)
    return out


FAMILIES_R4 = {FAMILY: fam_session_chat_r4}


if __name__ == "__main__":
    print(_check_family())
