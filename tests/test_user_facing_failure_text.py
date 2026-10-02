"""Failure text Wisp shows a person must not contain instructions to the model."""
import pytest

from service.user_text import user_facing_failure as clean


@pytest.mark.parametrize("raw,expected", [
    ("(no saved contact matches 'Sam', and it isn't a valid address. Ask the user for it — don't guess.)",
     "No saved contact matches 'Sam', and it isn't a valid address. Please tell me the address or number to use."),
    ("(no recipient — ask the user who this should go to)",
     "No recipient — please tell me who this should go to."),
    ("(no location given — ask the user which city, and do not guess one)",
     "No location given — please tell me which city."),
    ("(no email or number found for 'Sam' — do not guess one.)",
     "No email or number found for 'Sam'."),
])
def test_directives_to_the_model_become_a_request_to_the_person(raw, expected):
    assert clean(raw) == expected


@pytest.mark.parametrize("raw", [
    "(error: Wisp app is not connected; Calendar was not changed.)".replace("(", "").replace(")", ""),
    "Reminder creation was not verified. I won't retry automatically; check the reminder list before creating another.",
])
def test_facts_addressed_to_the_person_are_preserved(raw):
    cleaned = clean(raw)
    assert cleaned.startswith(raw[:1].upper()) and cleaned.rstrip(".") .endswith(raw.rstrip(".")[-20:])


@pytest.mark.parametrize("raw", ["", None, "   "])
def test_empty_input_never_raises(raw):
    assert clean(raw) in {"", "None"}


def test_no_directive_survives_in_the_output():
    for raw in ["(x — ask the user for it — don't guess)", "(ask the user which one, do not guess)",
                "(do not retry; ask the user for the number)"]:
        out = clean(raw).lower()
        assert "ask the user" not in out and "don't guess" not in out and "do not guess" not in out
