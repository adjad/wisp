"""Keep conversational task roles without escalating standalone greetings."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from service.router.router import RouteDecision


STICKY_ROLES = frozenset({"coding", "reasoning", "agent"})

# This deliberately does not reuse TRIVIAL_RE: that router rule also accepts
# assent, cancellation, and suffixes such as "okay shorter". Those can carry
# work from the previous turn and must retain the session's task role.
_SOCIAL_ONLY = re.compile(
    r"(?:hi|hey|hello|yo|sup|howdy|hiya|"
    r"(?:hi|hey|hello)\s+there|"
    r"thanks|thank\s+you|thx|ty|appreciate\s+it|"
    r"(?:thanks|thank\s+you)\s+(?:so\s+much|very\s+much|a\s+lot|bud)|"
    r"good\s+(?:morning|afternoon|evening|night)|"
    r"how(?:'?s\s+it\s+going|\s+are\s+you|\s+goes\s+it)|what'?s\s+up)"
    r"[\s!.?]*",
    re.I,
)


def apply_session_pin(decision: RouteDecision, session: dict | None,
                      text: str, *, active_skill: str = "") -> RouteDecision:
    """Apply pinning only after task, clarification and contextual routing.

    A complete social greeting may keep an already tool-free fast decision.
    It cannot override a contextual tool route, even if its words also look
    like a greeting. The stored pin remains available for the next task turn.
    """
    social_fast = (
        decision.role == "fast"
        and not active_skill
        and not decision.needs_tools
        and not decision.tool_subset
        and not decision.direct_calls
        and not decision.force_first_tool
        and not decision.required_tool_groups
        and not decision.resolved_request
        and bool(_SOCIAL_ONLY.fullmatch(text.strip()))
    )
    if (session and session["pinned_role"] in STICKY_ROLES
            and decision.role not in STICKY_ROLES
            and not decision.tool_subset and not social_fast):
        # Preserve this turn's tool requirement when pinned to coding/reasoning.
        needs_tools = decision.needs_tools
        decision.role = session["pinned_role"]
        decision.model = session["pinned_model"]
        decision.needs_tools = needs_tools or decision.role == "agent"
        decision.reason = f"pinned to {decision.role} for this conversation"
    return decision
