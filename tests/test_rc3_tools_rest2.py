"""rc3 tools-rest chunk 2: authoring check, read_file/list_dir bounds and TCC
messages, find_place category matching, text_to_speech argv. No real `say`, no
network, no user data."""
import subprocess

import pytest

from service.tools import tool_authoring as ta


@pytest.mark.parametrize("task", [
    "count words in the given text",
    "convert text to title case",
    "slugify some text",
])
def test_plain_text_utilities_are_not_redirected(task):
    redirect, _ = ta._capability_check("text_tool", "", task)
    assert redirect == ""


@pytest.mark.parametrize("task,expect", [
    ("read my text messages", "view_messages"),
    ("show recent iMessages", "view_messages"),
    ("find texts from Sam", "view_messages"),
    ("list my contacts", "list_contacts"),
    ("summarize my inbox", "view_emails"),
])
def test_personal_data_requests_are_still_redirected(task, expect):
    redirect, _ = ta._capability_check("helper", "", task)
    assert expect in redirect
