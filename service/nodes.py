"""Pro-side inbox for an opt-in proactive node. Remote data never executes tools."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
from contextlib import contextmanager

import httpx

from service.config import models_config
from service.config.endpoints import endpoint
from service.paths import MOE_DIR

KINDS = frozenset({"canvas.sync", "study.generate", "stocks.watch", "research.run", "effect.proposal"})
EFFECTS = frozenset({"email.send", "message.send", "calendar.write"})


class NodeProtocolError(ValueError):
    pass


def _text(value, name, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise NodeProtocolError(f"Invalid node result {name}")
    return value


def validate_result(raw, node_id):
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version", "node_id", "result_id", "job_id", "occurrence_id", "kind", "title", "text", "proposal"
    } or type(raw["schema_version"]) is not int or raw["schema_version"] != 1 or raw["node_id"] != node_id:
        raise NodeProtocolError("Invalid node result envelope")
    for name in ("node_id", "result_id", "job_id", "occurrence_id", "title"):
        _text(raw[name], name)
    _text(raw["text"], "text", 100_000)
    if raw["kind"] not in KINDS:
        raise NodeProtocolError("Unsupported node result kind")
    proposal = raw["proposal"]
    if raw["kind"] == "effect.proposal":
        if not isinstance(proposal, dict) or set(proposal) != {"effect_id", "kind", "arguments"}:
            raise NodeProtocolError("Invalid effect proposal")
        _text(proposal["effect_id"], "effect_id")
        if proposal["kind"] not in EFFECTS or not isinstance(proposal["arguments"], dict):
            raise NodeProtocolError("Unsupported proposed effect")
        # Stored/displayed only. Execution requires a separate local approval
        # contract and is deliberately not wired to the native action bridge.
    elif proposal is not None:
        raise NodeProtocolError("Unexpected effect proposal")
    encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode()) > 200_000:
        raise NodeProtocolError("Node result exceeds size limit")
    return encoded, hashlib.sha256(encoded.encode()).hexdigest()


class NodeInbox:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS node_cursors(node TEXT PRIMARY KEY, cursor TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS node_results(
                    node TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
                    digest TEXT NOT NULL, published INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(node,id));
            """)
        path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def cursor(self, node):
        with self.connect() as db:
            row = db.execute("SELECT cursor FROM node_cursors WHERE node=?", (node,)).fetchone()
            return row[0] if row else ""

    def ingest(self, node, page, *, expected_cursor):
        if not isinstance(page, dict) or set(page) != {"schema_version", "results", "next_cursor"} or type(page["schema_version"]) is not int or page["schema_version"] != 1:
            raise NodeProtocolError("Invalid node page")
        results, cursor = page["results"], page["next_cursor"]
        if not isinstance(results, list) or len(results) > 100 or not isinstance(cursor, str) or len(cursor) > 1024:
            raise NodeProtocolError("Invalid node page bounds")
        encoded = [(row, *validate_result(row, node)) for row in results]
        if results and (not cursor or cursor == expected_cursor):
            raise NodeProtocolError("Node cursor did not advance")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT cursor FROM node_cursors WHERE node=?", (node,)).fetchone()
            if (current[0] if current else "") != expected_cursor:
                raise NodeProtocolError("Concurrent node cursor update")
            for row, payload, digest in encoded:
                old = db.execute("SELECT digest FROM node_results WHERE node=? AND id=?", (node, row["result_id"])).fetchone()
                if old and old[0] != digest:
                    raise NodeProtocolError("Conflicting immutable node result")
                db.execute("INSERT OR IGNORE INTO node_results(node,id,payload,digest) VALUES(?,?,?,?)",
                           (node, row["result_id"], payload, digest))
            db.execute("INSERT INTO node_cursors VALUES(?,?) ON CONFLICT(node) DO UPDATE SET cursor=excluded.cursor", (node, cursor))

    def pending(self, node):
        with self.connect() as db:
            return db.execute("SELECT * FROM node_results WHERE node=? AND published=0 ORDER BY rowid LIMIT 100", (node,)).fetchall()

    async def publish(self, node, hub):
        for row in self.pending(node):
            result = json.loads(row["payload"])
            title, text = result["title"], result["text"]
            if result["kind"] == "effect.proposal":
                title = "Review requested: " + title
                text += "\n\nProposed action (not executed):\n" + json.dumps(result["proposal"], ensure_ascii=False, sort_keys=True)
            # Only a presentation event can cross this boundary. Incoming types,
            # action IDs and approval claims never reach the action dispatcher.
            await hub.publish({"type": "node_result", "title": title, "text": text,
                               "node_id": node, "result_id": result["result_id"]},
                              dedupe_key="node:" + hashlib.sha256(json.dumps([node, result["result_id"]]).encode()).hexdigest())
            with self.connect() as db:
                db.execute("UPDATE node_results SET published=1 WHERE node=? AND id=?", (node, row["id"]))


async def poll_once(inbox, hub, cfg, *, transport=None):
    node = _text(cfg.get("node_id"), "node_id")
    ep = endpoint(_text(cfg.get("endpoint"), "endpoint"))
    if ep.managed:
        raise NodeProtocolError("A proactive node requires a separate remote endpoint")
    cursor = inbox.cursor(node)
    async with asyncio.timeout(10):
        async with httpx.AsyncClient(base_url=ep.base_url, trust_env=False, follow_redirects=False,
                                     transport=transport, timeout=httpx.Timeout(5, connect=2)) as client:
            async with client.stream("GET", "/v1/results", params={"cursor": cursor, "limit": 100},
                                     headers={"Authorization": f"Bearer {ep.api_key()}"}) as response:
                response.raise_for_status()
                body = bytearray()
                async for part in response.aiter_bytes():
                    body.extend(part)
                    if len(body) > 2_000_000:
                        raise NodeProtocolError("Node page exceeds size limit")
            page = json.loads(body)
    inbox.ingest(node, page, expected_cursor=cursor)
    await inbox.publish(node, hub)


async def run():
    inbox = None
    from service.assistant.hub import hub
    while True:
        cfg = models_config().get("proactive_node", {})
        if cfg.get("enabled") is True:
            try:
                if inbox is None:
                    inbox = NodeInbox(MOE_DIR / "node-inbox.db")
                # Recover committed but unpublished results even during outage.
                await inbox.publish(_text(cfg.get("node_id"), "node_id"), hub)
                await poll_once(inbox, hub, cfg)
            except Exception as exc:
                # Error class only; never log response bodies, keys or results.
                import logging
                logging.getLogger(__name__).warning("Node poll unavailable: %s", type(exc).__name__)
        await asyncio.sleep(30)
