"""Cloud-everything scale-up for meeting notes and documents, gate on and gate off.

Synthetic only, over a mocked transport (sockets are forbidden by the fixture).
Gate on: a remote-only profile with remote_parity; one real remote client
request carries the whole text. Gate off: the local values are unchanged.
"""
import asyncio
import json
import os
import tempfile
import time
from datetime import date
from unittest.mock import patch

from tests.test_remote_media import FULL, GEMINI, HAIKU, MediaBase, b64, reply, run
from tests.test_remote_inference_policy import policy

from service import documents as D
from service import idle, meetings as M
import service.vision as V

NOTES = {"title": "Sync", "summary": "They planned the launch.", "decisions": [],
         "action_items": [], "follow_up": "Thanks all."}


class LocalStub:
    """This Mac's engine (no `managed` attribute: treated as local)."""

    def __init__(self, notes=json.dumps(NOTES), answer="A lease."):
        self.notes, self.answer = notes, answer
        self.calls = []

    def activity_key(self, model):
        return f"stub:{model}"

    async def status(self):
        return {"models": []}

    async def chat(self, model, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        first = messages[0]["content"]
        if "JSON" in first and "meeting" in first:
            content = self.notes
        elif "one part of a meeting" in first:
            content = "- part notes"
        else:
            content = self.answer
        return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


class ScaleUpBase(MediaBase):
    profile_overrides = {**FULL, "model_id": GEMINI, "context_window": 200_000}

    def setUp(self):
        super().setUp()
        os.environ["WISP_HOME"] = tempfile.mkdtemp(prefix="wisp-scale-up-")
        D._ONE_AT_A_TIME = asyncio.Semaphore(1)
        self.addCleanup(lambda: setattr(idle, "_foreground_turns", 0))

    def gate_off(self):
        self.legacy()                 # no remote-only profile: this Mac's engine


class GateHelpers(ScaleUpBase):
    def test_the_gate_and_the_text_budget(self):
        self.assertTrue(D.cloud_everything_active())
        self.assertEqual(D.cloud_text_chars(600), 400_000)
        # A smaller saved window bounds one call to what the client will accept.
        self.cfg["inference"]["remote_only"]["context_window"] = 32_000
        policy.remote_only_profile.cache_clear()
        self.assertEqual(D.cloud_text_chars(600), (32_000 - 4096 - 2000) * 4)
        self.use_model(GEMINI, parity=False)
        self.assertFalse(D.cloud_everything_active())
        self.assertEqual(D.cloud_text_chars(600), 0)
        self.gate_off()
        self.assertFalse(D.cloud_everything_active())
        self.assertEqual(D.cloud_text_chars(600), 0)

    def test_text_limit_needs_the_cloud_client(self):
        self.assertEqual(D._text_limit(self.client([])), 400_000)
        self.assertEqual(D._text_limit(LocalStub()), D.MAX_TEXT_CHARS)
        self.use_model(GEMINI, parity=False)
        self.assertEqual(D._text_limit(self.client([])), D.MAX_TEXT_CHARS)

    def test_scanned_page_cap(self):
        self.assertEqual(D._scan_limit(), min(policy.REMOTE_IMAGES_PER_REQUEST, V.MAX_FRAMES))
        self.use_model(HAIKU)
        self.assertEqual(D._scan_limit(), min(policy.REMOTE_IMAGES_PER_REQUEST, V.MAX_FRAMES))
        self.use_model("example/unlisted-model")          # text only: no pictures go
        self.assertEqual(D._scan_limit(), D.MAX_SCANNED_PAGES)
        self.gate_off()
        self.assertEqual(D._scan_limit(), 4)


class DocumentsGateOn(ScaleUpBase):
    def test_a_long_document_is_answered_whole_in_one_call(self):
        text = "Clause 7: the rent is due monthly. " * 3000          # 105k chars
        client = self.client([reply("Monthly.")])
        result = run(D.answer_request("When is rent due?", {"name": "lease.txt", "data": b64(text.encode())},
                                      client=client, model=GEMINI))
        self.assertEqual(len(self.bodies), 1)
        sent = self.bodies[0]["messages"][1]["content"]
        self.assertIn(text.strip(), sent)
        self.assertNotIn("condensed", sent)
        self.assertNotIn("only its first part", sent)
        self.assertGreaterEqual(self.bodies[0]["max_tokens"], policy.REMOTE_MEDIA_MIN_TOKENS)
        self.assertEqual(result.answer, "Monthly.")

    def test_the_long_answer_gets_the_long_document_time(self):
        seen = []
        real = D.local_chat

        async def spy(client, model, messages, max_tokens, deadline):
            seen.append(deadline - time.monotonic())
            return await real(client, model, messages, max_tokens, deadline)
        client = self.client([reply("ok")])
        with patch.object(D, "local_chat", spy):
            run(D.answer_request("?", {"name": "long.txt", "data": b64(("Rent is due. " * 4000).encode())},
                                 client=client, model=GEMINI))
        self.assertTrue(seen and all(D._TIMEOUT_S < left <= D._LARGE_TIMEOUT_S for left in seen))

    def test_above_the_cloud_budget_it_is_brought_down_in_the_one_answer_call(self):
        text = "The rent is due on the first of each month. " * 11_000   # 484k chars
        client = self.client([reply("On the first.")])
        result = run(D.answer_request("?", {"name": "huge.txt", "data": b64(text.encode())},
                                      client=client, model=GEMINI))
        self.assertEqual(len(self.bodies), 1)                 # no notes passes
        sent = self.bodies[0]["messages"][1]["content"]
        self.assertGreater(len(sent), D.MAX_TEXT_CHARS * 10)
        self.assertLess(len(sent), D.CLOUD_TEXT_CHARS + 10_000)
        self.assertIn("condensed for you", sent)
        self.assertIn("was not read", result.answer)

    def test_up_to_eight_scanned_pages_go_to_the_cloud_model(self):
        seen = {}

        async def fake_answer(prompt, images, **kwargs):
            seen["images"] = images
            return V.VisionResult("A signed form.")
        cap = D._scan_limit()
        pages = ["data:image/jpeg;base64,AAAA"] * cap
        with patch.object(V, "answer", fake_answer):
            self.assertEqual(run(D.answer_request("?", {"name": "Scan.pdf", "pages": pages})).answer,
                             "A signed form.")
            self.assertEqual(len(seen["images"]), cap)
            with self.assertRaisesRegex(ValueError, f"at most {cap} pages"):
                run(D.answer_request("?", {"name": "Scan.pdf", "pages": pages + pages[:1]}))


class DocumentsGateOff(ScaleUpBase):
    def setUp(self):
        super().setUp()
        self.gate_off()

    def test_local_values_unchanged(self):
        self.assertEqual((D.MAX_TEXT_CHARS, D.MAX_SCANNED_PAGES, D._MAX_TOKENS), (24_000, 4, 600))
        self.assertEqual(D._text_limit(LocalStub()), 24_000)
        self.assertEqual(D._scan_limit(), 4)

    def test_a_long_document_is_still_condensed_locally(self):
        client = LocalStub()
        text = ("Rent is due monthly. " * 3000).encode()                # 63k chars
        run(D.answer_request("?", {"name": "long.txt", "data": b64(text)}, client=client, model="gemma-stub"))
        self.assertGreater(len(client.calls), 1)                          # notes passes, then the answer
        self.assertLess(len(client.calls[-1]["messages"][1]["content"]), D.MAX_TEXT_CHARS + 1000)
        self.assertEqual(client.calls[-1]["max_tokens"], 600)

    def test_five_scanned_pages_are_refused_locally(self):
        with self.assertRaisesRegex(ValueError, "at most 4 pages"):
            run(D.answer_request("?", {"name": "Scan.pdf", "pages": ["data:image/jpeg;base64,AAAA"] * 5}))


class MeetingsGateOn(ScaleUpBase):
    def test_the_whole_transcript_is_written_up_in_one_call(self):
        text = "We agreed to ship the launch on Friday. " * 2500           # 100k chars
        client = self.client([reply(json.dumps(NOTES))])
        notes = run(M.write_notes(text, client=client, model=GEMINI, deadline=time.monotonic() + 30,
                                  held=date(2026, 11, 14)))
        self.assertEqual(notes["summary"], "They planned the launch.")
        self.assertEqual(len(self.bodies), 1)
        body = self.bodies[0]
        self.assertEqual(body["max_tokens"], 4096)
        self.assertIn(text.strip(), body["messages"][1]["content"])

    def test_a_very_long_transcript_falls_back_to_large_parts(self):
        text = "We agreed to ship the launch on Friday. " * 12_500         # 500k chars
        client = self.client([reply("- part one"), reply("- part two"), reply(json.dumps(NOTES))])
        run(M.write_notes(text, client=client, model=GEMINI, deadline=time.monotonic() + 30))
        self.assertEqual(len(self.bodies), 3)
        parts = [b["messages"][1]["content"] for b in self.bodies[:2]]
        self.assertTrue(all(len(p) <= M._CLOUD_ONE_CALL_CHARS + 1000 for p in parts))
        self.assertGreater(len(parts[0]), 300_000)
        self.assertIn("- part one", self.bodies[2]["messages"][1]["content"])

    def test_the_cloud_write_up_gets_more_time(self):
        seen = []

        async def spy(text, *, client, model, deadline, held=None):
            seen.append(deadline - time.monotonic())
            return dict(NOTES)
        cloud = self.client([])
        meeting = {"id": "abcdef123456", "title": "", "status": "summarizing", "started_at": time.time(),
                   "touched_at": time.time(), "ended_at": time.time(), "notes": None, "error": "",
                   "heard_s": 1.0, "missed_s": 0.0, "segments": [{"seq": 0, "seconds": 1.0, "text": "Hi.",
                                                                  "heard": True}]}
        M._save(meeting)
        with patch.object(M, "write_notes", spy), patch.object(M.voice, "_resolve", return_value=(cloud, GEMINI)):
            self.assertEqual(run(M._write_up(meeting["id"], meeting))["status"], "done")
        self.assertTrue(M._NOTES_TIMEOUT_S < seen[0] <= M._CLOUD_NOTES_TIMEOUT_S)


class MeetingsGateOff(ScaleUpBase):
    def setUp(self):
        super().setUp()
        self.gate_off()

    def test_local_values_unchanged(self):
        self.assertEqual((M._CHUNK_CHARS, M._NOTES_TIMEOUT_S), (6000, 120.0))
        self.assertEqual(M._one_call_chars(LocalStub()), 6000)

    def test_a_long_transcript_is_still_noted_in_small_parts(self):
        client = LocalStub()
        text = "A sentence about the plan. " * 800                          # 21.6k chars
        run(M.write_notes(text, client=client, model="gemma-stub", deadline=time.monotonic() + 30))
        parts, final = client.calls[:-1], client.calls[-1]
        self.assertGreaterEqual(len(parts), 3)
        self.assertTrue(all(len(c["messages"][1]["content"]) < M._CHUNK_CHARS + 200 for c in parts))
        self.assertTrue(all(c["max_tokens"] == 400 for c in parts))
        self.assertEqual(final["max_tokens"], 900)

    def test_the_local_write_up_keeps_its_time(self):
        seen = []

        async def spy(text, *, client, model, deadline, held=None):
            seen.append(deadline - time.monotonic())
            return dict(NOTES)
        meeting = {"id": "abcdef654321", "title": "", "status": "summarizing", "started_at": time.time(),
                   "touched_at": time.time(), "ended_at": time.time(), "notes": None, "error": "",
                   "heard_s": 1.0, "missed_s": 0.0, "segments": [{"seq": 0, "seconds": 1.0, "text": "Hi.",
                                                                  "heard": True}]}
        M._save(meeting)
        with patch.object(M, "write_notes", spy), \
                patch.object(M.voice, "_resolve", return_value=(LocalStub(), "gemma-stub")):
            run(M._write_up(meeting["id"], meeting))
        self.assertTrue(M._NOTES_TIMEOUT_S - 5 < seen[0] <= M._NOTES_TIMEOUT_S)
