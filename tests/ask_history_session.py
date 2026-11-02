"""A synthetic 20-exchange Ask chat for long-history recall checks.

Each exchange plants one distinctive fact: either in what the user said or in
what the assistant answered (a looked-up detail). Replies are sized like real
Ask replies (median ~1,150 characters, the local session store's median), so
the token budget bites where it does in a real long chat. No model, no user
data: every name and number here is invented.

Used by tests/test_ask_history_recall.py and scripts/measure_ask_history.py.
"""
from __future__ import annotations

# (user text, assistant text, fact, where the fact sits). The fact string is
# what a recall check looks for in the request the model is sent.
_FILLER = (
    "If you want, I can also set a reminder or add it to your calendar so it does not "
    "slip through the cracks later this week. Just let me know what works best for you, "
    "and I will keep it short. I can also check whether anything else on your schedule "
    "overlaps with it, look up travel time from home, or draft a quick note to anyone "
    "who needs to know. Nothing has been changed yet; this is only a summary of what I "
    "found and what I would suggest as the next step. "
)

EXCHANGES: list[tuple[str, str, str, str]] = [
    ("My locker code at the gym is 4817, remember that for me.",
     "Got it. Your gym locker code is 4817. " + _FILLER * 2,
     "4817", "user"),
    ("When is my dentist appointment?",
     "Your dentist appointment is Thursday at 3:15 PM with Dr. Okafor at Bayview Dental. "
     + _FILLER * 2,
     "Okafor", "assistant"),
    ("My sister Priya is flying in on flight UA 1192, can you keep an eye on it?",
     "Sure. I will treat UA 1192 as Priya's flight. " + _FILLER * 2,
     "UA 1192", "user"),
    ("What did the landlord say about the lease?",
     "The landlord, Marta Kowalczyk, wrote that the renewal is due by the 28th and the "
     "new rent would be $2,140 a month. " + _FILLER * 2,
     "2,140", "assistant"),
    ("The wifi password for the cabin is pinecone-77.",
     "Noted, the cabin wifi password is pinecone-77. " + _FILLER,
     "pinecone-77", "user"),
    ("Summarize the email from my manager.",
     "Your manager asked for the quarterly deck by Friday. " + _FILLER
     + "She also mentioned the offsite moved to the Halverson Lodge, and she wants "
       "the budget numbers checked twice before they go out. " + _FILLER,
     "Halverson", "assistant-middle"),
    ("I parked on level 3, spot C14.",
     "Okay, level 3, spot C14. " + _FILLER,
     "C14", "user"),
    ("What's the weather in Tahoe this weekend?",
     "Tahoe looks cold this weekend: a high of 41 degrees on Saturday with snow showers "
     "after noon. " + _FILLER * 2,
     "41 degrees", "assistant"),
    ("My favourite coffee order is an oat cortado, extra hot.",
     "Oat cortado, extra hot. Good to know. " + _FILLER,
     "oat cortado", "user"),
    ("Who texted me this morning?",
     "Jonah Abernathy texted at 8:12 asking if you can swap the Tuesday shift. "
     + _FILLER * 2,
     "Abernathy", "assistant"),
    ("The plumber's name is Ruiz and he charges 95 an hour.",
     "Plumber Ruiz, 95 an hour. " + _FILLER,
     "Ruiz", "user"),
    ("Find my passport renewal reminder.",
     "Your passport renewal reminder says to mail form DS-82 before November 30. "
     + _FILLER * 2,
     "DS-82", "assistant"),
    ("My bike lock combination is 3-9-1-4.",
     "Bike lock combination 3-9-1-4, noted. " + _FILLER,
     "3-9-1-4", "user"),
    ("What's on my calendar tomorrow?",
     "Tomorrow you have a 10:30 standup and lunch with Ingrid Solberg at noon. "
     + _FILLER * 2,
     "Solberg", "assistant"),
    ("Mom's birthday dinner is at Lucca's on the 14th.",
     "Mom's birthday dinner at Lucca's on the 14th. " + _FILLER,
     "Lucca's", "user"),
    ("How much was the electric bill?",
     "The electric bill from Pacific Grid came to $183.62, due on the 9th. "
     + _FILLER * 2,
     "183.62", "assistant"),
    ("My library card number is 22190045.",
     "Library card 22190045, saved for this chat. " + _FILLER,
     "22190045", "user"),
    ("Did the package ship?",
     "Yes, the package shipped with tracking number 1Z84W tomorrow by 8 PM. "
     + _FILLER * 2,
     "1Z84W", "assistant"),
    ("The kids' pediatrician is Dr. Varga.",
     "Dr. Varga for the kids, noted. " + _FILLER,
     "Varga", "user"),
    ("What's the gate code for the community pool?",
     "The pool gate code from the HOA email is 5521#. " + _FILLER * 2,
     "5521#", "assistant"),
]

FINAL_PROMPT = "Quick question about something from earlier."


def plant(store, sid: str) -> None:
    """Store the 20 exchanges as the session's turns, oldest first."""
    for user, assistant, _, _ in EXCHANGES:
        store.add_turn(sid, "user", user)
        store.add_turn(sid, "assistant", assistant)


def recall_depths(messages: list[dict]) -> dict[int, bool]:
    """Exchanges back (1 = the latest stored exchange) -> whether that
    exchange's fact appears anywhere in `messages`."""
    text = "\n".join(str(m.get("content") or "") for m in messages)
    count = len(EXCHANGES)
    return {count - i: fact in text for i, (_, _, fact, _) in enumerate(EXCHANGES)}
