"""Keep personal choices, time pressure and inferred importance distinct."""


def dimensions(demand, *, now, explicit=None, decisions=()):
    recent = [
        d
        for d in decisions
        if d.get("id") == demand["id"] and 0 <= now - d.get("ts", 0) <= 30 * 86400
    ]
    return dict(
        importance=explicit if explicit in {"high", "normal", "low"} else "unknown",
        importance_basis=(
            "explicit user choice"
            if explicit in {"high", "normal", "low"}
            else "not established"
        ),
        inferred_importance=demand.get("importance_hypothesis", "unknown"),
        due_range=demand.get("due_range"),
        recent_choices=[d["choice"] for d in recent[-10:]],
        preference_basis="voluntary planning choices; not proof of completion or value",
        displacement="unknown; different computer activity cannot establish abandonment",
    )
