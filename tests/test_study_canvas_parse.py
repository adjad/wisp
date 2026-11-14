"""service/study/canvas_parse.py: captured Canvas page text -> courses and assignments."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from service.study import canvas_parse as cp

TZ = "America/Los_Angeles"
ZONE = ZoneInfo(TZ)


def at(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ZONE).timestamp()


NOW = at(2026, 11, 14, 15, 0)

DASHBOARD = """Account Dashboard Courses Calendar Inbox History Help
Skip To Content
Dashboard
Today
November 14
INTRODUCTION TO PROGRAMMING
INTRODUCTION TO PROGRAMMING ASSIGNMENT
Week 3 Reading: Functions
Week 3 Reading: Functions
10 pts
DUE: 11:59 PM
Missing
Assignment Week 3 Reading: Functions is not marked as done.
Tomorrow, November 15
STATISTICS
STATISTICS ASSIGNMENT
Worksheet 2
Due
5:00 PM

Monday, November 16
WRITING AND RHETORIC: VOICES AND GLOBAL PERSPECTIVES ASSIGNMENT
Upload personal essay here
STATISTICS CALENDAR EVENT
STAT 101-02 Office Hours with TA Sam
Zoom Online Meeting
STATISTICS ANNOUNCEMENT
Room change for Friday
STATISTICS ASSIGNMENT
HW2
20 pts   Due: 11:59 PM
Submitted
STATISTICS QUIZ
Chapter 6 Quiz
CS 101 LAB DISCUSSION
Introduce yourself
Tuesday, November 17
Show 2 completed items
My Grades
Introduction to Programming
87.5%
Statistics
No Grade
View Grades
"""


def by_title(page):
    return {a["title"]: a for a in page.assignments}


def test_dashboard_planner_list():
    page = cp.parse_page("https://canvas.example.edu/", "Dashboard", DASHBOARD, now=NOW, timezone=TZ)
    assert page.kind == "dashboard"
    items = by_title(page)
    assert set(items) == {"Week 3 Reading: Functions", "Worksheet 2", "Upload personal essay here", "HW2",
                          "Chapter 6 Quiz", "Introduce yourself"}
    reading = items["Week 3 Reading: Functions"]
    assert reading["due_ts"] == at(2026, 11, 14, 23, 59) and reading["all_day"] is False
    assert reading["kind"] == "reading" and reading["status"] == "missing" and reading["points"] == 10.0
    assert items["Worksheet 2"]["due_ts"] == at(2026, 11, 15, 17, 0)
    assert items["Upload personal essay here"]["due_ts"] == at(2026, 11, 16, 23, 59)
    assert items["HW2"]["status"] == "submitted" and items["HW2"]["points"] == 20.0
    assert items["Chapter 6 Quiz"]["kind"] == "quiz"
    assert items["Introduce yourself"]["kind"] == "discussion"  # DISCUSSION type label
    names = {c["name"]: c for c in page.courses}
    assert "Introduction to Programming" in names and "Statistics" in names
    assert "Writing and Rhetoric: Voices and Global Perspectives" in names
    assert names["Introduction to Programming"]["score"] == 87.5
    assert names["Statistics"]["score"] is None and names["Statistics"]["grade"] is None
    # No announcement, calendar event or office hours became an assignment.
    assert not any("Office Hours" in t or "Room change" in t for t in items)


def test_dashboard_ids_are_stable_and_name_keyed():
    one = cp.parse_page("https://canvas.example.edu/", "Dashboard", DASHBOARD, now=NOW, timezone=TZ)
    two = cp.parse_page("https://canvas.example.edu/", "Dashboard", DASHBOARD.upper().replace("MY GRADES", "My Grades"),
                        now=NOW, timezone=TZ)
    stats = next(c for c in one.courses if c["name"] == "Statistics")
    assert stats["id"].startswith("canvas:") and len(stats["id"]) == len("canvas:") + 12
    assert stats["merge_key"] == "statistics"
    # Same course, same id, whatever the case on the page.
    assert stats["id"] in {c["id"] for c in two.courses}
    for a in one.assignments:
        assert a["course_id"] in {c["id"] for c in one.courses}
        assert a["source"] == "canvas" and a["sample"] is False and a["details"] == ""


def test_mixed_case_label_for_a_known_course():
    text = """Dashboard
Today
STATISTICS
STATISTICS ASSIGNMENT
HW2
Tomorrow, November 15
Statistics Quiz
Chapter 6 Quiz
Final Quiz Review Notes
"""
    page = cp.parse_page("https://canvas.example.edu/", "Dashboard", text, now=NOW, timezone=TZ)
    items = by_title(page)
    assert set(items) == {"HW2", "Chapter 6 Quiz"}
    assert items["Chapter 6 Quiz"]["kind"] == "quiz"
    assert items["Chapter 6 Quiz"]["due_ts"] == at(2026, 11, 15, 23, 59)


def test_display_name_title_cases_upper_labels():
    assert cp.display_name("INTRODUCTION TO PROGRAMMING") == "Introduction to Programming"
    assert cp.display_name("CS 101 INTRO TO DATA STRUCTURES") == "CS 101 Intro to Data Structures"
    assert cp.display_name("STAT 101 HW2 REVIEW") == "STAT 101 HW2 Review"
    assert cp.display_name("UNIV WRITING 2") == "UNIV Writing 2"
    assert cp.display_name("Already Mixed Case of Things") == "Already Mixed Case of Things"


def test_dashboard_year_boundary_resolves_to_nearest_date():
    now = at(2026, 12, 30, 9, 0)
    text = """Dashboard
Thursday, December 31
LINEAR ALGEBRA ASSIGNMENT
Problem Set 9
Saturday, January 2
LINEAR ALGEBRA ASSIGNMENT
Problem Set 10
Due: 9:00 AM
"""
    page = cp.parse_page("https://school.instructure.com/", "Dashboard", text, now=now, timezone=TZ)
    items = by_title(page)
    assert items["Problem Set 9"]["due_ts"] == at(2026, 12, 31, 23, 59)
    assert items["Problem Set 10"]["due_ts"] == at(2027, 1, 2, 9, 0)
    assert page.courses[0]["name"] == "Linear Algebra"


def test_parse_due_formats():
    assert cp.parse_due("Nov 14 by 11:59pm", now=NOW, timezone=TZ) == at(2026, 11, 14, 23, 59)
    assert cp.parse_due("Nov 16 at 11:59pm", now=NOW, timezone=TZ) == at(2026, 11, 16, 23, 59)
    assert cp.parse_due("November 16", now=NOW, timezone=TZ) == at(2026, 11, 16, 23, 59)
    assert cp.parse_due("Mon Nov 16, 2026 11:59pm", now=NOW, timezone=TZ) == at(2026, 11, 16, 23, 59)
    assert cp.parse_due("Sept 3 at 9am", now=NOW, timezone=TZ) == at(2026, 9, 3, 9, 0)
    assert cp.parse_due("Dec 2 at 12pm", now=NOW, timezone=TZ) == at(2026, 12, 2, 12, 0)
    assert cp.parse_due("Jan 5 at 8:30am", now=NOW, timezone=TZ) == at(2027, 1, 5, 8, 30)
    assert cp.parse_due("no date here", now=NOW, timezone=TZ) is None


ALL_GRADES = """Account Dashboard Courses Calendar Inbox History Help
Grades
Courses I'm Taking
Course
Grade
Introduction to Programming
87.5%
Statistics
no grade
Writing and Rhetoric
A- (90.5%)
Ocean Sciences
--
"""


def test_all_courses_grades():
    page = cp.parse_page("https://canvas.example.edu/grades", "Grades", ALL_GRADES, now=NOW, timezone=TZ)
    assert page.kind == "grades" and page.assignments == []
    names = {c["name"]: c for c in page.courses}
    assert set(names) == {"Introduction to Programming", "Statistics", "Writing and Rhetoric",
                          "Ocean Sciences"}
    assert names["Introduction to Programming"]["score"] == 87.5
    assert names["Writing and Rhetoric"]["score"] == 90.5
    assert names["Writing and Rhetoric"]["grade"] == "A-"
    assert names["Statistics"]["score"] is None
    # A name-keyed course here matches the Dashboard's id for the same course.
    dash = cp.parse_page("https://canvas.example.edu/", "Dashboard", DASHBOARD, now=NOW, timezone=TZ)
    dash_ids = {c["name"]: c["id"] for c in dash.courses}
    assert names["Statistics"]["id"] == dash_ids["Statistics"]


COURSE_GRADES = """Account Dashboard Courses Calendar Inbox History Help
CSE-20-01
Grades
Grades for Jordan Example
Course
CS 101: Introduction to Programming
Arrange By
Due Date
Apply
Name
Due
Submitted
Status
Score
Out of
Lab 4: Loops
Labs
Nov 7 by 11:59pm
Nov 6 at 10:12pm
18
/ 20
Click to test a different score
Lab 5: Lists
Labs
Nov 14 by 11:59pm
missing
-
/ 20
Midterm Exam
Exams
Nov 20 by 2pm
-
/ 100
Essay: Why Python
Nov 2 by 11:59pm
Oct 31 at 4:00pm
late
45
/ 50
Labs
90%
18.00 / 20.00
Total: 87.5% (B+)
182.28 / 200.00
"""


def test_one_course_grades_table():
    page = cp.parse_page("https://canvas.example.edu/courses/12345/grades",
                         "Grades for Jordan Example: CS 101: Introduction to Programming",
                         COURSE_GRADES, now=NOW, timezone=TZ)
    assert page.kind == "course_grades"
    assert len(page.courses) == 1
    course = page.courses[0]
    assert course["id"] == "canvas:12345" and course["code"] == "CS 101"
    assert course["name"] == "CS 101: Introduction to Programming"
    assert course["score"] == 87.5 and course["grade"] == "B+"
    assert course["url"] == "https://canvas.example.edu/courses/12345"
    items = by_title(page)
    assert set(items) == {"Lab 4: Loops", "Lab 5: Lists", "Midterm Exam", "Essay: Why Python"}
    lab4 = items["Lab 4: Loops"]
    assert lab4["score"] == 18.0 and lab4["points"] == 20.0 and lab4["status"] == "graded"
    assert lab4["due_ts"] == at(2026, 11, 7, 23, 59)
    lab5 = items["Lab 5: Lists"]
    assert lab5["status"] == "missing" and lab5["score"] is None and lab5["points"] == 20.0
    assert items["Midterm Exam"]["kind"] == "exam" and items["Midterm Exam"]["due_ts"] == at(2026, 11, 20, 14, 0)
    assert items["Midterm Exam"]["status"] == "upcoming"
    essay = items["Essay: Why Python"]
    assert essay["status"] == "late" and essay["kind"] == "project" and essay["score"] == 45.0
    for a in page.assignments:
        assert a["id"].startswith("canvas:12345:") and a["course_id"] == "canvas:12345"
    # Stable ids across re-reads.
    again = cp.parse_page("https://canvas.example.edu/courses/12345/grades", "Grades for Jordan Example: CS 101",
                          COURSE_GRADES, now=NOW + 3600, timezone=TZ)
    assert {a["id"] for a in again.assignments} == {a["id"] for a in page.assignments}


ASSIGNMENTS = """Account Dashboard Courses Calendar Inbox History Help
Search for Assignment
Show By Date
Show By Type
Upcoming Assignments
Assignment
Lab 6: Dictionaries
Due Nov 21 at 11:59pm
-/20 pts
Quiz
Quiz 3
Not available until Nov 18 at 12am
Due
Nov 19 at 10am
10 pts
Undated Assignments
Final Project Proposal
-/30 pts
Past Assignments
Lab 4: Loops
Due Nov 7 at 11:59pm
18/20 pts
Lab 5: Lists
Due Nov 14 at 11:59pm
Missing
-/20 pts
"""


def test_assignments_list():
    page = cp.parse_page("https://school.instructure.com/courses/777/assignments",
                         "Assignments: Data Structures", ASSIGNMENTS, now=NOW, timezone=TZ)
    assert page.kind == "assignments"
    assert page.courses[0]["id"] == "canvas:777" and page.courses[0]["name"] == "Data Structures"
    items = by_title(page)
    assert set(items) == {"Lab 6: Dictionaries", "Quiz 3", "Final Project Proposal", "Lab 4: Loops", "Lab 5: Lists"}
    assert items["Lab 6: Dictionaries"]["due_ts"] == at(2026, 11, 21, 23, 59)
    assert items["Lab 6: Dictionaries"]["points"] == 20.0 and items["Lab 6: Dictionaries"]["score"] is None
    assert items["Quiz 3"]["kind"] == "quiz" and items["Quiz 3"]["due_ts"] == at(2026, 11, 19, 10, 0)
    assert items["Quiz 3"]["points"] == 10.0
    assert items["Final Project Proposal"]["due_ts"] is None and items["Final Project Proposal"]["kind"] == "project"
    assert items["Lab 4: Loops"]["status"] == "graded" and items["Lab 4: Loops"]["score"] == 18.0
    assert items["Lab 5: Lists"]["status"] == "missing"


def test_course_home_coming_up_and_dashboard_todo():
    home = """Account Dashboard Courses Calendar Inbox History Help
Home
Welcome to the course
Coming Up
View Calendar
Lab 6: Dictionaries
20 points
Nov 21 at 11:59pm
Reading Quiz 4
5 points • Nov 18 at 9am
"""
    page = cp.parse_page("https://canvas.example.edu/courses/777", "Course Home: Data Structures", home,
                         now=NOW, timezone=TZ)
    assert page.kind == "calendar"
    items = by_title(page)
    assert items["Lab 6: Dictionaries"]["due_ts"] == at(2026, 11, 21, 23, 59)
    assert items["Lab 6: Dictionaries"]["points"] == 20.0
    assert items["Reading Quiz 4"]["kind"] == "quiz" and items["Reading Quiz 4"]["points"] == 5.0
    assert all(a["course_id"] == "canvas:777" for a in page.assignments)

    cards = """Dashboard
Introduction to Programming
CSE-20-01
Fall 2026
To Do
Turn in Lab 6: Dictionaries
CS 101
20 points • Nov 21 at 11:59pm
"""
    dash = cp.parse_page("https://canvas.example.edu/", "Dashboard", cards, now=NOW, timezone=TZ)
    assert dash.kind == "dashboard"
    assert [a["title"] for a in dash.assignments] == ["Lab 6: Dictionaries"]
    assert dash.assignments[0]["points"] == 20.0


def test_unrecognised_and_non_school_pages():
    assert cp.parse_page("https://example.com/", "Example", "Hello", now=NOW, timezone=TZ).kind == "other"
    assert cp.parse_page("http://canvas.example.edu/grades", "Grades", ALL_GRADES, now=NOW, timezone=TZ).kind == "other"
    page = cp.parse_page("https://www.example.edu/admissions", "Admissions", "Apply now\nVisit campus",
                         now=NOW, timezone=TZ)
    assert page == cp.ParsedPage("other", [], [])
    # Another LMS is left to the model-assisted path.
    assert cp.parse_page("https://classroom.google.com/u/0/h", "Classes", "Stream", now=NOW, timezone=TZ).kind == "other"
    # A Canvas dashboard with nothing planned is still recognised.
    empty = cp.parse_page("https://school.instructure.com/", "Dashboard", "Dashboard\nNothing Planned Yet",
                          now=NOW, timezone=TZ)
    assert empty.kind == "dashboard" and empty.assignments == []


@pytest.mark.parametrize("url, ok", [
    ("https://school.instructure.com/courses/1", True),
    ("https://canvas.example.edu/", True),
    ("https://canvas.example.org/", True),
    ("https://classroom.google.com/u/0/h", True),
    ("https://district.schoology.com/home", True),
    ("https://x.powerschool.com/", True),
    ("https://learn.blackboard.com/", True),
    ("https://uni.brightspace.com/d2l/home", True),
    ("https://uni.d2l.com/", True),
    ("https://moodle.example.org/", True),
    ("https://us.edstem.org/courses", True),
    ("https://www.gradescope.com/courses/1", True),
    ("https://www.example.edu/", True),
    ("https://portal.springfield.k12.or.us/", True),
    ("http://canvas.example.edu/", False),
    ("https://example.com/", False),
    ("https://instructure.com.evil.net/", False),
    ("https://notedu.com/", False),
    ("not a url", False),
    ("", False),
])
def test_is_school_url(url, ok):
    assert cp.is_school_url(url) is ok


def test_kind_mapping():
    assert cp._kind("Midterm 2") == "exam"
    assert cp._kind("Final Exam Review") == "exam"
    assert cp._kind("Chapter 3 Test") == "quiz"
    assert cp._kind("Research Paper Draft") == "project"
    assert cp._kind("Week 2 Reading") == "reading"
    assert cp._kind("Weekly Discussion 4") == "discussion"
    assert cp._kind("Lab 1") == "assignment"


def test_this_file_is_in_the_simulation_manifest():
    from pathlib import Path
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_study_canvas_parse.py"' in manifest
