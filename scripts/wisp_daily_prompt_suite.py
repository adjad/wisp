#!/usr/bin/env python3
"""Build and run a 1000-prompt daily-usage Wisp test suite.

The suite is designed around prompts people might actually ask in real life
for planning, messaging, reminders, files, shopping, travel, and quick utility
tasks.  It writes a replay-compatible JSON structure:

```
[{"label": "planner", "prompts": ["...", ...]}, ...]
```

By default this script writes:
`test_fixtures/wisp_daily_life_1000_prompts.json`

If `--run` is passed, it executes the suite with
`scripts/replay_prompts.py`, which streams turns against `127.0.0.1:8765` and
auto-denies confirmations.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SUITE_PATH = ROOT / "test_fixtures" / "wisp_daily_life_1000_prompts.json"
DEFAULT_RESULTS_PATH = ROOT / "test_results" / "wisp_daily_life_1000_results.json"
DEFAULT_CONTACT = "Mom"
DEFAULT_EMAIL = "johnstandark@gmail.com"

CONTACTS = [
    "Dad", "Sam", "Alex", "Taylor", "Priya", "Jordan", "Riley",
    "Kai", "Morgan", "Casey", "Nina", "Devon", "Cameron", "Avery", "Mia",
]

DAYS = [
    "today", "tomorrow", "this Friday", "next Monday", "this weekend",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
]

TIMES = [
    "8 AM", "9 AM", "10:30 AM", "11:15 AM", "1 PM", "2:30 PM",
    "3:45 PM", "5:00 PM", "6:30 PM", "8 PM",
]

TASKS = [
    "pick up groceries", "buy groceries", "water the plants", "call the dentist",
    "prepare the monthly budget", "review my emails", "pay the electric bill",
    "pack for the trip", "walk the dog", "finish the project brief",
]

LOCATIONS = [
    "downtown", "home", "office", "coffee shop", "gym", "the grocery store",
    "market park", "lake park", "airport", "downtown train station",
    "Oakland", "San Francisco", "Dublin", "Seattle", "Boston", "Chicago",
]

ITEMS = [
    "milk", "coffee", "bread", "eggs", "spinach", "rice", "pasta",
    "olive oil", "dish soap", "paper towels", "toilet paper", "almond butter",
]

ACTIVITIES = [
    "team meeting", "client call", "project review", "workout session",
    "doctor appointment", "therapy appointment", "dentist appointment",
    "parent-teacher note", "car maintenance", "tax filing", "laptop backup",
]

SUBJECTS = [
    "vacation", "project update", "tomorrow's deliverable", "team progress",
    "invoice status", "weekend plans", "budget questions", "meeting reminder",
    "new idea", "support request",
]

TOPICS = [
    "weekend planning", "grocery list", "fitness goals", "meal prep", "travel",
    "home repairs", "work priorities", "family schedule", "study time", "pet care",
]


def _pick(items: list[str], idx: int, step: int = 1) -> str:
    return items[(idx * step) % len(items)]


def build_planning_prompts() -> list[str]:
    out: list[str] = []
    for i in range(100):
        day = _pick(DAYS, i, 3)
        time = _pick(TIMES, i, 7)
        task = _pick(TASKS, i, 11)
        activity = _pick(ACTIVITIES, i, 13)
        template = i % 10
        if template == 0:
            out.append(f"What do I have on my calendar for {day}?")
        elif template == 1:
            out.append(f"Do I have any open time on {day} around {time}?")
        elif template == 2:
            out.append(f"Add a reminder to {task} at {time} {day}.")
        elif template == 3:
            out.append(f"Add a 30-minute block for {activity} on {day}.")
        elif template == 4:
            out.append(f"Reschedule my {activity} to {time} this {day}.")
        elif template == 5:
            out.append(f"Who else is in my next meeting and when does it start?")
        elif template == 6:
            out.append(f"Remind me at {time} to call {DEFAULT_CONTACT} about {task}.")
        elif template == 7:
            out.append(f"Move next week's {activity} to after {time} on {day}.")
        elif template == 8:
            out.append(f"Show my reminders for {day} and next {day}.")
        else:
            out.append(f"What meetings do I have with {DEFAULT_CONTACT} this {day}?")
    return out


def build_messaging_prompts() -> list[str]:
    out: list[str] = []
    for i in range(100):
        task = _pick(TASKS, i, 3)
        time = _pick(TIMES, i, 2)
        subject = _pick(SUBJECTS, i, 7)
        location = _pick(LOCATIONS, i, 11)
        alt_contact = _pick(CONTACTS, i, 7)
        template = i % 10
        if template == 0:
            out.append(f"Text {DEFAULT_CONTACT} that I will be running late and can arrive by {time}.")
        elif template == 1:
            out.append(f"Send {DEFAULT_CONTACT} a short note: I finished {subject} and can chat after work.")
        elif template == 2:
            out.append(f"Email {DEFAULT_EMAIL} a quick summary of {subject}.")
        elif template == 3:
            out.append(f"Draft an email to {DEFAULT_EMAIL} asking about our meeting at {location}.")
        elif template == 4:
            out.append(f"What messages did I get from {DEFAULT_CONTACT} today?")
        elif template == 5:
            out.append(f"Send a text to {DEFAULT_CONTACT} asking if {subject} is still on.")
        elif template == 6:
            out.append(f"Can you text {DEFAULT_CONTACT} that I can {task} later this afternoon?")
        elif template == 7:
            out.append(f"Prepare a follow-up message for {DEFAULT_CONTACT} about {subject}.")
        elif template == 8:
            out.append(f"Who should I send my {subject} update to first: {DEFAULT_CONTACT} or {alt_contact}?")
        else:
            out.append(f"Write a friendly message to {DEFAULT_CONTACT} confirming our plan for {time}.")
    return out


def build_notes_prompts() -> list[str]:
    out: list[str] = []
    for i in range(100):
        topic = _pick(TOPICS, i, 3)
        item = _pick(ITEMS, i, 4)
        activity = _pick(ACTIVITIES, i, 2)
        day = _pick(DAYS, i, 6)
        template = i % 10
        if template == 0:
            out.append(f"Create a note called '{topic}' with bullet points for {day}.")
        elif template == 1:
            out.append(f"Add 'Need {item} tomorrow' to my shopping note.")
        elif template == 2:
            out.append(f"Take a note: remember to {activity} and check {topic} results.")
        elif template == 3:
            out.append(f"Find my latest notes about {topic}.")
        elif template == 4:
            out.append(f"Create a note for {day} with one-paragraph prep for {activity}.")
        elif template == 5:
            out.append(f"Append this to my weekly planning note: {activity} finished by { _pick(TIMES, i, 5)}.")
        elif template == 6:
            out.append(f"Create a checklist note called '{activity} plan' for the week.")
        elif template == 7:
            out.append(f"Please summarize any notes that mention {topic} and deadlines.")
        elif template == 8:
            out.append(f"Make a note that my next grocery run needs {item}, { _pick(ITEMS, i + 7, 3)} and fruit.")
        else:
            out.append(f"Search notes for {topic} and pull the section about {activity}.")
    return out


def build_shopping_prompts() -> list[str]:
    out: list[str] = []
    for i in range(100):
        item = _pick(ITEMS, i, 2)
        qty = (i % 5) + 1
        location = _pick(LOCATIONS, i, 4)
        topic = _pick(TOPICS, i, 8)
        day = _pick(DAYS, i, 9)
        template = i % 10
        if template == 0:
            out.append(f"Add {item} to my shopping list.")
        elif template == 1:
            out.append(f"Add {qty} units of {item} and some eggs to my grocery list.")
        elif template == 2:
            out.append(f"Plan tonight's dinner with whatever I have for {topic}.")
        elif template == 3:
            out.append(f"Give me a budget-friendly meal idea using {item} and vegetables.")
        elif template == 4:
            out.append(f"Find a good store near {location} to get {item} today.")
        elif template == 5:
            out.append(f"Can you suggest a quick {day} grocery list for a busy week?")
        elif template == 6:
            out.append(f"Track this item: I need {item} at {location} by {day}.")
        elif template == 7:
            out.append(f"What pantry staples should I replenish this month for {topic}?")
        elif template == 8:
            out.append(f"Make a weekly grocery plan for {qty} people centered on {item}.")
        else:
            out.append(f"Should I buy {item} or a substitute for this week?")
    return out


def build_files_prompts() -> list[str]:
    out: list[str] = []
    folders = ["Desktop", "Downloads", "Documents", "Work", "Home", "Projects"]
    file_types = ["report", "notes", "invoice", "plan", "log", "summary"]
    for i in range(100):
        folder = _pick(folders, i, 3)
        ftype = _pick(file_types, i, 4)
        day = _pick(DAYS, i, 7)
        template = i % 10
        if template == 0:
            out.append(f"List files in {folder}.")
        elif template == 1:
            out.append(f"What files were modified in {folder} on {day}?")
        elif template == 2:
            out.append(f"Create a draft file called {ftype}_{_pick(TIMES, i, 6).replace(':', '')}.txt with tomorrow's notes.")
        elif template == 3:
            out.append(f"Search for the latest file about {ftype} and summarize it.")
        elif template == 4:
            out.append(f"Read the text in {folder}/meeting_notes.txt and give a one-line summary.")
        elif template == 5:
            out.append(f"Move {DEFAULT_CONTACT}'s {ftype} draft from {folder} into archive.")
        elif template == 6:
            out.append(f"Create a folder called `weekly-{day.replace(' ', '-')}` under {folder}.")
        elif template == 7:
            out.append(f"Show me all PDFs under {folder} and their approximate purpose.")
        elif template == 8:
            out.append(f"Find duplicate file names in {folder}.")
        else:
            out.append(f"Can you remind me to review the {ftype} file before { _pick(TIMES, i, 9)}?")
    return out


def build_info_prompts() -> list[str]:
    out: list[str] = []
    cities = ["San Francisco", "Dublin", "Seattle", "Austin", "Boston", "Denver", "Portland", "Chicago"]
    for i in range(100):
        city = _pick(cities, i, 3)
        topic = _pick(TOPICS, i, 5)
        subject = _pick(SUBJECTS, i, 4)
        item = _pick(ITEMS, i, 6)
        template = i % 10
        if template == 0:
            out.append(f"What's the weather in {city} for this afternoon?")
        elif template == 1:
            out.append(f"What is the latest update about {subject}?")
        elif template == 2:
            out.append(f"Compare current temperatures in {city} and { _pick(cities, i + 3, 2)}.")
        elif template == 3:
            out.append(f"Define the word {item} in a simple way.")
        elif template == 4:
            out.append(f"Explain {topic} in a quick, practical summary.")
        elif template == 5:
            out.append(f"Tell me a practical tip for productivity and focus today.")
        elif template == 6:
            out.append(f"Search for a reliable way to reduce spending on {subject} this month.")
        elif template == 7:
            out.append(f"What is the current local time in {city} compared to here?")
        elif template == 8:
            out.append(f"Give me one travel safety tip for visiting {city}.")
        else:
            out.append(f"Help me compare options for {subject} and pick the simplest approach.")
    return out


def build_travel_prompts() -> list[str]:
    out: list[str] = []
    origins = ["home", "office", "downtown", "airport", "the gym", "library", "grand ave", "city hall"]
    destinations = ["Oakland", "San Francisco", "Dublin", "Seattle", "Chicago", "Boston", "Austin", "Denver"]
    for i in range(100):
        origin = _pick(origins, i, 2)
        destination = _pick(destinations, i, 5)
        time = _pick(TIMES, i, 11)
        day = _pick(DAYS, i, 4)
        template = i % 10
        if template == 0:
            out.append(f"How long is it to drive from {origin} to {destination} right now?")
        elif template == 1:
            out.append(f"What is the best way to get from {origin} to {destination} by transit around {time}?")
        elif template == 2:
            out.append(f"Should I leave for {destination} at {time} on {day}?")
        elif template == 3:
            out.append(f"Find nearby parking tips for {destination}.")
        elif template == 4:
            out.append(f"Estimate driving distance from {origin} to {destination} for {day}.")
        elif template == 5:
            out.append(f"What traffic-aware route option is good for {origin} to {destination}?")
        elif template == 6:
            out.append(f"How can I stay on budget for a weekend trip from {origin} to {destination}?")
        elif template == 7:
            out.append(f"Give me a backup route from {origin} to {destination} if traffic is heavy.")
        elif template == 8:
            out.append(f"What should I pack for a short {day} trip to {destination}?")
        else:
            out.append(f"Can you draft a quick travel checklist for {origin} to {destination} this week?")
    return out


def build_health_prompts() -> list[str]:
    out: list[str] = []
    habits = ["walk", "stretch", "drink water", "sleep", "journal", "cook", "meditate", "read"]
    for i in range(100):
        habit = _pick(habits, i, 3)
        time = _pick(TIMES, i, 4)
        day = _pick(DAYS, i, 2)
        template = i % 10
        if template == 0:
            out.append(f"Set a reminder to {habit} at {time} every {day}.")
        elif template == 1:
            out.append(f"Give me a short hydration plan for today with reminders.")
        elif template == 2:
            out.append(f"Create a simple {habit} routine before bed tonight.")
        elif template == 3:
            out.append(f"Draft a 5-minute evening wind-down plan.")
        elif template == 4:
            out.append(f"Should I add rest day {day} after a long walk?")
        elif template == 5:
            out.append(f"Help me track a 7-day habit streak for {habit}.")
        elif template == 6:
            out.append(f"Remind {DEFAULT_CONTACT} that I moved my walk to {time}.")
        elif template == 7:
            out.append(f"Give one nutrition idea to keep my energy stable on {day}.")
        elif template == 8:
            out.append(f"Plan a healthy lunch with less than 600 calories for tonight.")
        else:
            out.append(f"Create a short list of low-effort workouts for a busy {day}.")
    return out


def build_money_prompts() -> list[str]:
    out: list[str] = []
    for i in range(100):
        template = i % 10
        amount = f"{(i + 1) * 12}.{(i % 9)}"
        item = _pick(ITEMS, i, 3)
        task = _pick(TASKS, i, 8)
        time = _pick(TIMES, i, 6)
        if template == 0:
            out.append(f"How much is 18 percent of {amount}?")
        elif template == 1:
            out.append(f"Convert {amount} {_pick(['USD', 'EUR', 'GBP'], i, 2)} to a monthly budget for {task}.")
        elif template == 2:
            out.append(f"I spent {amount} on {item}; help me log it under {task}.")
        elif template == 3:
            out.append(f"What is a practical way to split {amount} between needs and extras?")
        elif template == 4:
            out.append(f"Write me a weekly spending summary for {task} with three controls.")
        elif template == 5:
            out.append(f"Can you convert 15 pounds of groceries to kilos for my recipe?")
        elif template == 6:
            out.append(f"If I save {amount} each {time}, what is that after a month?")
        elif template == 7:
            out.append(f"Suggest a grocery budget for this week with {amount} max.")
        elif template == 8:
            out.append(f"Create a simple reminder to review expenses at {time} on { _pick(DAYS, i, 4)}.")
        else:
            out.append(f"How can I reduce my spending on {item} while still getting what I need?")
    return out


def build_creative_prompts() -> list[str]:
    out: list[str] = []
    for i in range(100):
        topic = _pick(TOPICS, i, 7)
        item = _pick(ITEMS, i, 5)
        template = i % 10
        if template == 0:
            out.append(f"Draft a polite email response about {topic} to {DEFAULT_CONTACT}.")
        elif template == 1:
            out.append(f"Help me write a short note requesting an update on {topic}.")
        elif template == 2:
            out.append(f"Write a one-paragraph summary of this week focused on {topic}.")
        elif template == 3:
            out.append(f"Create a simple checklist for organizing a room around {topic}.")
        elif template == 4:
            out.append(f"Draft a 3-day plan to finish {topic} with realistic daily goals.")
        elif template == 5:
            out.append(f"Give me a clean email subject line for {topic} follow-up.")
        elif template == 6:
            out.append(f"Write an easy outline for a talk about {item}.")
        elif template == 7:
            out.append(f"Help me brainstorm three ideas for a quick {topic} improvement.")
        elif template == 8:
            out.append(f"Create a simple thank-you message for someone helping with {topic}.")
        else:
            out.append(f"Draft a short message to {DEFAULT_CONTACT} with one ask and one clear next step for {topic}.")
    return out


def build_suite() -> list[dict[str, object]]:
    groups = [
        ("daily planning and reminders", build_planning_prompts()),
        ("communication and follow-ups", build_messaging_prompts()),
        ("notes and memory prompts", build_notes_prompts()),
        ("shopping and pantry planning", build_shopping_prompts()),
        ("files and local organization", build_files_prompts()),
        ("information and utility", build_info_prompts()),
        ("travel and logistics", build_travel_prompts()),
        ("health and wellness", build_health_prompts()),
        ("finance and budgets", build_money_prompts()),
        ("creative writing prompts", build_creative_prompts()),
    ]
    suite: list[dict[str, object]] = []
    for label, prompts in groups:
        suite.append({"label": label, "prompts": prompts})
    all_prompts = sum(len(group["prompts"]) for group in suite)
    if all_prompts != 1000:
        raise RuntimeError(f"Expected 1000 prompts, built {all_prompts}")
    return suite


def write_suite(path: Path) -> None:
    suite = build_suite()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(suite, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {sum(len(item['prompts']) for item in suite)} prompts -> {path}")


def run_suite(path: Path, results: Path) -> None:
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "replay_prompts.py"),
        str(path),
        str(results),
    ]
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=DEFAULT_SUITE_PATH)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS_PATH)
    parser.add_argument("--run", action="store_true", help="Run the generated suite with replay_prompts.py")
    args = parser.parse_args()

    if not (str(args.output).endswith(".json") and args.output.name):
        raise ValueError("--output must be a JSON path")

    write_suite(args.output)
    if args.run:
        print(f"running suite against live Wisp backend using {args.output}")
        run_suite(args.output, args.results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
