from service.tools.registry import (Tool, ToolOutcome, REGISTRY, tool_schemas, get_tool,
                                    is_tool_error, classify_tool_outcome,
                                    is_tool_routable, routable_tool_names)
import service.tools.builtin  # noqa: F401  (registers built-in tools)
import service.tools.apps  # noqa: F401     (registers app-control tools)
import service.tools.assistant_tools  # noqa: F401  (registers schedule/reminder tools)
import service.tools.email_tools  # noqa: F401  (registers the email summary tool)
import service.tools.imessage_tools  # noqa: F401  (registers the messages summary tool)
import service.tools.system_control  # noqa: F401  (registers volume/clipboard/wifi/lock tools)
import service.tools.notes_tools  # noqa: F401  (registers the notes search tool)
import service.tools.recent_tools  # noqa: F401  (registers the cross-source recent-activity tool)
import service.tools.browser_history_tools  # noqa: F401  (registers the browser history search tool)
import service.tools.web_tools  # noqa: F401    (registers the web_fetch tool)
import service.tools.memory_tools  # noqa: F401   (registers remember/recall/forget)
import service.tools.action_tools  # noqa: F401   (registers send_email/send_message/http_request)
import service.tools.tool_authoring  # noqa: F401 (registers draft_tool/create_tool)
# --- Capability Atlas, Phase 1 (T1) ---
import service.tools.files_tools  # noqa: F401     (registers find_files — Spotlight, not `find`)
import service.tools.timers_alarms  # noqa: F401   (registers timers, alarms, stopwatch)
import service.tools.conversions  # noqa: F401     (registers calculate, convert_units)
import service.tools.notes_write  # noqa: F401     (registers create_note, append_note)
import service.tools.reference_tools  # noqa: F401 (registers world_time)
import service.tools.system_extras  # noqa: F401   (display, capture, toggles, status)
import service.tools.window_tools  # noqa: F401    (windows, app switching, reveal)
import service.tools.maps_travel  # noqa: F401     (places, drive time, directions)
import service.tools.everyday  # noqa: F401        (daily_brief, find_my_device)
import service.tools.schedule_extras  # noqa: F401 (complete_reminder, find_free_time)
import service.tools.misc_t1  # noqa: F401         (alerts, definitions, calls, status)
import service.tools.search_coverage  # noqa: F401 (deterministic "how far back" answers)
# --- Capability Atlas, Phase 2 (T2) ---
import service.tools.web_extras  # noqa: F401       (air quality, wikipedia, sports, recipes)
import service.tools.media_tools  # noqa: F401      (podcasts, ambient sound, internet radio)
import service.tools.system_extras2  # noqa: F401   (appearance, wifi join, power, updates)
import service.tools.email_extras  # noqa: F401     (subscriptions, triage, thread summary)
import service.tools.contacts_tools  # noqa: F401   (birthdays, contact create/update)
# apps.py, action_tools.py, assistant_tools.py, conversions.py extended in place
import service.tools.shortcuts_bridge  # noqa: F401  (run/list/install Apple Shortcuts)
import service.tools.security_privacy  # noqa: F401  (passwords, Keychain, file encryption)
import service.tools.local_log  # noqa: F401         (private journal, fitness goals)
# memory_tools.py, reference_tools.py, files_tools.py, system_control.py,
# system_extras2.py, imessage_tools.py (via tool_aliases.py) extended in place
import service.tools.speech_tools  # noqa: F401     (text-to-speech via `say`)
import service.tools.automation_tools  # noqa: F401 (raw AppleScript, schedule_task)
# notes_write.py, misc_t1.py, local_log.py, maps_travel.py extended in place
import service.tools.doc_tools  # noqa: F401         (real .docx/.xlsx via python-docx/openpyxl)
import service.tools.codex_tools  # noqa: F401       (local Codex task overview)

__all__ = ["Tool", "ToolOutcome", "REGISTRY", "tool_schemas", "get_tool",
           "is_tool_error", "classify_tool_outcome", "is_tool_routable",
           "routable_tool_names"]
