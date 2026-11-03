"""Round-4 family ``chat_direct_r4``: casual chat answered directly, from general knowledge.

Why. r3 turned some casual chat into lookups that failed ("I couldn't find any web results for cozy
movies", "I don't have your home city yet, so I can't suggest a dinner"). Every turn here is the
direct reply: a recommendation, an opinion, a small game or a quick fact, answered with no tool call,
no receipt phrasing and no mention of how the answer was produced.

Voice (chat_voice_r3's, made fuller, because r3's chat replies ran short):
- gentle and warm, not upbeat; the user is "you"; no emoji; no ritual closers.
- 2-4 sentences, median about 250-350 characters, one concrete detail each: a named title, a vivid
  reason or a small story beat.
- recommendations name 2-3 specific picks, each with a one-line reason.
- about 40% end in a natural follow-up question or invitation, worded to the topic.
- games keep playing: make the move, react, offer the next round.

``_check`` enforces the mechanical rules at build time.
"""
from __future__ import annotations

import re

from harness import Final
from scen import Scenario, setup
from scen_r3_chat import BANNED_OPENERS, CLOSERS, EMOJI, LEAKS, PERSONA, _first3
import scen_r3_chat as R3

FAMILY = "chat_direct_r4"
WRITE_ALLOW_R4 = {FAMILY: frozenset()}

E: list[tuple[str, str, str]] = []


def A(cat, prompt, reply):
    E.append((cat, prompt, reply))


# ------------------------------------------------------------------ movies
A("rec_movie", "need a soft landing kind of movie tonight, nothing tense",
  "Paddington 2 is the kindest film I can think of: its villain is a vain, theatrical actor, and Paddington wins over an entire prison with marmalade. If you want something quieter, My Neighbor Totoro is 86 minutes of rain, soot sprites and one very large cat. Which kind of tired are you tonight, giggly or sleepy?")
A("rec_movie", "lazy sunday, want a feel-good movie that isnt a rom com",
  "Chef is a lovely one: a burned-out cook quits his job, buys a food truck and rebuilds his life one Cubano sandwich at a time. It will make you hungry, so have a snack ready. The Princess Bride is the other safe bet, since it is funny, sweet and full of sword fights in equal parts.")
A("rec_movie", "movie with a proper twist ending pls",
  "Parasite is the one I would push, but go in knowing as little as possible, because the second half turns into something else entirely. For a lighter ride, Knives Out is a cozy whodunit with Daniel Craig doing a delightfully silly accent. If you somehow have not seen The Sixth Sense yet, that is the classic to save for another night.")
A("rec_movie", "movie for grandma night, shes 80 and loves old musicals",
  "Singin' in the Rain is the obvious joy, and Gene Kelly splashing through puddles is one of the best scenes ever filmed. If she wants a story to sink into, The Sound of Music makes for a long, warm afternoon with songs everyone knows. Put the subtitles on if her hearing is not what it was, and the songs will still carry everything.")
A("rec_movie", "date night film, we both like funny but not dumb",
  "Crazy, Stupid, Love has real laughs and a surprisingly sweet middle, plus a famously chaotic third act. When Harry Met Sally is the older classic, mostly because the deli scene still works after all these years. Either one gives you something to talk about on the walk home.")
A("rec_movie", "animated movies for adults, any?",
  "Spirited Away is the first one I would try, a bathhouse for spirits with a strange, gentle logic that works at any age. Persepolis is the serious pick, a black-and-white memoir about growing up in Iran. For pure fun, Into the Spider-Verse looks like nothing else ever made. Pick by mood: Spirited Away for wonder, Persepolis for something weightier.")
A("rec_movie", "got like 90 minutes free, short good movie?",
  "My Neighbor Totoro runs about 86 minutes and manages to feel like a whole summer. Frances Ha is around the same length, shot in black and white, funny, and a little wistful about being young and broke in New York. Pick Totoro for comfort and Frances Ha for company.")
A("rec_movie", "horror but not gory?",
  "The Others is a slow, candlelit ghost story with almost no blood, and Nicole Kidman is wonderful in it. A Quiet Place is tense without being gory, because the whole danger is that nobody can make a sound. Keep a blanket close for either one, and maybe a second person on the sofa.")
A("rec_movie", "a film to put on when i cant sleep",
  "Something gentle and familiar works best, so Kiki's Delivery Service is a good bet: a young witch starts a delivery business in a seaside town, and nothing in it is truly frightening. Lost in Translation is the quiet late-night choice, all neon and jet lag. Is it restlessness or worry keeping you up?")
A("rec_movie", "movie night with six friends, what wont start an argument",
  "Hunt for the Wilderpeople is a funny, big-hearted New Zealand road story that wins over nearly everyone. The Grand Budapest Hotel is a colorful pick if your group likes dry humor. If someone insists on action, Mad Max: Fury Road is loud enough to keep the whole sofa awake.")

# ------------------------------------------------------------------ books
A("rec_book", "book recs for a long train ride",
  "Piranesi is short and strange, set in an endless house of tides and statues, and it is easy to finish before your stop. If you want more momentum, The Thursday Murder Club is a cozy mystery set in a retirement village, with sharp humor and a soft heart. Do you want a page-turner or something to sink into?")
A("rec_book", "havent finished a book in months, best way to ease back into reading",
  "Pick something short and hard to put down, like Convenience Store Woman, which is about 160 pages of dry, strange humor. Of Mice and Men is another quick one that hits hard. Finishing a book matters more than which one it is, because that feeling makes you want the next.")
A("rec_book", "fantasy series for someone who loved the hobbit",
  "A Wizard of Earthsea is a calm, wise place to start: a young mage learns the cost of power across a chain of islands. The Name of the Wind is bigger and more lyrical, but the trilogy is still unfinished, so know that going in. What did you love most in The Hobbit, the adventure or the cozy bits?")
A("rec_book", "nonfiction that reads like a thriller",
  "Bad Blood by John Carreyrou tells the Theranos story with the pace of a novel, and the villain is real. The Immortal Life of Henrietta Lacks is more moving than tense, following a woman whose cells changed medicine without her family knowing. Both are hard to put down once you start.")
A("rec_book", "loved the martian, whats a good next read",
  "Project Hail Mary, by the same author, has the same cheerful problem-solving, only with an alien friend in the mix. Children of Time is bigger and stranger, about spiders evolving a civilization over thousands of years. Both are best read with a free weekend, because stopping halfway is hard.")
A("rec_book", "poetry for someone who says they hate poetry",
  "Mary Oliver is a gentle way in: her poems are short, plain-spoken and mostly about paying attention to a heron or a field, and Wild Geese is the usual first one. Billy Collins is another easy door, with poems that are funny before they turn quiet. Read one or two aloud, because poems tend to open up when you hear them.")
A("rec_book", "gift book for my friend who loves cooking",
  "Salt Fat Acid Heat by Samin Nosrat teaches why food works, so it makes people better cooks without feeling like homework, and the illustrations are lovely. Ottolenghi's Simple is a brighter pick if she likes vegetable-heavy dishes. A note on the first page turns either into something personal.")
A("rec_book", "something comforting to read, kind of sad kind of hopeful",
  "A Man Called Ove is about a grumpy widower whose quiet routine gets crowded by new neighbors, and it is both sad and funny the way real life is. Anxious People starts with a botched robbery that becomes a hostage situation at an apartment viewing, and turns out tender. Do you want to cry a little or just feel held?")
A("rec_book", "short story recs, i only get like 10 min at a time",
  "Short stories fit that rhythm well. Ted Chiang's Stories of Your Life and Others gives you one clean, strange idea per sitting, and Jhumpa Lahiri's Interpreter of Maladies is quiet and observant, about families caught between two places. Each story ends cleanly, so you never have to remember where you stopped.")

# ------------------------------------------------------------------ food
A("rec_food", "dinner idea? i have rice, eggs and some spinach",
  "A pan of egg fried rice fits that perfectly. Use cold rice if you can, scramble the eggs first, then wilt the spinach in at the end with a splash of soy sauce and a few drops of sesame oil if there is any. It takes about ten minutes and tastes like you tried harder than you did.")
A("rec_food", "dinner suggestion for tonight, something warm",
  "Tomato soup with a grilled cheese is a soft, comforting pairing that needs almost no skill. If you would rather use one pot, spaghetti with garlic, olive oil and chili flakes is ready in fifteen minutes and feels fancier than it is. Both double easily if friends turn up hungry.")
A("rec_food", "what can i have for breakfast thats not cereal",
  "Overnight oats make the lowest-effort breakfast: stir oats, milk and a spoon of yogurt in a jar tonight, and add fruit in the morning. If you have five minutes, soft scrambled eggs on buttered toast are hard to beat. Do you like breakfast sweet or savory?")
A("rec_food", "snacks for pulling an all nighter",
  "Go for things that release energy slowly: apple slices with peanut butter, a handful of almonds, or cheese with whole-grain crackers. Skip the big sugar hit, because the crash around 3 AM is worse than the tiredness. Drink water in between, since thirst often passes for sleepiness.")
A("rec_food", "cheap meal for 4 people, ideas?",
  "A big pot of chili stretches a long way, and canned beans and tomatoes keep it cheap. Black bean tacos are another budget winner, especially with rice and a pile of toppings so everyone builds their own. Keep the hot sauce on the table so everyone can set their own heat.")
A("rec_food", "dessert with no oven",
  "Frozen banana blended until smooth turns into soft-serve with no added sugar, and a spoon of peanut butter makes it taste like a sundae. Or press oats, melted chocolate and a pinch of salt into a pan for no-bake bars. Which sounds closer to what you are craving, creamy or chewy?")
A("rec_food", "cook something impressive for a first date at home",
  "Make a simple tomato pasta with good olive oil and fresh basil, plus a sharp green salad: it looks thoughtful and is hard to ruin. Finish with decent vanilla ice cream topped with olive oil and sea salt, which sounds odd and tastes wonderful. Candles are optional, but they help.")
A("rec_food", "veggie dinner meat lovers wont complain about",
  "Mushroom stroganoff over egg noodles is rich and savory enough to win over most carnivores. Chickpea curry with rice is another reliable pick, especially with a squeeze of lemon at the end. A sprinkle of smoked paprika adds the savory depth they usually miss.")
A("rec_food", "im starving and its midnight, quick",
  "Make a cheese quesadilla in a dry pan: two minutes a side, and it is crisp, melty and done. If you have an egg, fry it and slide it onto buttered toast with a pinch of pepper. Either way you will be eating in under five minutes, which is the right speed for midnight.")
A("rec_food", "what to do with overripe bananas",
  "Banana bread is the classic, and the riper the bananas, the sweeter it gets, because the brown spots mean the starch has turned to sugar. If you do not want to bake, peel and freeze them for smoothies, or mash one into pancake batter. Walnuts or a handful of chocolate chips make it feel like a proper treat.")
A("rec_food", "meal prep for the week, small student fridge",
  "One big pot of lentil soup holds up for days and freezes well. Roast a tray of vegetables and cook a batch of rice, then mix and match them with an egg or canned tuna for different bowls through the week. Small, stackable containers fit neatly into a cramped fridge.")

# ------------------------------------------------------------------ music
A("rec_music", "music for studying that wont distract me",
  "Instrumental or ambient music works best, because lyrics compete with the words you are reading. Brian Eno's Music for Airports is a calm classic, and Nils Frahm's quiet piano albums feel like a warm room. Does lo-fi usually help you focus, or does it make you drift?")
A("rec_music", "good listening for a long drive?",
  "Podcasts with a story make hours disappear: Criminal for odd true tales, or This American Life for people you will think about afterward. If you want songs, an album you know by heart is lovely on a long road because you can sing every word. What is the drive like, mountains or flat highway?")
A("rec_music", "need a workout playlist, any tips",
  "Aim for songs around 160 beats per minute for running, which matches a natural stride. Upbeat pop-punk and driving electronic music often land right there. Save one big song you love for the last mile, so you have a reason to finish strong.")
A("rec_music", "something to fall asleep to",
  "Slow piano at a low volume works nicely, or just steady rain sounds. Max Richter's Sleep was composed for exactly this, and it runs around eight hours so it will not end on you. Set a sleep timer so it fades out once you have drifted off.")
A("rec_music", "jazz for a total beginner",
  "Kind of Blue by Miles Davis is the usual doorway, relaxed and melodic enough to leave on in the background. Dave Brubeck's Time Out has Take Five, a catchy tune in an odd rhythm that hooks almost everyone. Start with one album and let it play through in an evening.")
A("rec_music", "getting into classical music, where to begin",
  "Start with pieces you half-know already: Clair de Lune, Vivaldi's Four Seasons, Pachelbel's Canon. Then try the second movement of Beethoven's Seventh Symphony, which builds slowly and gets under your skin. Is it something to relax with, or something to listen to closely?")
A("rec_music", "music to cook dinner to",
  "Something with warmth and rhythm helps. The Getz and Gilberto bossa nova record is made for a kitchen, and old Motown is hard to stand still to. Khruangbin's groovy instrumentals also work well. Do you cook quietly, or sing at the stove?")
A("rec_music", "give me an album for a rainy morning",
  "Bon Iver's For Emma, Forever Ago fits a gray window perfectly: sparse, tender and recorded in a cabin in the Wisconsin woods. For something instrumental, Tycho's Dive is soft and spacious. Make the tea first, then press play.")

# ------------------------------------------------------------------ other recommendations
A("rec_other", "a hobby i can start with no money",
  "Sketching with a pencil and scrap paper is free and calming, and you improve fast in the first month. Walking with your phone to photograph doors, shadows or odd signs is another zero-cost one that changes how you see your neighborhood. Do you want something quiet or something that gets you outside?")
A("rec_other", "board game for four, easy to learn",
  "Codenames takes about two minutes to explain and gets loud and funny quickly, since it is all word clues. Ticket to Ride is a bit slower, but the rules are simple and the train routes are satisfying. Both work well with a bowl of snacks in the middle of the table.")
A("rec_other", "im bored. whole afternoon free no plans",
  "Pick one small thing you have been putting off, like clearing a single drawer, and then reward yourself with a long walk and a playlist. Or bake something simple, because the house will smell good for hours. What kind of bored is this, restless or sleepy?")
A("rec_other", "gift idea for my mom, she gardens",
  "A pair of good pruning shears is something she will use every season, and think of you each time. A sturdy kneeling pad is a nice extra, or seeds for a flower she has never grown before. If she grows tomatoes, a pack of heirloom seeds is a lovely small extra.")
A("rec_other", "game to play on a chill evening",
  "Stardew Valley is a gentle farm life where nothing is urgent and you can stop whenever you like. A Short Hike is a tiny, lovely game about climbing a mountain, and it fits neatly into one evening. Do you prefer building things or exploring?")
A("rec_other", "comedy series i can binge",
  "Parks and Recreation is warm, quick and gets better after its short first season. Schitt's Creek is another comfortable one, about a rich family learning to like each other in a small town. Do you like sharp jokes or soft ones?")
A("rec_other", "any podcast for curious people",
  "Radiolab turns science questions into stories, and 99% Invisible does the same for the hidden design of everyday things. Planet Money is great if you have ever wondered why something costs what it does. Which sounds more like you, science or everyday mysteries?")
A("rec_other", "ideas for a weekend project",
  "Build something small with your hands, like a simple bookshelf or a herb box for the windowsill. Or learn one song start to finish on an instrument you already own. Something with a clear finish line makes Sunday evening feel good. What do you have lying around the house?")

# ------------------------------------------------------------------ opinions
A("opinion", "stairs or elevator, whats your pick",
  "Stairs, narrowly, since a few flights a day is the cheapest exercise there is. The elevator wins when you are carrying groceries or running late, which seems fair. My line is drawn at about ten floors, after which everyone deserves a lift.")
A("opinion", "do you like rainy days",
  "In principle, yes: gray light, the sound on the window, and an excuse to stay in with something warm. The rainy commute is a different matter, and I would happily skip that part. Are you a rain person, or do you wait for the sun?")
A("opinion", "text or call, which is better",
  "Text for logistics and a call for anything with feeling in it, since tone is hard to read in writing. A two-minute call can settle what a dozen messages never will. The trick is knowing which kind of conversation you are having, and a quick text asking if now is a good time to call is a polite bridge.")
A("opinion", "crunchy or smooth peanut butter",
  "Crunchy, because the texture makes a plain sandwich more interesting and gives toast something to say. Smooth is better for sauces and for spreading thin on a banana, so I see its place. It is one of those small questions that quietly reveals a lot. Which team are you on?")
A("opinion", "book first or film first when theres an adaptation",
  "Usually the book first, because it lets your imagination build the world before someone else's version appears. The exception is a dense or sprawling story, where seeing the film first can make the characters easier to keep straight. Either way, the version you meet first tends to become the one you love.")
A("opinion", "is it okay to eat dessert first",
  "Now and then, absolutely. The ice cream is melting and the plan is flexible, and a meal that starts happy tends to end that way too. Just keep the actual dinner coming afterward so the evening does not turn into only sugar.")
A("opinion", "which season has the best food",
  "Autumn, I would say: squash, apples, soup and warm bread all show up at once. Summer is a close second, for a ripe peach eaten over the sink with juice running down your wrist. What is the one dish that makes a season for you?")
A("opinion", "are standing desks worth it",
  "They help if you change positions through the day, since standing still for hours tires you in a different way. A decent chair and a short walk every hour matter just as much as the desk. A cheap riser on a normal desk is a good way to test the idea first.")
A("opinion", "learning two languages at once, good idea?",
  "It works if the two are different enough not to blur, like Spanish and Japanese, rather than Spanish and Italian. Short daily sessions for each beat a long weekend cram, and it helps to link each language to a different time or place. Which two are you eyeing?")
A("opinion", "is it weird that i kind of like doing chores",
  "Not weird at all. Chores have a clear start and finish, which is rare in most of life, and a clean counter gives an instant sense of progress. Plenty of people feel the same way about folding laundry or washing dishes. Which one is your favorite?")
A("opinion", "pen and paper or notes app",
  "Paper for thinking, an app for keeping. Writing by hand slows you down enough to choose your words, while the app is better for searching and finding things months later. Many people end up using both, with a notebook on the desk and the app in the pocket.")

# ------------------------------------------------------------------ games
A("rps", "rock paper scissors, i'll throw scissors",
  "Rock, which crushes your scissors, so that round goes to me. Scissors is a classic opening throw, and rock is a quiet answer to it. Throw again whenever you are ready.")
A("rps", "rock paper scissors? you throw first",
  "Paper, laid flat and confident. Now you get to answer knowing my move, which seems a little unfair, so I will trust you to play honestly. Win or lose, we go again after this one.")
A("rps", "best of 5 rps, im opening with paper",
  "Scissors, and they cut your paper in half, so I take the first round. That is one to nothing, with four rounds left to turn it around. Throw your second whenever you like, and I will try not to gloat.")
A("rps", "rock! you go",
  "Paper, which wraps your rock neatly, so that round is mine. I am curious whether you will stick with rock or switch next time. Throw again when you are ready.")
A("rps", "scissors! go",
  "Paper this time, which means your scissors cut me cleanly and you take the round. That was a good throw, and I can feel the tide turning against me. Shall we make it best of three, or is a winner's exit allowed?")
A("wyr", "would you rather have unlimited pizza or unlimited tacos forever",
  "Tacos, because they come in a hundred forms: fish, al pastor, potato, even breakfast. Pizza is a real contender, but it gets heavy by day three. Which would you pick, and which topping could you never give up?")
A("wyr", "would you rather be ten minutes early or ten minutes late to everything",
  "Ten minutes early, since waiting is far kinder than rushing: you get a coffee, a quiet breath and a look around. Being late spares you the waiting but costs you every calm opening moment, and apologizing gets old. A book in the bag turns the spare minutes into a small pleasure. Which camp are you in?")
A("wyr", "would you rather have a personal chef or a personal driver",
  "A personal chef, since you eat three times a day and drive far less than that, and every meal would be a small event. A driver would give back your commute hours, though, which is a real luxury on bad-traffic days. Which one takes more of your time, the kitchen or the road?")
A("wyr", "would you rather live in a treehouse or on a houseboat",
  "A houseboat, for the slow rocking at night and the morning coffee on a deck with water all around. A treehouse wins on childhood magic, but the rope ladder gets old when you are carrying groceries. Either way, the neighbors would be ducks or squirrels. Which one would you decorate first?")
A("riddle", "hit me with a riddle",
  "Here is one: what can travel around the world while staying in a corner? Think of something small that you would stick on an envelope. Take your time, and tell me your guess when you have one.")
A("riddle", "riddle please, a tricky one",
  "The more of me there is, the less you can see. I fill a room when the lights go out and vanish when they come back. What am I? Give it a go, and I will tell you if you are warm.")
A("riddle", "riddle for you: what has a neck but no head",
  "Is it a bottle? A shirt works too, since both have a neck and no head, but the bottle is the classic answer. Want one back from me, maybe a harder one?")
A("riddle", "riddle: what has to be broken before you can use it",
  "An egg, I think, since you have to crack it before cooking or baking with it. It is a neat riddle because the answer is probably in your kitchen right now. Do you have another one for me?")
A("game", "can we play a guessing game, i have an animal in mind",
  "Let us do it. First question: is it a mammal? Answer with yes or no, and I will narrow it down from there, aiming to guess it in under twenty questions. Take as long as you need to settle on one.")
A("game", "two truths and a lie, you go first",
  "Here are my three: octopuses have three hearts, honey stored in a sealed jar can last for thousands of years, and bananas grow on trees. One of those is a lie. Which would you pick, and why?")
A("game", "word chain game? start with river",
  "Rainbow. The last letter of my word is w, so your word has to start with w. No repeats allowed, and I will try to keep the chain interesting for as long as it lasts.")
A("game", "give me a trivia question",
  "Quick trivia for you: which planet in our solar system has the shortest day, rotating once in about ten hours? Take a guess, and I will tell you whether you are close. Hint: it is the biggest one.")

# ------------------------------------------------------------------ quick knowledge
A("fact", "why do dogs wag their tails",
  "A wag is a way of communicating, and the details matter: studies suggest a wag leaning to the right goes with positive feelings and one leaning to the left with wariness. A loose, wide swing is friendly, while a stiff, high, quick one means alert. Do you have a dog at home?")
A("fact", "which mountain is tallest if you measure base to peak",
  "Measured from base to summit, it is Mauna Kea in Hawaii, at over 10,000 meters, because most of it sits underwater. Everest is the highest above sea level, at about 8,849 meters, so both answers are true depending on where you start counting. Mauna Kea only rises about 4,200 meters above the waves.")
A("fact", "do babies have more bones than adults",
  "They do: babies are born with around 300 bones, and many of them fuse together as the child grows, which is why the skull and the lower spine end up with fewer separate pieces. An adult has 206 in total. The hands and feet alone hold more than half of them.")
A("fact", "why is the sea salty but rivers arent",
  "Rain slowly wears minerals out of rock, and rivers carry them to the sea in tiny amounts. When sea water evaporates, the salt stays behind. Rivers keep flowing out, so salt never builds up in them, but the ocean has no outlet, and over millions of years it adds up.")
A("fact", "crocodile vs alligator, whats the difference",
  "Alligators have a wide, rounded snout, while crocodiles have a narrower, pointed one. A closed crocodile mouth shows its lower teeth in a toothy grin, but an alligator's upper jaw hides them. Alligators also prefer fresh water, while crocodiles tolerate salt.")
A("fact", "is a banana a berry",
  "Botanically, yes: a berry is a fleshy fruit with seeds grown from a single flower, and a banana qualifies. Strawberries, meanwhile, are not berries at all. Botanists and supermarkets rarely agree, which makes it a good fact to bring to a dinner.")
A("fact", "what does ephemeral mean",
  "It describes something that lasts only a short time, like a soap bubble, a sunset or a snowflake on a warm sleeve. The word comes from a Greek term meaning lasting a single day, which is a lovely origin for it. Do you have something ephemeral in mind?")
A("fact", "how do vaccines work, simple version",
  "Think of a vaccine as a rehearsal: it shows your immune system a harmless piece or copy of a germ, so your body learns what to look for. When the real germ turns up later, your immune system recognizes it and responds fast, often before you feel sick. It works like a practice drill for your defenses.")
A("fact", "which artist made the mona lisa and where can you see it",
  "It was Leonardo da Vinci, who started it around 1503, and he kept working on it for years afterward. It hangs in the Louvre in Paris, behind glass, and it is smaller than most people expect, roughly 77 by 53 centimeters. Have you seen it in person?")
A("fact", "capital of australia, i always say sydney",
  "Canberra. Sydney is the biggest city, so it is the usual guess, but Canberra was built from scratch as a compromise because Sydney and Melbourne each wanted to be the capital. You are in good company with that mix-up.")
A("fact", "why are sunsets red",
  "Near sunset, sunlight travels through much more air to reach you, and the blue light gets scattered away along the way. What is left are the longer wavelengths, the oranges and reds. Dust and clouds can make the colors richer, which is why some evenings look better than others.")
A("fact", "why does the moon change shape",
  "The moon is always half lit by the sun, and what changes is how much of that lit half faces us as it circles Earth every month. At new moon the lit side faces away, and at full moon we see all of it. A thin crescent is just a sliver of that sunlit half coming into view. Do you track it, or only notice when it is big?")
A("fact", "what causes the northern lights",
  "Charged particles from the sun hit gases high in Earth's atmosphere, and the gases glow. Oxygen gives green and sometimes red, while nitrogen adds blue and purple. They are strongest near the poles because the planet's magnetic field funnels the particles there.")
A("fact", "what causes hiccups",
  "A hiccup is a sudden spasm of the diaphragm, followed by the vocal cords snapping shut, which makes the sound. Eating fast, fizzy drinks or sudden excitement can set it off. Holding your breath for a few seconds works for many people, though nobody fully knows why.")


# ------------------------------------------------------------------ self-check and family
def _tri(text):
    t = re.sub(r"[^a-z0-9 ]", "", text.lower())
    return {t[i:i + 3] for i in range(len(t) - 2)}


def _jacc(a, b):
    x, y = _tri(a), _tri(b)
    return len(x & y) / max(1, len(x | y))


def _check():
    r3_first = {_first3(r[2]) for r in R3.E}
    r3_prompts = [r[1] for r in R3.E]
    firsts, prompts = {}, set()
    q_n = 0
    lens = []
    for cat, prompt, reply in E:
        low = reply.lower()
        n_sent = len(re.findall(r"[.!?](?:\s|$)", reply))
        assert 1 <= n_sent <= 5, f"sentences={n_sent}: {reply!r}"
        assert not any(low.startswith(b) for b in BANNED_OPENERS), f"banned opener: {reply!r}"
        assert not CLOSERS.search(reply), f"ritual closer: {reply!r}"
        assert not PERSONA.search(reply), f"invented persona history: {reply!r}"
        assert not LEAKS.search(reply), f"machinery/name word: {reply!r}"
        assert "*" not in reply and "\n" not in reply and "#" not in reply, f"markup: {reply!r}"
        assert not EMOJI.search(reply), f"emoji: {reply!r}"
        k = _first3(reply)
        assert k not in firsts and k not in r3_first, f"shared opener {k!r}: {reply!r}"
        firsts[k] = reply
        assert prompt.lower() not in prompts, f"duplicate prompt {prompt!r}"
        prompts.add(prompt.lower())
        assert all(_jacc(prompt, p) < 0.6 for p in r3_prompts), f"prompt close to a chat_voice_r3 prompt: {prompt!r}"
        q_n += reply.rstrip().endswith("?")
        lens.append(len(reply))
    n = len(E)
    lens.sort()
    med = lens[n // 2]
    assert 250 <= med <= 360, f"median reply length {med}"
    assert 0.35 <= q_n / n <= 0.45, f"end-questions {q_n}/{n}"
    assert 80 <= n <= 100, f"{n} replies"
    return n, med, q_n


def fam_chat_direct_r4(rng):
    _check()
    out = []
    for cat, prompt, reply in E:
        now, w = setup(rng)
        out.append(Scenario(FAMILY, prompt, now, w, [Final(reply)],
                            f"casual chat ({cat}): direct reply from general knowledge, no tool, no lookup"))
    return out


FAMILIES_R4 = {FAMILY: fam_chat_direct_r4}
