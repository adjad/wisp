"""0.5.0 release blocker: whose question is it, on a model-led turn?

A request whose owner the classifier could not place ("Did Sam's package
arrive today?") is UNKNOWN. The rules route answers that with a question and
offers no web tool meanwhile. A model-led turn has no such step, and with a
time word in the request it left every web tool open on the first message of a
chat: the real-model run of 2026-11-12 searched "Mom's operating hours on
Sunday". Worse, a kin word with no 's read as a public lookup ("How is Grandma
doing today?").

Now, on a model-led turn:
- UNKNOWN offers no web tool, unless the user explicitly asked for a search;
- an 's that is plainly public keeps one thing, a search of the user's own
  words ("Who won Sunday's game?", "What's Apple's stock price today?");
- a question about someone in the user's life is private, whatever time word
  it carries, with no ASCII or length gate on that check.
Nothing here opens anything that was closed.
"""
from __future__ import annotations

import pytest

from service import model_only as mo
from service.router.router import routing_guard_contract
from service.router.web_request import Provenance, classify
from tests.test_model_led_integration import (  # noqa: F401
    DISCOVERY_TOOL, Approval, _web_tools, call, run, synthetic)

WEB = {"web_search", "web_fetch", "http_request"}


def _model_led_forbidden(prompt):
    return set(routing_guard_contract(prompt, model_led=True)[0])


# The reviewer's inputs, plus neighbours: no web tool on a model-led turn.
NO_WEB = [
    "What time is Mom's flight tomorrow?", "Did Sam's package arrive today?", "What's Dana's address now?",
    "is grandma's place open this weekend", "Is the doctor's office open on Monday?",
    "How is Grandma doing today?", "Is Mom home yet?", "Where is Dad right now?", "Is grandma coming this weekend?",
    "Is Mom's open on Sunday?", "When does Mom's close tonight?", "What are Dana's plans on Saturday?",
    "What's Sarah's new job this year?", "Did Dana's team win today?", "what about Dana's roadmap?",
    "latest news from Monday's product team", "any news from mom", "What did the landlord say about the lease?",
    # No ASCII or length gate on the tightening path.
    "Is Mamá's open on Sunday?", "Is Mom's open on Sunday? \U0001F642", "Is Mom’s open on Sunday?",
    "Is Mom's open on Sunday or should we maybe go on Monday or Tuesday or some other day this week instead?",
    # An 's behind an apostrophe lookalike is not readable by any ownership check.
    "Is Trader Joeʹs open today?", "What are Costco′s hours?", "What are Costco`s hours?",
    "What are Costco´s hours?",
]


@pytest.mark.parametrize("prompt", NO_WEB)
def test_a_model_led_turn_offers_no_web_tool(prompt):
    request = classify(prompt)
    assert not request.allowed and request.query is None and not request.public_possessive
    assert WEB <= _model_led_forbidden(prompt), (prompt, request.provenance)


@pytest.mark.parametrize("prompt", [
    "How is Grandma doing today?", "Is Mom home yet?", "What time is Mom's flight tomorrow?",
    "Is the doctor's office open on Monday?", "Is Mamá's open on Sunday?", "Is Trader Joeʹs open today?",
])
def test_a_question_about_someone_of_the_users_own_is_private(prompt):
    assert classify(prompt).provenance == Provenance.PRIVATE


@pytest.mark.parametrize("prompt", ["Did Sam's package arrive today?", "What's Dana's address now?",
                                    "What are Dana's plans on Saturday?"])
def test_an_owner_nobody_can_place_stays_unknown_and_closed(prompt):
    request = classify(prompt)
    assert request.provenance == Provenance.UNKNOWN and not request.private
    assert WEB <= _model_led_forbidden(prompt)
    assert not mo.public_owner_request(prompt)


# Plainly public, with an 's: a search of the user's own words and nothing else.
SEARCH_ONLY = [
    "Who won Sunday's game?", "What's Apple's stock price today?", "what's Apple's stock doing today",
    "What is Tesla's market cap right now?", "What's McDonald's new burger this month?",
    "What's the Warriors' score tonight?", "What's Dad's Army showing tonight?",
    "What's Dr Pepper's new flavor this year?", "what are these stocks' P/E ratios today",
    "What's Google's latest phone this year?", "When is Mother's Day this year?",
    "What are Costco's hours?", "Is Trader Joe's open on Thanksgiving?", "Is Papa John's open today?",
]


@pytest.mark.parametrize("prompt", SEARCH_ONLY)
def test_a_plainly_public_owner_keeps_a_words_only_search(prompt):
    request = classify(prompt)
    assert request.provenance == Provenance.PRIVATE and request.public_possessive
    assert not request.allowed and request.query is None
    forbidden = _model_led_forbidden(prompt)
    assert "web_search" not in forbidden and {"web_fetch", "http_request"} <= forbidden
    assert WEB <= set(routing_guard_contract(prompt)[0])          # the rules route opens nothing


# No 's and no person: unchanged.
STILL_OPEN = [
    "Is Costco open on Sunday?", "who is the president?", "what's the weather tomorrow", "What's the news today?",
    "When is Thanksgiving this year?", "What's today's date", "Will it rain tomorrow?",
    "How is the market doing today?", "what is the price of bitcoin", "who won the world series this year",
]


@pytest.mark.parametrize("prompt", STILL_OPEN)
def test_public_questions_with_no_owner_are_unchanged(prompt):
    assert classify(prompt).provenance == Provenance.EXTERNAL
    assert "web_search" not in _model_led_forbidden(prompt)


# An explicit request to search stays exactly as it was.
@pytest.mark.parametrize("prompt", [
    "search the web for Mom's flight status", "search the web for Dana's bakery",
    "Search the web for Mira Vale's tour dates and add them to my calendar", "search the web for Sarah Connor news",
])
def test_an_explicit_search_is_untouched(prompt):
    assert classify(prompt).explicit
    assert "web_search" not in _model_led_forbidden(prompt)


# Someone named only as the recipient is not the subject.
@pytest.mark.parametrize("prompt", ["text mom the bitcoin price right now",
                                    "email Mom the latest Zorvia news; don't send it"])
def test_a_recipient_does_not_make_the_lookup_private(prompt):
    assert classify(prompt).provenance == Provenance.EXTERNAL


def test_kin_words_inside_public_names_are_not_people():
    for prompt in ("When is Mother's Day this year?", "Is Papa John's open today?", "When is Father's Day?",
                   "What is Uncle Sam's tax deadline this year?"):
        assert not mo.personal_reference(prompt) and not mo.personal_owner(prompt), prompt
    for prompt in ("How is Grandma doing today?", "is MOM home", "Where's my sister's flight"):
        assert mo.personal_reference(prompt) or mo.personal_owner(prompt), prompt


def test_a_bare_first_name_is_not_recognised_and_routing_loads_no_profile(monkeypatch):
    # Stated plainly: "Is Sarah coming tomorrow?" cannot be told from a shop
    # or a team by its words, and the classifier may not read the know-you
    # profile for every request (tests/test_live_corpus.py: routing loads no
    # saved context in test mode or after an opt-out). An 's is different:
    # the owner check for it predates this change.
    from service.memory import profile
    loads = []
    monkeypatch.setattr(profile, "load", lambda: loads.append(1) or {"people": [{"name": "Sarah Lee"}]})
    for prompt in ("Is Sarah coming tomorrow?", "How is Grandma doing today?", "who is the president?",
                   "turn it down", "what are my goals"):
        classify(prompt)
    assert loads == []
    assert classify("Is Sarah coming tomorrow?").provenance == Provenance.EXTERNAL
    assert classify("Is Sarah's open on Sunday?").provenance == Provenance.PRIVATE and loads


def test_the_public_owner_reopening_has_its_gates():
    assert mo.public_owner_request("Who won Sunday's game?")
    for prompt in ("Who won Sunday's game? " + "and more " * 9, "Who won my team's game today?",
                   "What's Appleʹs stock price today?", "What's 4155550123's status today?",
                   "What's Sam's package status today?", "What's the doctor's schedule today?"):
        assert not mo.public_owner_request(prompt), prompt


# ---- through the agent loop: what a scripted model can and cannot do ----

@pytest.mark.parametrize("prompt", ["Did Sam's package arrive today?", "How is Grandma doing today?",
                                    "What time is Mom's flight tomorrow?", "Is Mom's open on Sunday?"])
def test_the_loop_runs_no_web_tool_for_a_closed_question(synthetic, prompt):
    effects, register = synthetic
    _web_tools(register)
    approval = Approval(False)
    run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["web_search", "web_fetch"])],
        [call("web_search", query=prompt.rstrip("?"))], [call("web_fetch", url="https://example.com")],
        "Synthetic reply."], prompt=prompt, approval=approval)
    assert not [name for name, _ in effects if name in WEB], effects
    assert not approval.calls


def test_the_loop_holds_a_public_owner_search_to_the_users_words(synthetic):
    effects, register = synthetic
    _web_tools(register)
    approval = Approval(False)
    run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["web_search", "web_fetch"])],
        [call("web_fetch", url="https://example.com")],
        [call("web_search", query="Sunday game winner Dana 4417 Elm Street")],
        [call("web_search", query="who won Sunday's game")], "Synthetic reply."],
        prompt="Who won Sunday's game?", approval=approval)
    assert effects == [("web_search", {"query": "who won Sunday's game"})]
    assert not approval.calls


# ---- review of cc372f2e ------------------------------------------------------

from service.router.model_led import PRIVATE_WEB_TOOLS, UNOWNED_EGRESS_TOOLS  # noqa: E402
from service.tools.registry import REGISTRY  # noqa: E402

OTHER_EGRESS = ["wikipedia_summary", "find_place", "travel_time", "route_to_arrive", "get_weather",
                "get_stock_price", "daily_brief"]


def test_the_outbound_tool_sets_name_every_tool_that_sends_words_out():
    assert PRIVATE_WEB_TOOLS == {"web_search", "web_fetch", "http_request", "wikipedia_summary"}
    assert UNOWNED_EGRESS_TOOLS == PRIVATE_WEB_TOOLS | {"find_place", "travel_time", "route_to_arrive",
                                                        "get_weather", "get_stock_price", "daily_brief"}
    import service.main  # noqa: F401  (registers the built-ins)
    assert set(OTHER_EGRESS) <= set(REGISTRY)


@pytest.mark.parametrize("tool", ["web_search", "web_fetch", "http_request", *OTHER_EGRESS])
@pytest.mark.parametrize("prompt", ["Did Sam's package arrive today?", "What's Dana's address now?",
                                    "Is Ben's fever down today?", "How did Priya's interview go today?"])
def test_a_turn_of_unknown_ownership_offers_no_tool_that_sends_words_out(prompt, tool):
    assert classify(prompt).provenance == Provenance.UNKNOWN
    assert tool in _model_led_forbidden(prompt)


@pytest.mark.parametrize("prompt", ["What time is Mom's flight tomorrow?", "How is Grandma doing today?",
                                    "what's Mom's number", "What are Costco's hours?"])
def test_a_private_turn_withholds_wikipedia_and_keeps_maps_weather_and_prices(prompt):
    forbidden = _model_led_forbidden(prompt)
    assert {"web_fetch", "http_request", "wikipedia_summary"} <= forbidden
    # Known limit, on purpose: "drive time to Mom's" needs the maps tools, so a
    # private turn still offers place, route, weather and price lookups.
    assert not {"find_place", "travel_time", "route_to_arrive", "get_weather", "get_stock_price",
                "daily_brief"} & forbidden


def _egress_tools(register):
    _web_tools(register)
    for name in OTHER_EGRESS:
        register(name, "web_tools", "web_read", parameters={"type": "object", "properties": {
            "query": {"type": "string"}, "topic": {"type": "string"}, "location": {"type": "string"}}})


@pytest.mark.parametrize("history", [(), "private"])
@pytest.mark.parametrize("tool", ["web_search", "web_fetch", "http_request", *OTHER_EGRESS])
def test_the_loop_runs_none_of_them_on_a_turn_of_unknown_ownership(synthetic, tool, history):
    from tests.test_model_led_integration import PRIVATE_HISTORY
    effects, register = synthetic
    _egress_tools(register)
    approval = Approval(False)
    args = {"url": "https://example.com"} if tool in ("web_fetch", "http_request") else {"query": "Dana address"}
    run([[call(DISCOVERY_TOOL, families=["public_web"], tools=[tool])], [call(tool, **args)], "Synthetic reply."],
        prompt="What's Dana's address now?", history=PRIVATE_HISTORY if history else (), approval=approval)
    assert not effects and not approval.calls


@pytest.mark.parametrize("history", [(), "private"])
def test_the_loop_refuses_wikipedia_on_a_private_turn_with_or_without_history(synthetic, history):
    from tests.test_model_led_integration import PRIVATE_HISTORY
    effects, register = synthetic
    _egress_tools(register)
    approval = Approval(False)
    run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["wikipedia_summary", "web_search"])],
        [call("wikipedia_summary", topic="Grandma")], [call("web_search", query="Grandma health today")],
        "Synthetic reply."], prompt="How is Grandma doing today?",
        history=PRIVATE_HISTORY if history else (), approval=approval)
    assert not effects and not approval.calls


def test_with_private_history_a_public_owner_search_is_still_words_only(synthetic):
    from tests.test_model_led_integration import PRIVATE_HISTORY
    effects, register = synthetic
    _egress_tools(register)
    approval = Approval(False)
    run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["web_search", "web_fetch", "wikipedia_summary"])],
        [call("web_search", query="Apple stock price today gate code 4471")],
        [call("wikipedia_summary", topic="Apple gate code 4471")],
        [call("web_search", query="Apple stock price today")], "Synthetic reply."],
        prompt="What's Apple's stock price today?", history=PRIVATE_HISTORY, approval=approval)
    assert effects == [("web_search", {"query": "Apple stock price today"})]


# A kin word makes a request private only when it is whom the question is about.
KIN_ABOUT = [
    "How is Grandma doing today?", "Is Mom home yet?", "Where is Dad right now?", "Did Dad call?",
    "Is my sister coming", "is my mom ok", "any news about our landlord today", "What did Mom say?",
    "Did the hospital call Mom today?", "Will they let Dad go home today?", "any news from mom",
    "Tell me if Mom is online right now", "do you know when Dad lands", "What time is Mom's flight tomorrow?",
    "Has grandma arrived yet?", "When does Dad land tonight?",
]
# The delta review of dce3fd9b: a relative in subject position, whatever follows.
KIN_ABOUT += [
    "When will Dad be home?", "Will Mom be back tonight?", "Is Mom mad at me?", "Is Dad upset?",
    "Has Mom eaten today?", "Did Mom take her meds today?", "Did Dad pay the rent?", "Did Grandma fall again?",
    "Is Grandma lonely?", "Why is Mom upset?", "Who is Dad with?", "Mom home yet?", "Dad ok?", "Is Mom OK",
    "Tell me if Mom's online right now", "Check if Dad's online", "Show me if Mom's still online",
    "any news from Mom today?", "any word from Dad yet?", "Can you tell if Mom is home yet?",
]
KIN_MERELY_NAMED = [
    # A capitalised word after the kin word, as typed, makes it a name.
    "Is Sister Rosetta Tharpe touring this year?", "Did Daddy Yankee tour this year?",
    "Is Brother HL-L2350DW in stock today?",
    "husband pillow reviews 2026", "best gifts for dad", "landlord tenant law changes this year",
    "Daddy Yankee tour dates this year", "Brother HL-L2350DW driver", "Sister Wives new season",
    "mother of the bride dresses", "Is Papa Roach touring this year?", "Is Son Heung-min playing today?",
    "Is Brother printer support open today?", "best gift ideas for mom this year", "how to be a good dad",
    "husband and wife comedy movies 2026", "uncle sam poster history", "what is a roommate agreement",
]


@pytest.mark.parametrize("prompt", KIN_ABOUT)
def test_a_question_about_kin_is_private(prompt):
    assert mo.personal_reference(prompt) or mo.personal_owner(prompt), prompt
    assert classify(prompt).provenance == Provenance.PRIVATE
    assert {"web_search", "web_fetch", "wikipedia_summary"} <= _model_led_forbidden(prompt)


@pytest.mark.parametrize("prompt", KIN_MERELY_NAMED)
def test_a_kin_word_that_merely_appears_leaves_the_request_public(prompt):
    assert not mo.personal_reference(prompt) and not mo.personal_owner(prompt), prompt
    assert classify(prompt).provenance == Provenance.EXTERNAL
    assert "web_search" not in _model_led_forbidden(prompt)


def test_dads_army_is_a_show_and_keeps_a_words_only_search():
    request = classify("What's Dad's Army showing tonight?")
    assert request.public_possessive and "web_search" not in _model_led_forbidden("What's Dad's Army showing tonight?")


def test_online_as_a_state_is_not_a_request_to_search():
    from service.router.web_request import _explicit
    for prompt in ("Tell me if Mom is online right now", "show me who is online", "check if the site is back online"):
        assert not _explicit(prompt), prompt
    assert classify("Tell me if Mom is online right now").provenance == Provenance.PRIVATE
    for prompt in ("search the web for Dana's bakery", "tell me the news online", "look up Costco hours online",
                   "lookup current information about cities not online; save it in Notes"):
        assert _explicit(prompt.split(";")[0]), prompt


def test_a_recipient_is_skipped_only_at_the_start_of_the_request():
    for prompt in ("Did the hospital call Mom today?", "Will they let Dad go home today?",
                   "did anyone text mom about dinner today"):
        assert classify(prompt).provenance == Provenance.PRIVATE, prompt
    for prompt in ("text mom the bitcoin price right now", "please email Mom the latest Zorvia news; don't send it",
                   "can you text dad the weather today"):
        assert classify(prompt).provenance == Provenance.EXTERNAL, prompt


# An unknown name reopens a search only with a public noun.
@pytest.mark.parametrize("prompt", [
    "Is Ben's fever down today?", "How did Priya's interview go today?", "How's Maya's recovery going today?",
    "What's Sam's latest test result today?", "Is Dana's new baby home today?", "Does McDonald's have wifi today?",
    "What's Ben's status today?", "Where is Priya's team meeting today?",
])
def test_an_unknown_name_with_no_public_noun_stays_closed(prompt):
    assert not mo.public_owner_request(prompt)
    assert not classify(prompt).public_possessive
    forbidden = _model_led_forbidden(prompt)
    assert {"web_search", "web_fetch", "wikipedia_summary"} <= forbidden, prompt
    if classify(prompt).provenance == Provenance.UNKNOWN:
        assert set(UNOWNED_EGRESS_TOOLS) & set(REGISTRY) <= forbidden


def test_papa_johns_before_and_after_on_purpose(monkeypatch):
    """rc2 (e27dcd1b) classed "Is Papa John's open today?" UNKNOWN and a
    model-led turn left every web tool open for it. bb539d87 closed it
    entirely, because "papa" is a kin word: an over-block. It now gets what a
    pizza chain's hours should: one search of the user's own words, no fetch."""
    prompt = "Is Papa John's open today?"
    request = classify(prompt)
    assert request.provenance == Provenance.PRIVATE and request.public_possessive
    forbidden = _model_led_forbidden(prompt)
    assert "web_search" not in forbidden and {"web_fetch", "http_request", "wikipedia_summary"} <= forbidden
    # The known-person check still runs on the owner's words.
    monkeypatch.setattr(mo, "_known_people_tokens", lambda: {"john", "papa"})
    assert not mo.public_possessive_question(prompt)
    assert "web_search" in _model_led_forbidden(prompt)


@pytest.mark.parametrize("mark", ["\ua78c", "\u02bd", "\u02c8", "\u2035", "\u02b9", "\u2032", "\u00b4", "`"])
def test_any_apostrophe_lookalike_closes_the_request(mark):
    prompt = f"Is Trader Joe{mark}s open today?"
    assert mo.lookalike_possessive(prompt)
    assert classify(prompt).provenance == Provenance.PRIVATE
    assert {"web_search", "web_fetch"} <= _model_led_forbidden(prompt)


def test_an_accented_word_ending_in_s_is_not_a_lookalike():
    for prompt in ("best caf\u00e9s in Paris today", "Beyonc\u00e9s", "pi\u00f1atas for sale today"):
        assert not mo.lookalike_possessive(prompt), prompt


def test_one_year_does_not_excuse_another_long_number():
    assert mo.public_owner_request("What's Acme's 2026 price today?")
    assert not mo.public_owner_request("What's Acme's 2026 price for order 4155550123 today?")
    assert not mo.public_owner_request("What's Acme's price for 20261 today?")


# ---- delta review of dce3fd9b ------------------------------------------------

@pytest.mark.parametrize("prompt", ["When will Dad be home?", "Is Mom mad at me?", "Did Grandma fall again?",
                                    "Mom home yet?", "Tell me if Mom's online right now",
                                    "any news from Mom today?", "Can you tell if Mom is home yet?"])
@pytest.mark.parametrize("history", [(), "private"])
def test_the_loop_runs_no_web_tool_for_a_question_about_a_relative(synthetic, prompt, history):
    from tests.test_model_led_integration import PRIVATE_HISTORY
    effects, register = synthetic
    _egress_tools(register)
    approval = Approval(False)
    run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["web_search", "web_fetch", "wikipedia_summary"])],
        [call("web_search", query=prompt.rstrip("?"))], [call("wikipedia_summary", topic="Mom")],
        [call("web_fetch", url="https://example.com")], "Synthetic reply."],
        prompt=prompt, history=PRIVATE_HISTORY if history else (), approval=approval)
    assert not [name for name, _ in effects if name in PRIVATE_WEB_TOOLS], effects
    assert not approval.calls


def test_a_question_whether_someone_is_online_is_not_explicit():
    from service.router.web_request import _explicit
    for prompt in ("Tell me if Mom's online right now", "Check if Dad's online", "Show me if Mom's still online",
                   "Tell me if Mom is online right now"):
        assert not _explicit(prompt), prompt
        request = classify(prompt)
        assert request.provenance == Provenance.PRIVATE and not request.explicit_unknown_owner
        assert set(UNOWNED_EGRESS_TOOLS) & set(REGISTRY) - {"find_place", "travel_time", "route_to_arrive",
                                                             "get_weather", "get_stock_price", "daily_brief"} \
            <= _model_led_forbidden(prompt)
    # A request that names the web as the place to look is still explicit.
    for prompt in ("search the web for Dana's bakery", "look up Dana's bakery online", "google Mom's bakery hours"):
        assert _explicit(prompt), prompt


def test_an_explicit_search_of_unknown_ownership_keeps_its_search_and_only_that_path(synthetic):
    # "search the web for ..." is the user asking; it stays as rc2 had it.
    prompt = "search the web for Dana's bakery"
    request = classify(prompt)
    assert request.explicit and request.provenance == Provenance.UNKNOWN
    assert "web_search" not in _model_led_forbidden(prompt)
    effects, register = synthetic
    _egress_tools(register)
    run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["web_search"])],
        [call("web_search", query="Dana's bakery")], "Synthetic reply."], prompt=prompt, approval=Approval(False))
    assert [name for name, _ in effects] == ["web_search"]


def test_a_capitalised_ordinary_word_after_a_relative_is_not_a_name():
    for prompt in ("Is Mom OK", "Is Dad Home yet", "Did Mom Call"):
        assert mo.personal_reference(prompt), prompt
    for prompt in ("Is Sister Rosetta Tharpe touring this year?", "Is Uncle Kracker touring this year?"):
        assert not mo.personal_reference(prompt), prompt


# Over-blocks the subject rule accepts, pinned so a change to them is a choice.
@pytest.mark.parametrize("prompt", ["Are mom jeans in style this year?", "Is dad rock still popular today?",
                                    "is brother a good printer brand", "Is sister location scary"])
def test_known_over_blocks_of_the_subject_rule(prompt):
    assert classify(prompt).provenance == Provenance.PRIVATE


@pytest.mark.parametrize("prompt", ["Is Ben's new puppy okay today?", "What's Ben's current mood today?",
                                    "What's Dana's next move today?", "Is Priya's latest idea good today?"])
def test_new_or_latest_alone_does_not_reopen_a_search(prompt):
    assert not mo.public_owner_request(prompt)
    assert {"web_search", "web_fetch", "wikipedia_summary"} <= _model_led_forbidden(prompt)


@pytest.mark.parametrize("prompt", ["What's Google's latest phone this year?", "What's McDonald's new burger this month?",
                                    "Does McDonald's have breakfast all day today?",
                                    "What's Dr Pepper's new flavor this year?"])
def test_a_product_or_food_noun_keeps_the_words_only_search(prompt):
    assert mo.public_owner_request(prompt) and classify(prompt).public_possessive


def test_a_phone_number_is_not_excused_by_a_year_like_group():
    for prompt in ("What's Acme's price for 415-555-2019 today?", "What's Acme's price for 415 555 2019 today?",
                   "What's Acme's price for (415) 555-2019 today?"):
        assert not mo.public_owner_request(prompt), prompt


def test_news_from_a_relative_with_a_time_word_is_private():
    for prompt in ("any news from Mom today?", "any word from Dad yet?", "what's the latest from Grandma this week"):
        assert mo.personal_reference(prompt), prompt
    assert not mo.personal_reference("recipes from grandma style cooking channels")


def test_the_recipient_skip_keeps_the_clause_word():
    for prompt in ("Can you tell if Mom is home yet?", "tell me when Dad lands", "let me know if Grandma called"):
        assert classify(prompt).provenance == Provenance.PRIVATE, prompt


def test_a_decomposed_accent_reads_as_a_lookalike_known_over_block():
    # "cafe" + U+0301 + "s": the combining mark sits between a letter and s.
    assert mo.lookalike_possessive("best cafe\u0301s in Paris today")
    assert not mo.lookalike_possessive("best caf\u00e9s in Paris today")


# ---- second delta review (of 66888f2c) ----------------------------------------

# Blocker 1: the question may start any sentence or clause, behind a greeting,
# a filler, a list marker, quotes, an emoji or a zero-width character.
BEHIND_SOMETHING = [
    "Hi, is Mom home yet?", "Hi Wisp, is Mom home yet?", "Quick question: is Mom ok today?",
    "Also, is Mom home yet?", "Btw is Mom home yet?", "btw how's grandma today", "Morning! Is Dad up yet?",
    "Hey Wisp! Is Dad home yet?", "I'm worried. Is Mom ok today?", "Sorry to bother you, is Grandma ok today?",
    "Is, um, Mom home yet?", '"Is Mom home yet?"', "\U0001F642 Is Mom home yet?", "​Is Mom home yet?",
    "- Is Mom home yet?", "1. Is Mom home yet?", "xyzzy plugh is mom ok today?", "siri is dad home",
    "ok so anyway is grandma doing better today", "“Is Mom home yet?”",
]
# Blocker 2: words between the lead and the relative.
WORDS_BETWEEN = ["How old is Mom", "How old is Grandma now", "Which hospital is Grandma at today?",
                 "What hospital is Grandma in today?", "What time does Dad land tonight?"]
SHOULD_FIX = [
    "Mom feeling better today?", "mom doing ok today?", "Dad landed yet?",
    "Are the kids home yet?", "Is the baby asleep yet?", "Is hubby home yet", "How is Pop doing today?",
    "Is granddad okay today", "Is partner home yet",
    "Is moms flight on time today", "is moms surgery today", "mom flight today", "dad surgery today",
    "grandma hospital today", "check on mom", "check on grandma today",
    "Is dinner with Dad still on tonight?", "Mom posted online today, what did she say?",
]


@pytest.mark.parametrize("prompt", BEHIND_SOMETHING + WORDS_BETWEEN + SHOULD_FIX)
def test_the_question_about_a_relative_is_found_wherever_it_starts(prompt):
    assert mo.personal_reference(prompt), prompt
    forbidden = _model_led_forbidden(prompt)
    assert {"web_search", "web_fetch", "wikipedia_summary"} <= forbidden, prompt


@pytest.mark.parametrize("prompt", [p for p in BEHIND_SOMETHING + WORDS_BETWEEN + SHOULD_FIX if '"' not in p])
def test_and_the_classifier_calls_it_private(prompt):
    assert classify(prompt).provenance == Provenance.PRIVATE, prompt


def test_random_words_in_front_do_not_hide_the_question():
    import random
    rng = random.Random(20261112)
    letters = "abcdefghijklmnopqrstuvwxyz"
    for _ in range(400):
        prefix = " ".join("".join(rng.choice(letters) for _ in range(rng.randint(2, 8)))
                          for _ in range(rng.randint(1, 4)))
        prompt = f"{prefix} is mom ok today?"
        assert mo.personal_reference(prompt), prompt
        assert "web_search" in _model_led_forbidden(prompt), prompt


@pytest.mark.parametrize("prompt", ["Hi, is Mom home yet?", "Quick question: is Mom ok today?", '"Is Mom home yet?"',
                                    "How old is Grandma now", "Mom feeling better today?", "check on grandma today",
                                    "xyzzy plugh is mom ok today?"])
@pytest.mark.parametrize("history", [(), "private"])
def test_the_loop_runs_no_web_tool_for_them_either(synthetic, prompt, history):
    from tests.test_model_led_integration import PRIVATE_HISTORY
    effects, register = synthetic
    _egress_tools(register)
    approval = Approval(False)
    run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["web_search", "web_fetch", "wikipedia_summary"])],
        [call("web_search", query="mom home")], [call("wikipedia_summary", topic="Mom")],
        [call("web_fetch", url="https://example.com")], "Synthetic reply."],
        prompt=prompt, history=PRIVATE_HISTORY if history else (), approval=approval,
        forbidden_tools=frozenset(_model_led_forbidden(prompt)))
    assert not [name for name, _ in effects if name in PRIVATE_WEB_TOOLS], effects


# The soft family words are people only when what follows says so.
@pytest.mark.parametrize("prompt", ["Is baby formula recalled today?", "Are kids allowed at Costco today?",
                                    "Is pop music dying?", "Is partner visa processing slow this year?",
                                    "are twins more common now", "kids menu at Olive Garden today",
                                    "How old is the president?", "Which team is winning today?",
                                    "Hi, who is the president?", "gifts that are mom approved",
                                    "Dinner ideas for Mom tonight", "Weather in Chicago for Mom today"])
def test_public_questions_stay_public(prompt):
    assert not mo.personal_reference(prompt), prompt
    assert classify(prompt).provenance == Provenance.EXTERNAL
    assert "web_search" not in _model_led_forbidden(prompt)


# Accepted over-blocks of this round, pinned.
@pytest.mark.parametrize("prompt", ["Is Uncle Julio's open today?", "which brand is brother better than",
                                    "Brother printing problems today?"])
def test_known_over_blocks_of_the_clause_rules(prompt):
    assert {"web_search", "web_fetch"} <= _model_led_forbidden(prompt)


# Holidays keep a search of the user's own words, with or without a time word.
@pytest.mark.parametrize("prompt", ["When is Father's Day?", "Father's Day gifts 2026",
                                    "Mother's Day brunch near me today", "Valentine's Day gift ideas",
                                    "Is father's day a federal holiday?", "When is Mother's Day this year?"])
def test_a_holiday_with_an_s_keeps_its_search(prompt):
    assert mo.holiday_question(prompt)
    request = classify(prompt)
    assert request.provenance == Provenance.PRIVATE and request.public_possessive
    forbidden = _model_led_forbidden(prompt)
    assert "web_search" not in forbidden and {"web_fetch", "wikipedia_summary"} <= forbidden


def test_a_holiday_does_not_excuse_a_relative_or_a_number():
    for prompt in ("What should I get my mom for Mother's Day?", "Is Mom's flight on Father's Day?",
                   "Father's Day gifts for order 4155550123", "When is Fatherʹs Day?"):
        assert not mo.holiday_question(prompt), prompt


def test_long_runs_are_judged_quickly():
    import time
    cases = ["'" * 5000, '"' * 5000, "is mom " * 800, "mom's " * 900, "hi, " * 1500, "how old is " * 500,
             "is, um, mom home yet? " * 250, "a's" * 1700, "mom dad " * 700, "​" * 5000 + "is mom ok"]
    for text in cases:
        for check in (classify, lambda t: routing_guard_contract(t, model_led=True), mo.personal_reference,
                      mo.personal_owner, mo.lookalike_possessive, mo.public_owner_request, mo.holiday_question):
            start = time.thread_time()
            check(text)
            assert time.thread_time() - start < 1.0, text[:24]


def test_cutting_a_quote_run_keeps_what_is_quoted_and_what_is_not():
    from service.router.web_request import bounded_quotes
    assert bounded_quotes("'" * 5000) == "'" * 8 and bounded_quotes("'" * 5001) == "'" * 7
    assert bounded_quotes('say "hello" to them') == 'say "hello" to them'
    assert bounded_quotes("it's 5 o'clock") == "it's 5 o'clock"


# ---- final review of 5d2d3ea0: a holiday excuses only itself -------------------

HOLIDAY_NOT_AN_EXCUSE = [
    "Grandma fell on Mother's Day, is she ok?", "Mom is in the hospital on Mother's Day, how is she?",
    "Priya's Mother's Day flight today", "Dad is sick on Father's Day, is he better today?",
    "Mom is sad on Mother's Day, is she ok today?", "Mom called on Mother's Day, what did she want",
    "Mother's Day: Grandma is in hospital, any update?", "Aunt Sue is sick on Mother's Day, is she ok?",
    "any update on grandma Mother's Day", "Dana's Mother's Day plans today", "Ben's Father's Day party tonight",
    "Mother's Day at Dana's today?", "Uncle Joe's Father's Day party tonight", "Sam's Mother's Day surprise today",
    "Mother's Day at the Smiths' house today", "Father's Day 42 Elm Street", "Mother's Day at 12 Oak Ave today",
    "Valentine's Day with Sarah tonight?", "Mother's Day gift for Mom", "Father's Day card for Dad",
    "Is she coming for Mother's Day?", "What did he get her for Valentine's Day?",
    "Grandma fell on Valentine's Day, is she ok?", "Dad is sick on Children's Day, is he better today?",
    "Dana's Father's Day plans today", "Sam's Valentine's Day surprise today",
]
HOLIDAY_PUBLIC = ["When is Father's Day?", "Father's Day gifts 2026", "Mother's Day brunch near me today",
                  "Valentine's Day gift ideas", "Is father's day a federal holiday?", "When is Mother's Day this year?"]


@pytest.mark.parametrize("prompt", HOLIDAY_NOT_AN_EXCUSE)
def test_a_holiday_excuses_only_itself(prompt):
    assert not mo.holiday_question(prompt), prompt
    assert not classify(prompt).public_possessive
    assert {"web_search", "web_fetch", "wikipedia_summary"} <= _model_led_forbidden(prompt), prompt


@pytest.mark.parametrize("prompt", HOLIDAY_PUBLIC)
def test_the_public_holiday_questions_still_search(prompt):
    assert mo.holiday_question(prompt) and classify(prompt).public_possessive
    assert "web_search" not in _model_led_forbidden(prompt)


@pytest.mark.parametrize("prompt", ["Grandma fell on Mother's Day, is she ok?",
                                    "Mom is in the hospital on Mother's Day, how is she?",
                                    "Priya's Mother's Day flight today"])
def test_the_loop_runs_no_web_tool_behind_a_holiday(synthetic, prompt):
    effects, register = synthetic
    _egress_tools(register)
    approval = Approval(False)
    run([[call(DISCOVERY_TOOL, families=["public_web"], tools=["web_search", "web_fetch", "wikipedia_summary"])],
        [call("web_search", query=prompt.rstrip("?"))], [call("wikipedia_summary", topic="Mother's Day")],
        [call("web_fetch", url="https://example.com")], "Synthetic reply."], prompt=prompt, approval=approval)
    assert not [name for name, _ in effects if name in PRIVATE_WEB_TOOLS], effects
    assert not approval.calls
