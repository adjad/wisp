import service.tools.action_tools  # noqa: F401   (registers send_email/send_message/http_request)
import service.tools.apps  # noqa: F401     (registers app-control tools)
import service.tools.assistant_tools  # noqa: F401  (registers schedule/reminder tools)
import service.tools.builtin  # noqa: F401  (registers built-in tools)
import service.tools.codegen  # noqa: F401  (registers the coding-specialist tool)
import service.tools.email_tools  # noqa: F401  (registers the email summary tool)
import service.tools.imessage_tools  # noqa: F401  (registers the messages summary tool)
import service.tools.memory_tools  # noqa: F401   (registers remember/recall/forget)
import service.tools.notes_tools  # noqa: F401  (registers the notes search tool)
import service.tools.profile_tools  # noqa: F401  (registers build/show profile tools)
import service.tools.system_control  # noqa: F401  (registers volume/clipboard/wifi/lock tools)
import service.tools.tool_authoring  # noqa: F401 (registers draft_tool/create_tool)
import service.tools.vision  # noqa: F401   (registers vision/screen tools)
import service.tools.web_tools  # noqa: F401    (registers the web_fetch tool)
from service.tools.registry import REGISTRY, Tool, get_tool, tool_schemas

__all__ = ["Tool", "REGISTRY", "tool_schemas", "get_tool"]
