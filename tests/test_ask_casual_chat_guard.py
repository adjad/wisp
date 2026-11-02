"""Casual chat, opinion and general-knowledge questions are not "my own data".

Ask re-prompts a tool-less answer with "call the tool that holds it now" when
`asks_personal_data(prompt)` is true or the reply is a `stall_reply`. The guard
was so broad that "should I get a cat or a dog" and "my phone keeps dying fast,
any tips?" were pushed into tool lookups ("I couldn't find any web results for
cozy movies"). Real lookups ("what's on my calendar tomorrow") must still hit.
"""
import pytest

from service import model_only as mo


@pytest.mark.parametrize("prompt", [
    # bare "do I" / "should I"
    "what do i do with overripe bananas",
    "should i learn guitar or piano",
    "what should i watch tonight if i like cozy movies",
    "do i need to peel ginger before grating it",
    "should i get a cat or a dog",
    "do i have to peel ginger first?",
    # "my <ambiguous noun>" where the person's data is not the point
    "my phone keeps dying fast, any tips?",
    "my class is so boring, how do i stay awake?",
    "my day was rough, can you cheer me up?",
    "what should i pack in my bag for a beach day?",
    "i messed up my order at a restaurant, is it rude to send it back?",
    "should I wear my blue jacket tomorrow?",
    "what should I bring to my sister's party tomorrow?",
    # relatives quoted
    "my dad said cracking knuckles causes arthritis, is that true?",
    "my mom said you shouldn't swim after eating, is that true?",
    # ship / deliver / arrive in general knowledge
    "when was the first iphone shipped?",
    "when do swallows arrive in spring?",
    "how are pizzas delivered so fast?",
    # "due to"
    "is the heat wave due to climate change?",
    # TV, not the calendar
    "what's on tv tonight?",
    "my meetings are exhausting, any tips?",
    "what are my thoughts on pineapple pizza?",
    "who am I to judge", "who am i supposed to vote for", "am I overthinking this",
    "am I going to be ok", "did I do the right thing", "do I owe my friend an apology for being late",
    "what's on at the movies", "what's on the menu at a thai restaurant",
    "how long is a marathon", "how busy is a typical hospital ER", "what time is it in tokyo",
    "summarize the plot of hamlet", "describe a sunset to me", "recap the french revolution",
    "how many tasks should I do per day to be productive", "what do i need to do to learn spanish",
    "what's my chance of winning the lottery", "when will the next iphone arrive",
])
def test_casual_and_general_questions_are_not_personal(prompt):
    assert not mo.asks_personal_data(prompt)


@pytest.mark.parametrize("prompt", [
    "What's on my calendar tomorrow?", "Did Jordan reply to me?", "How heavy is my backpack?",
    "any new texts?", "Do I have anything this weekend?",
    "Is it going to rain during my coffee with Alex tomorrow?",
    "When is the W-9 due?", "Has the package shipped?", "what's on tomorrow",
    "Is there a meeting this afternoon?", "When is the dentist appointment on Friday?",
    "what is up for tommorow", "anything planned for friday", "do you know my birthday?",
    "when is my midterm?", "where are my keys?", "what's my wifi password?",
    "did my order ship?", "has my package arrived?", "am i free at 3 tomorrow?",
    "did i pay the electric bill?", "do i have class tomorrow?",
    "what's my mom's phone number?", "did mom text me?", "how does my day look?",
    "what are my open tasks?", "is my flight on time?",
    "what are my plans for the weekend?", "has it shipped yet?",
    "do i have a meeting at noon?", "when is my class?", "when do my classes start?",
    "what do I have tomorrow?", "what do I have going on this weekend?",
    # first-person records and lookups the narrowed guard briefly dropped
    "Who am I meeting tomorrow?", "Am I double booked", "How busy am I this week?",
    "Did I get a text from mom", "Do I owe Sam money?", "What's the status of my order",
    "track my order", "summarize my day", "describe my day", "How many tasks are on my list",
    "Am I going to be late for my 3pm?", "What time is my lunch with Sam",
    "Do I need to bring anything to my 2pm", "Did the invoice arrive?",
    "When will my stuff arrive?", "what's on at 3", "how long is my commute",
    "recap my week", "how many emails do I have", "did the statement arrive",
    "what do I have at 3", "when do I need to leave", "is my 3pm cancelled",
    "is my dentist tomorrow", "who do I owe a reply to", "has Sam replied",
    "what's the status of my refund", "did my payment go through",
    "what time does my meeting start", "how does my afternoon look",
])
def test_lookups_of_the_users_own_data_stay_personal(prompt):
    assert mo.asks_personal_data(prompt)


@pytest.mark.parametrize("text", [
    "Hang tight, I'm checking your calendar now!",
    "One moment!",
    "Let me check your schedule.",
    "Give me just a moment to look that up.",
    "Bear with me while I pull up your reminders.",
    "Stand by.",
    "Sure!\nOne moment.",
    "Okay.\nHang tight, I'm checking your calendar.\nOne moment!",
    "One moment!\nBear with me.",
])
def test_short_promises_to_look_are_stalls(text):
    assert mo.stall_reply(text)


@pytest.mark.parametrize("text", [
    "Hang on to the bananas! Overripe ones are perfect for banana bread: mash three, mix with "
    "half a cup of sugar, one egg, a stick of melted butter and a cup and a half of flour, "
    "then bake at 350 for about an hour until a toothpick comes out clean.",
    "Hang on to them for banana bread.",
    "Stand by your decision; guitar is a fine first instrument.",
    "It was one moment in history that changed how phones look.",
    "Hang on to the bananas!\nOverripe ones make great banana bread.",
])
def test_real_answers_that_use_a_stall_phrase_are_not_stalls(text):
    assert not mo.stall_reply(text)


@pytest.mark.parametrize("prompt", [
    "When is the W-9 due?", "Did my order ship?", "has my package arrived?",
])
def test_mail_first_still_leads_with_mail(prompt):
    assert mo.mail_first(prompt)


def test_calendar_and_reminder_first_keep_their_gate():
    assert mo.calendar_first("When is my midterm?")
    assert mo.calendar_first("do i have class tomorrow?")
    assert mo.reminders_first("what are my open tasks?")
    assert not mo.calendar_first("my class is so boring, how do i stay awake?")
    assert not mo.reminders_first("should i make a to-do list app or a notes app?")


def test_casual_questions_stay_knowledge_questions():
    assert mo.knowledge_question("how do i stay awake in a boring class?")
    assert not mo.mail_first("when was the first iphone shipped?")


def test_day_plan_hint_yields_to_the_users_own_length_or_format():
    assert "unless the user asked for another length or format" in mo.ASK_DAY_PLAN_HINT


# Second audit of the narrowed guard (7734fb72): lookups the old "question +
# my X" catch-all held, now held by a lookup lead with "my <thing>" behind it.
@pytest.mark.parametrize("prompt", [
    "Did my landlord write back?", "What did my boss say in the last email?", "How does my Thursday look?",
    "Do I have any voicemails?", "Am I booked for lunch on Wednesday?", "Do I owe my roommate for groceries?",
    "What did I say to Maya yesterday?", "What did Mom say?", "What time do I pick up the kids?",
    "What day is my interview?", "How much is my electric bill?", "What am I doing tonight?",
    "What's left on my list?", "Who is my dentist?", "How old is my dog?"])
def test_lookup_leads_with_my_thing_stay_personal(prompt):
    assert mo.asks_personal_data(prompt)


@pytest.mark.parametrize("prompt", [
    "What is my purpose?", "how much water should my dog drink", "who is the best guitarist ever",
    "what did the romans eat", "how old is the universe", "what's my best option for dinner?",
    "where should i take my mom for her birthday", "what do you think of my idea to learn rust"])
def test_opinion_with_my_thing_is_not_personal(prompt):
    assert not mo.asks_personal_data(prompt)


def test_a_two_line_promise_to_check_is_a_stall():
    assert mo.stall_reply("Hang on.\nChecking now.")
