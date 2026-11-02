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
