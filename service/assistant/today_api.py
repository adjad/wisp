"""Local Today endpoints. No source writes, model calls, or outbound effects."""
import time
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from service.assistant.store import assistant_store
from service.assistant.today import RevisionConflict, build_plan

router = APIRouter(prefix="/assistant/today", tags=["today"])
from service.assistant.daily_workspace_api import router as workspace_router
router.include_router(workspace_router)


class CreateTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    day: str
    timezone: str = "UTC"
    kind: str = "task"
    duration_minutes: StrictInt = Field(ge=1, le=1440)
    priority: StrictInt = Field(default=2, ge=1, le=3)
    due_ts: float | None = Field(default=None, strict=True, allow_inf_nan=False)
    pinned_start: float | None = Field(default=None, strict=True, allow_inf_nan=False)
    # When the thing this task came from first mattered: a reminder's due time
    # once it has passed. The morning carry-forward ages the task from here
    # (checkin.task_origin), so a task made from a two-week-old reminder is not
    # carried as if it were new. Range-checked by today.validate_task.
    origin_ts: float | None = Field(default=None, strict=True, allow_inf_nan=False)


class EditTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictInt = Field(ge=1)
    title: str | None = None
    day: str | None = None
    timezone: str | None = None
    kind: str | None = None
    duration_minutes: StrictInt | None = None
    priority: StrictInt | None = None
    due_ts: float | None = Field(default=None, strict=True, allow_inf_nan=False)
    pinned_start: float | None = Field(default=None, strict=True, allow_inf_nan=False)
    status: str | None = None


class Replan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    day: str
    timezone: str
    revision: StrictInt = Field(ge=0)
    start_minute: StrictInt = 540
    end_minute: StrictInt = 1080
    not_before: float | None = Field(default=None, strict=True, allow_inf_nan=False)
    # Plan this day although an all-day event reads like time away.
    plan_all_day: bool = Field(default=False, strict=True)


def fail(exc):
    raise HTTPException(status_code=409 if isinstance(exc, RevisionConflict) else
                        404 if isinstance(exc, KeyError) else 422, detail=str(exc)) from exc


def cleared_needs():
    """The Needs you rows the user cleared (needs_you_api); none from a store without them."""
    read = getattr(assistant_store, "needs_you_dismissals", None)
    try:
        return read() if read is not None else None
    except Exception:  # noqa: BLE001 - a failed read shows every row, never no workspace
        return None


def removed_suggestions():
    """The Day suggestions the user removed (daily_workspace_api); none from a store without them."""
    read = getattr(assistant_store, "removed_suggestions", None)
    try:
        return read() if read is not None else None
    except Exception:  # noqa: BLE001 - a failed read shows every suggestion, never no workspace
        return None


def live_access(sources: dict, now: float) -> dict:
    """The plan's source health with what the person can do about a source Wisp
    can't read (today.with_access), from this launch's reader reports. Never
    fatal: without them the stored health is returned unchanged."""
    try:
        from service.assistant import scheduler
        from service.assistant.today import with_access
        return with_access(sources, scheduler.connectors_status(), now=now, started_at=scheduler.STARTED_AT)
    except Exception:  # noqa: BLE001
        return sources


def snapshot(day, timezone, *, debug=False):
    from service import diagnostics
    with diagnostics.span("today") as trace:
        state = assistant_store.today_snapshot(day, timezone)
        now = time.time()
        # A Day task that duplicates a reminder the stale policy hides is hidden with it.
        state = {**state, "tasks": assistant_store.hide_stale_tasks(state["tasks"], now)}
        plan = build_plan(day, timezone, state["tasks"], state["commitments"], state["preferences"],
                          state["sources"], now=now)
        trace.event("plan", tasks=len(plan["tasks"]), blocks=len(plan["blocks"]),
                    unscheduled=len(plan["unscheduled"]), warnings=len(plan["warnings"]),
                    revision=state["revision"], calendar_ready=plan["sources"]["calendar"]["ready"],
                    reminders_ready=plan["sources"]["reminders"]["ready"])
        result = {**plan, "revision": state["revision"], "trace_id": trace.id,
                  "sources": live_access(plan["sources"], now)}
        try:
            from service.assistant import daily_workspace as daily
            from service.assistant.daily_workspace_api import email_store, availability, user_profile
            result["workspace"] = daily.assess(day, timezone, state,
                daily.collect_evidence(assistant_store, state, day, timezone, now=now), now=now,
                behavior=availability(day, timezone, now),
                ignored_windows=email_store().ignored(day, timezone),
                # The people the user named in "Getting to know you" rise to the
                # top of Needs you here too, as that screen promised.
                profile=user_profile(),
                dismissed=cleared_needs(), removed=removed_suggestions())
        except Exception:
            # The original revisioned planner remains usable when an optional
            # source or private workspace settings are unavailable.
            result["workspace_error"] = "Daily source evidence could not be refreshed; your saved day plan is shown."
        # Timed Calendar blocks carry what the Day timeline's Cancel and Move need
        # (the check-in card's rules, service/assistant/checkin.py). New lists, so
        # the planner's own output (and the replay below) stays as built; the
        # all-day strip shows titles only, so those lists are left alone. Never
        # fatal: without the facts the app offers only "Open in Calendar".
        try:
            from service.assistant import checkin
            seen: dict = {}
            result["blocks"] = checkin.annotate_calendar_blocks(assistant_store, result["blocks"], state["commitments"], seen)
            proposed = result.get("workspace", {}).get("proposed_plan")
            if isinstance(proposed, dict) and isinstance(proposed.get("blocks"), list):
                proposed["blocks"] = checkin.annotate_calendar_blocks(assistant_store, proposed["blocks"],
                                                                      state["commitments"], seen)
        except Exception:  # noqa: BLE001
            pass
        if "workspace" in result:
            try:
                # One-line gists of important emails for the Brief's MAIL row,
                # when the background writer has made them. Never fatal.
                from service.assistant import mail_gists
                mail_gists.attach(result["workspace"])
            except Exception:  # noqa: BLE001
                pass
            try:
                # "Before you ask" cards, to-dos and thread summaries (service/proactive).
                from service.proactive import cycle as proactive_cycle
                proactive_cycle.attach(result["workspace"], now=now)
            except Exception:  # noqa: BLE001
                pass
        if debug:
            # Returned only on explicit opt-in; never journaled. Fixed clock and
            # exact inputs make a replay independent of current state/sources.
            result["replay"] = {"schema_version": 1, "planner_fingerprint": diagnostics.planner_fingerprint(), "day": day, "timezone": timezone,
                                "now": now, **{k: state[k] for k in
                                ("tasks", "commitments", "preferences", "sources")},
                                "expected_plan": plan}
        return result


@router.get("")
def today(day: str, timezone: str, debug: bool = False):
    try:
        return snapshot(day, timezone, debug=debug)
    except ValueError as exc:
        fail(exc)


@router.post("/tasks", status_code=201)
def create_task(body: CreateTask):
    try:
        return assistant_store.today_save_task(body.model_dump())
    except ValueError as exc:
        fail(exc)


@router.patch("/tasks/{task_id}")
def edit_task(task_id: str, body: EditTask):
    try:
        values = body.model_dump(exclude_unset=True, exclude={"revision"})
        return assistant_store.today_save_task(values, task_id=task_id, revision=body.revision)
    except (ValueError, KeyError) as exc:
        fail(exc)


@router.post("/replan")
def replan(body: Replan):
    try:
        assistant_store.today_save_preferences(body.day, body.timezone,
            body.model_dump(exclude={"day", "timezone", "revision"}), body.revision)
        return snapshot(body.day, body.timezone)
    except ValueError as exc:
        fail(exc)
