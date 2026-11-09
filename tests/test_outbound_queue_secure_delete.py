"""A scheduled message's text must not stay in assistant.db's free pages once removed."""
import time

from service.assistant.outbound_queue import OutboundQueue

MARKER = "scheduled-words-that-must-not-linger"


def test_removed_text_is_overwritten_in_the_file(tmp_path):
    path = tmp_path / "assistant.db"
    queue = OutboundQueue(path)
    sid = queue.add(channel="email", recipient="ann@example.test", body=MARKER * 50,
                    when_ts=time.time() + 3600)
    assert MARKER.encode() in path.read_bytes()
    queue._db.execute("DELETE FROM scheduled_sends WHERE id=?", (sid,))
    queue._db.commit()
    queue._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert MARKER.encode() not in path.read_bytes()
