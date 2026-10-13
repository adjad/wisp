"""Chat-with-no-tool families (category 'chat'). The model answers directly: no get_tool_schemas, no stub, no tool call."""

FAIL = ("tool_on_chat", "acted_on_chat")


def register(G):
    def qa(r, c, now, bank, hist_bank=False):
        i = r.randrange(len(bank))
        item = bank[i]
        if hist_bank:
            q0, a0, q, a = item
            c.hist("user", q0).hist("assistant", a0)
        else:
            q, a = item
        c.user(G.messy(r, q))
        c.final(a)
        return {"template": i, "slots": {}}

    def reg(fid, bank, weight, split="train", failure=FAIL, hist=False):
        @G.family("chat", fid, split=split, weight=weight, failure=failure)
        def fn(r, c, now):
            return qa(r, c, now, bank, hist)
        fn.__name__ = "f_" + fid
        return fn

    GREET = [
        ("good evening", "Good evening! What can I do for you?"),
        ("hey, how's it going?", "Going well, thanks. What can I help with?"),
        ("morning!", "Good morning! Ready when you are."),
        ("hi wisp, are you there?", "I'm here. What do you need?"),
        ("thanks, that was really helpful", "Glad it helped! Ask anytime."),
        ("cheers, appreciate it", "You're welcome - happy to help."),
        ("ok that's all for now, bye", "Sounds good. Talk to you later!"),
        ("goodnight wisp", "Goodnight! Sleep well."),
        ("see you tomorrow", "See you tomorrow!"),
        ("thanks a ton", "Of course - anytime."),
        ("yo", "Hey! What's up?"),
        ("that's all I needed, thank you", "You're welcome. Have a great rest of your day!"),
        ("hello again", "Welcome back! What can I do for you?"),
        ("just saying hi", "Hi! Nice to hear from you."),
    ]
    reg("ch_greet_thanks", GREET, 5)

    SOCIAL = [
        ("I'm kind of bored today", "Boredom happens. A short walk, a podcast, or tackling one small thing you've been putting off can shake it loose. Want some ideas?"),
        ("do you like music?", "I don't listen the way you do, but I find the idea of music fascinating - patterns that move people. What do you enjoy?"),
        ("what's your favorite color?", "I don't really see colors, but if I had to pick, a deep blue sounds nice. How about you?"),
        ("tell me something interesting", "Honey never really spoils - jars of it found in ancient tombs were still edible, because it's low in water and slightly acidic."),
        ("do you ever get tired?", "No, I don't get tired the way people do. You can ask me things any time."),
        ("I had a rough day", "I'm sorry to hear that. Do you want to talk about it, or would a distraction be better?"),
        ("what do you think about pineapple on pizza?", "It's divisive, but I think the sweet and salty combination works. Pairing it with something spicy helps. Where do you stand?"),
        ("coffee or tea?", "I'd lean toward tea for a calm afternoon and coffee for a slow morning. Which are you?"),
        ("I'm feeling a bit nervous about tomorrow", "That's completely normal. A little nerves usually means you care. Breathing slowly and doing a quick run-through tonight can help."),
        ("I finally finished my project!", "That's great news, congratulations! Take a moment to enjoy it."),
        ("are you smart?", "I can help with a lot, but I make mistakes too. I'm glad to double-check anything that matters."),
        ("would you rather live by the sea or in the mountains?", "Hard to choose, but the mountains for the quiet and the views. What about you?"),
    ]
    reg("ch_smalltalk_opinion", SOCIAL, 6)

    EXPLAIN = [
        ("how do vaccines work, in general terms", "A vaccine shows your immune system a harmless piece or weakened form of a germ, so it learns to recognize and fight it quickly if you meet the real thing."),
        ("what's the difference between weather and climate", "Weather is what's happening in the atmosphere over hours or days; climate is the average pattern over many years."),
        ("why do we have leap years", "A year is about 365.25 days, so adding an extra day every four years keeps the calendar aligned with the seasons."),
        ("what is photosynthesis", "It's how plants turn sunlight, water, and carbon dioxide into sugar for energy, releasing oxygen as a by-product."),
        ("explain inflation like I'm ten", "Inflation means prices slowly go up over time, so the same amount of money buys a little less than it used to."),
        ("what does a noun do?", "A noun names a person, place, thing, or idea, like 'teacher', 'Paris', 'table', or 'freedom'."),
        ("why is the ocean salty", "Rivers wash tiny amounts of minerals from rocks into the sea, and when water evaporates the salt stays behind, building up over millions of years."),
        ("what is a prime number", "A whole number greater than 1 whose only divisors are 1 and itself, like 2, 3, 5, 7, and 11."),
        ("how do airplanes stay up", "Their wings are shaped so air moving over them creates lower pressure above and higher pressure below, producing lift that balances the plane's weight."),
        ("what's the difference between a virus and a bacterium", "Bacteria are living single-celled organisms that can reproduce on their own; viruses are much smaller and need a host cell to replicate."),
        ("what does 'machine learning' mean", "It means programs that learn patterns from examples instead of following only hand-written rules."),
        ("why do cats purr", "Mostly as a sign of contentment, but cats also purr to self-soothe when stressed or hurt."),
        ("what is the water cycle", "Water evaporates, rises and condenses into clouds, falls as rain or snow, and then flows back to oceans, lakes, and the ground to start again."),
        ("how many continents are there", "Seven by the most common convention: Africa, Antarctica, Asia, Australia/Oceania, Europe, North America, and South America."),
        ("what is the speed of light, roughly", "About 300,000 kilometers per second, or roughly 186,000 miles per second."),
        ("why does ice float", "When water freezes it forms a structure that's less dense than liquid water, so ice floats on top."),
    ]
    reg("ch_explain", EXPLAIN, 8)

    MATH = [
        ("what's 18% of 250", "18% of 250 is 45."),
        ("how much is a 15% tip on $80", "A 15% tip on $80 is $12, so $92 in total."),
        ("convert 72 fahrenheit to celsius", "72 degrees Fahrenheit is about 22.2 degrees Celsius."),
        ("how many minutes are in 3 and a half hours", "3.5 hours is 210 minutes."),
        ("what's 9 times 7", "63."),
        ("what's the square root of 144", "12."),
        ("split $90 four ways", "Each person pays $22.50."),
        ("how many inches in 6 feet", "6 feet is 72 inches."),
        ("5 kilometers in miles?", "5 kilometers is about 3.1 miles."),
        ("how many cups are in a quart", "There are 4 cups in a quart."),
        ("what is a third of 90", "A third of 90 is 30."),
        ("is 91 a prime number", "No - 91 is 7 times 13."),
        ("what's 1,200 divided by 16", "1,200 divided by 16 is 75."),
        ("how many grams in a kilogram", "There are 1,000 grams in a kilogram."),
        ("what's 17 plus 28", "45."),
        ("convert 250 grams to ounces", "250 grams is about 8.8 ounces."),
        ("how many days are in 12 weeks", "12 weeks is 84 days."),
    ]
    reg("ch_math_units", MATH, 6)

    WORDING = [
        ("make this sound nicer: give me the file now", "How about: \"Could you send me the file when you have a moment? Thank you!\""),
        ("rephrase 'I can't come tomorrow' more formally", "\"Unfortunately, I will be unable to attend tomorrow.\""),
        ("is it 'less' or 'fewer' for people", "Use 'fewer' for things you can count, like people: \"There were fewer people than last year.\""),
        ("give me a synonym for 'happy'", "Some options: glad, cheerful, delighted, content, or pleased."),
        ("how do I say 'thanks for waiting' more warmly", "Try: \"Thank you so much for your patience - I really appreciate it.\""),
        ("fix the grammar: me and him goes to school", "\"He and I go to school.\""),
        ("make 'we need to talk' sound less scary", "Try: \"Do you have a few minutes later? I'd like to catch up on something.\""),
        ("shorten this: I wanted to reach out to you in order to ask whether you might be available", "\"Are you available?\""),
        ("is 'alot' a word", "No, it's two words: \"a lot\"."),
        ("what's a polite way to say no to a favor", "Something like: \"I wish I could help, but I can't take that on right now. Thanks for thinking of me.\""),
        ("how do you spell 'necessary'", "N-E-C-E-S-S-A-R-Y: one collar, two sleeves."),
        ("give me a friendlier way to end a work message", "Try: \"Thanks again, and have a great day!\""),
    ]
    reg("ch_wording", WORDING, 6)

    COMPLAIN = [
        ("I hate checking my calendar every morning", "That sounds tedious. A short weekly review on Sunday evening can make mornings lighter, since you already know what's coming."),
        ("emails are so stressful", "They can be. Checking only at set times and replying to the quick ones first usually helps. It's okay to leave the rest for later."),
        ("my reminders are driving me crazy", "Too many reminders turn into noise. Keeping only the ones that truly need a nudge, and grouping the rest into one daily list, can calm things down."),
        ("notes apps always get so messy", "They really do. A simple routine helps: one inbox note for quick captures, then a weekly tidy-up to file or delete things."),
        ("I never remember to reply to texts", "That's really common. Answering right away when it takes under a minute, and setting aside a regular time for the rest, tends to work."),
        ("my downloads folder is a disaster", "Almost everyone's is. A quick monthly sweep - delete what you don't need, file the rest - keeps it from piling up."),
        ("I'm so tired of notifications", "Muting the apps that aren't urgent and batching the rest into a couple of check-ins a day usually makes a big difference."),
        ("meetings take over my whole week", "That's frustrating. Blocking focus time and asking whether each meeting really needs you can win some hours back."),
        ("I always forget birthdays", "Many people do. Writing them down in one place and checking it at the start of each month is a simple fix."),
        ("my inbox has thousands of unread emails", "That's overwhelming, but you don't have to clear it all. Start fresh from today, and only dig into older ones if something specific comes up."),
        ("scheduling stuff is the worst", "Agreed, it's a lot of back-and-forth. Offering two or three specific times up front makes it much quicker."),
        ("I can't stand to-do lists", "Fair enough. Some people do better picking just the top three things for the day instead of a long list."),
    ]
    reg("ch_tempt_complaint", COMPLAIN, 8)

    ADVICE = [
        ("what's a good way to organise reminders?", "Keep it simple: put a specific time on anything time-sensitive, use clear action wording like 'Call the dentist', and review the list once a day."),
        ("how do I write a polite reminder email?", "Keep it short and friendly: mention the earlier request, state the deadline, and thank them. For example: \"Just a gentle reminder about the draft due Friday. Thanks so much!\""),
        ("how should I structure my notes for a class?", "Use a heading per topic, short bullet points under each, and a few lines at the end summarizing the key ideas in your own words."),
        ("any tips for keeping a clean inbox?", "Unsubscribe from what you never read, handle quick emails right away, and archive everything you've dealt with so the inbox only holds what's pending."),
        ("how do I say no to a meeting invite politely?", "Thank them, say you can't attend, and offer an alternative, like reading the notes afterward or joining a shorter call."),
        ("what's a good subject line for a follow-up email?", "Clear and specific works best, for example \"Following up: budget draft\" or \"Quick question about Thursday\"."),
        ("how often should I back up my files?", "A good rule is automatic daily or weekly backups, plus keeping one copy somewhere other than your computer."),
        ("how can I plan my week better?", "Pick your top three priorities on Monday, block time for them first, and leave some buffer for surprises."),
        ("how do I organize a messy desktop?", "Make a few broad folders like Work, Personal, and Archive, move files into them, and set a weekly habit to clear the rest."),
        ("what's the best time to schedule a meeting?", "It depends on the group, but mid-morning on weekdays tends to work well, avoiding early Monday and late Friday."),
        ("how long should a follow-up email wait?", "A few business days is typical. If it's urgent, one or two days is fine."),
        ("how do I keep a good to-do list?", "Keep each item small and actionable, put the most important ones at the top, and remove finished items so the list stays short."),
    ]
    reg("ch_tempt_advice", ADVICE, 8)

    CAPGEN = [
        ("what kind of things are you good at?", "I'm good at everyday tasks like managing schedules and reminders, working with email and messages, handling notes and files, and answering questions or helping with writing."),
        ("how do you work?", "You tell me what you need in plain words, and I figure out the steps. For anything that sends or changes something, I check with you first."),
        ("do you keep my data private?", "Wisp runs on your own Mac, and I only use what's needed for the task you ask for."),
        ("is there anything you can't do?", "Yes - I can't do things that need accounts I'm not connected to, like ordering food or booking travel, and I can't do anything without your say-so when it matters."),
        ("can I just talk to you normally?", "Yes, plain language works fine. You don't need special commands."),
        ("do I need to be exact when I ask for things?", "Not at all. If I'm missing a detail, I'll ask you instead of guessing."),
        ("will you send things without asking me?", "No - for anything that goes out to other people, I check with you first."),
        ("what should I ask you first?", "A good start is asking about your day, setting a reminder, or having me summarize something. Try whatever you do most often."),
    ]
    reg("ch_capability_general", CAPGEN, 5, failure=("tool_on_chat", "capability_question_tool_call"))

    HIST = [
        ("what's the capital of australia", "Canberra.", "thanks, I always think it's Sydney", "A very common mix-up - Sydney is the biggest city, but Canberra is the capital."),
        ("convert 72 fahrenheit to celsius", "72 degrees Fahrenheit is about 22.2 degrees Celsius.", "is that warm?", "Yes, that's a comfortable, mild temperature - the kind of day you'd want a light layer at most."),
        ("give me a haiku about rain", "Soft rain on the roof,\nthe quiet city exhales,\npuddles hold the sky.", "can you make it more cheerful", "Bright rain on the roof,\nthe whole city starts to dance,\npuddles full of sun."),
        ("explain what an API is in simple terms", "An API is a set of rules that lets one program ask another for data or actions, like a waiter carrying orders to the kitchen.", "ok that makes sense, thanks", "Glad it clicked!"),
        ("how many cups are in a quart", "There are 4 cups in a quart.", "and a gallon?", "There are 16 cups in a gallon."),
        ("what's 18% of 250", "18% of 250 is 45.", "and 20%?", "20% of 250 is 50."),
        ("rephrase 'I can't come tomorrow' more formally", "\"Unfortunately, I will be unable to attend tomorrow.\"", "a bit shorter please", "\"I'm unable to attend tomorrow.\""),
        ("tell me a joke", "Why don't scientists trust atoms? Because they make up everything.", "ha, another one", "Why did the scarecrow win an award? He was outstanding in his field."),
        ("why do cats purr", "Mostly as a sign of contentment, but cats also purr to self-soothe when stressed or hurt.", "that's cute", "It is! They're surprisingly complicated little animals."),
        ("give me three name ideas for a bakery", "1) Rise & Shine Bakehouse, 2) The Daily Crumb, 3) Golden Whisk.", "I like the second one", "The Daily Crumb has a nice, friendly feel. It would suit a neighborhood shop well."),
        ("how do I write a polite reminder email?", "Keep it short and friendly: mention the earlier request, the deadline, and thank them.", "can you give an example", "Sure: \"Hi, just a friendly reminder about the report due Friday. Let me know if you need anything from me. Thanks!\""),
        ("what's a good way to organise reminders?", "Put a specific time on anything time-sensitive and use clear action wording.", "what do you mean by action wording", "Start each reminder with a verb, like \"Call the dentist\" or \"Pay the electric bill\", so it's obvious what to do."),
        ("is 91 a prime number", "No - 91 is 7 times 13.", "huh, I would've guessed prime", "It's a classic trick - it looks prime because it isn't divisible by 2, 3, or 5."),
        ("what's your favorite color?", "I don't really see colors, but a deep blue sounds nice. How about you?", "I like green", "Green is a lovely choice - calm and fresh."),
    ]
    reg("ch_reply_history", HIST, 8, hist=True, failure=("tool_on_chat", "followup_context_lost"))

    TIPS = [
        ("how do I boil an egg properly", "Lower the egg into boiling water and cook about 7 minutes for a jammy yolk or 10 to 12 for fully set, then cool it in cold water."),
        ("what's a good stretch for a stiff neck", "Gently tilt your ear toward your shoulder and hold for 20 to 30 seconds on each side. Stop if anything hurts."),
        ("how can I fall asleep faster", "Keep a consistent bedtime, dim the lights and screens an hour before, and try slow breathing. If it keeps being a problem, mention it to a doctor."),
        ("how do I get better at remembering names", "Repeat the name when you hear it, link it to something memorable about the person, and use it once or twice in the conversation."),
        ("how do I stay focused when working from home", "Set a dedicated workspace, work in short blocks with breaks, and keep your phone out of reach."),
        ("how do I get a red wine stain out of a shirt", "Blot it right away, don't rub, then rinse with cold water and wash with detergent. Salt or club soda can help with fresh stains."),
        ("what should I pack for a weekend trip", "A few versatile outfits, toiletries, chargers, any medications, and a light jacket. Keep the bag small so it's easy to carry."),
        ("how do I learn a language faster", "Practice a little every day, listen to real speech, and speak early even if it's imperfect."),
    ]
    reg("ch_everyday_tips", TIPS, 4)

    # ---- dev families: different scenarios and phrasing
    DEV1 = [
        ("my phone buzzes constantly and I can't think", "That sounds draining. Turning off non-urgent alerts and keeping the phone out of sight while you focus can bring a lot of calm."),
        ("I dread opening my inbox on mondays", "A lot of people do. Opening it with a plan, like handling only the three most important messages first, can make it feel smaller."),
        ("what's a kind way to ask someone to reply to my message?", "Something gentle like: \"No rush at all, but when you get a chance, I'd love to hear your thoughts.\""),
        ("is it rude to send a text instead of calling?", "Not usually. Text is fine for simple or non-urgent things, while a call is better for sensitive or time-critical matters."),
        ("how do I draft a good out-of-office message?", "Say you're away, give the dates you'll return, and name an alternative contact if it's urgent. Keep the tone short and friendly."),
        ("what's the capital of canada", "Ottawa."),
        ("how many seconds are in an hour", "There are 3,600 seconds in an hour."),
        ("good luck charm ideas for an exam?", "A favorite pen, a small keepsake, or a calm pre-exam routine work for many people. Mostly, it helps you feel steady."),
    ]
    reg("ch_tempt_dev", DEV1, 3, split="dev")

    DEV2 = [
        ("what's the tallest mountain on earth", "Mount Everest, at about 8,849 meters above sea level.", "is that taller than K2", "Yes - K2 is the second highest, at about 8,611 meters."),
        ("how do I make my email sound less blunt", "Add a friendly greeting, soften requests with \"could you\", and close with a thank you.", "show me with 'send the invoice'", "\"Hi! Could you send the invoice when you get a chance? Thanks so much.\""),
        ("thanks for the help earlier", "Anytime! Happy to help.", "you're pretty good at this", "Thank you - that's kind of you to say."),
        ("what is 12 times 15", "12 times 15 is 180.", "what about 13 times 15", "13 times 15 is 195."),
        ("how many feet in a mile", "There are 5,280 feet in a mile.", "and in a kilometer?", "A kilometer is about 3,281 feet."),
    ]
    reg("ch_reply_history_dev", DEV2, 2, split="dev", hist=True, failure=("tool_on_chat", "followup_context_lost"))
