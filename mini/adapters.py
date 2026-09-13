"""Disabled portable snapshot adapters. This module has no I/O capabilities.

Qualification here admits only pure transformations of explicitly supplied
snapshots. Provider fetching is absent, even after portable qualification.
"""
from types import MappingProxyType

REVISION = "mini-portable-v1"
KINDS = frozenset({"canvas.sync", "study.generate", "stocks.watch", "research.run"})


class AdapterRefusal(ValueError):
    pass


def plain(value, maximum=20000):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > maximum:
        raise AdapterRefusal("invalid_snapshot")
    return value


def outline(arguments):
    if not isinstance(arguments, dict) or set(arguments) != {"text"}:
        raise AdapterRefusal("invalid_tool_arguments")
    return "\n".join(line.strip() for line in plain(arguments["text"]).splitlines() if line.strip())


# No dynamic imports, registration hooks, eval, URL interpretation, paths,
# credential references or arbitrary callable injection.
PORTABLE_TOOLS = MappingProxyType({"text.outline": outline})


def portable_tool(name, arguments):
    if not isinstance(name, str) or name not in PORTABLE_TOOLS:
        raise AdapterRefusal("tool_not_allowed")
    return PORTABLE_TOOLS[name](arguments)


class SnapshotAdapter:
    def __init__(self, kind, *, enabled=False, qualification=None):
        if kind not in KINDS or type(enabled) is not bool:
            raise AdapterRefusal("invalid_adapter")
        if qualification is not None and qualification != {"kind": kind, "revision": REVISION, "scope": "portable-snapshot-only"}:
            raise AdapterRefusal("adapter_unqualified")
        self.kind = kind
        self.enabled = enabled
        self.qualified = qualification is not None

    def validate(self, snapshot):
        if not self.enabled or not self.qualified:
            raise AdapterRefusal("adapter_disabled")
        if not isinstance(snapshot, dict) or set(snapshot) != {"items"}:
            raise AdapterRefusal("invalid_snapshot")
        items = snapshot["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise AdapterRefusal("invalid_snapshot")
        for item in items:
            if not isinstance(item, dict) or set(item) != {"title", "text"}:
                raise AdapterRefusal("invalid_snapshot")
            plain(item["title"], 200)
            plain(item["text"], 3000)

    def render(self, snapshot):
        self.validate(snapshot)
        labels = {"canvas.sync": "Canvas snapshot", "study.generate": "Study notes",
                  "stocks.watch": "Stocks snapshot", "research.run": "Research notes"}
        body = "\n\n".join(plain(item["title"], 200) + "\n" + portable_tool("text.outline", {"text": item["text"]})
                           for item in snapshot["items"])
        return labels[self.kind], body

    def fetch_provider(self, *args, **kwargs):
        raise AdapterRefusal("provider_not_qualified")


def disabled_adapters():
    return {kind: SnapshotAdapter(kind) for kind in sorted(KINDS)}
