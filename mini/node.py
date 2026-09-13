"""Read-only proactive-node HTTP surface. No timers or execution handlers."""
from urllib.parse import parse_qs

from mini.http import Boundary, Rejected
from mini.protocol import KINDS
from mini.store import CursorError


class Node(Boundary):
    routes = frozenset({("GET", "/healthz"), ("GET", "/v1/status"), ("GET", "/v1/results")})

    def __init__(self, token, store, **limits):
        super().__init__(token, deadline=2, concurrency=4, **limits)
        self.store = store
        # Persist immutable disabled schedules. Starting/restarting never stages
        # occurrences, invokes a connector, catches up missed work, or executes.
        for kind in sorted(KINDS):
            self.store.register_job(kind, kind)

    async def handle(self, scope, body, headers, reply):
        query = scope.get("query_string", b"")
        if scope["path"] != "/v1/results":
            if query:
                raise Rejected(400, "invalid_query")
            status = self.store.status()
            await reply.json({"status": status["status"]} if scope["path"] == "/healthz" else status)
            return
        try:
            params = parse_qs(query.decode("ascii"), keep_blank_values=True, strict_parsing=True, max_num_fields=2)
            if set(params) - {"cursor", "limit"} or any(len(v) != 1 for v in params.values()):
                raise ValueError
            limit = params.get("limit", ["100"])[0]
            if not limit.isascii() or not limit.isdigit() or not 1 <= int(limit) <= 100:
                raise ValueError
            page = self.store.page(params.get("cursor", [""])[0], int(limit))
        except (ValueError, UnicodeError, CursorError):
            raise Rejected(400, "invalid_cursor_or_query") from None
        await reply.json(page)
