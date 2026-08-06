"""Filesystem scopes a skill may declare.

A skill is an untrusted folder someone dropped into ~/.moe/skills, and its
frontmatter says which directories its generated tools may reach. `parse_scopes`
is what refuses the over-broad ones. Pure path logic, no I/O.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from service.skills.scopes import parse_scopes

HOME = Path.home()


@pytest.mark.parametrize("raw", [
    "/", "/System", "/usr", "/bin", "/sbin", "/etc", "/var",
    "/Library", "/Applications",
])
def test_system_roots_refused(raw):
    scopes, err = parse_scopes(raw)
    assert scopes == []
    assert err


def test_whole_home_refused():
    scopes, err = parse_scopes(str(HOME))
    assert scopes == []
    assert "too broad" in err


def test_all_user_homes_refused():
    scopes, err = parse_scopes(str(HOME.parent))
    assert scopes == []
    assert err


def test_traversal_out_of_home_is_caught_after_resolution():
    """`~/../..` resolves to / — the check runs on the resolved path, so
    dressing up a refused root as a relative walk doesn't get past it."""
    scopes, err = parse_scopes("~/../..")
    assert scopes == []
    assert err


def test_a_scope_containing_a_system_root_is_refused():
    """A scope doesn't have to BE /usr to be too broad — one that contains it
    is just as reachable."""
    scopes, err = parse_scopes("/")
    assert scopes == []
    assert err


def test_ordinary_subfolder_accepted():
    scopes, err = parse_scopes("~/Downloads")
    assert err == ""
    assert scopes == [HOME / "Downloads"]


def test_inside_a_system_root_is_fine():
    """Being under /usr is allowed; being /usr is not."""
    scopes, err = parse_scopes("/usr/local/share/mytool")
    assert err == ""
    assert scopes == [Path("/usr/local/share/mytool")]


def test_list_of_scopes():
    scopes, err = parse_scopes(["~/Downloads", "~/Documents"])
    assert err == ""
    assert scopes == [HOME / "Downloads", HOME / "Documents"]


def test_one_bad_scope_rejects_the_whole_list():
    """Partial acceptance would silently grant less than the skill asked for
    while looking like success — better to fail the declaration outright."""
    scopes, err = parse_scopes(["~/Downloads", "/etc"])
    assert scopes == []
    assert err


@pytest.mark.parametrize("raw", [None, "", []])
def test_empty_means_no_scopes(raw):
    scopes, err = parse_scopes(raw)
    assert scopes == []
    assert err == ""


def test_wrong_type_is_an_error_not_a_crash():
    scopes, err = parse_scopes(42)
    assert scopes == []
    assert err
