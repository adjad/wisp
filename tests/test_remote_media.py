"""Pictures and recordings to the cloud model in cloud-everything mode, over a mocked transport.

Synthetic only: run with a fresh WISP_HOME; sockets are forbidden and no Keychain
item is read. Covers the payload validator's content-part rules and caps, the
per-model input table, and each feature gate (pictures in Ask and look-at-screen,
voice, Meet, documents, the long-email digest): with remote_parity on and a model
that takes the input, one real remote client request with the right part shape;
otherwise today's message and no request. Image and audio bytes reach no log and
no debug capture.
"""
import asyncio
import base64
import copy
import json
import logging
import os
import struct
import zlib
from unittest.mock import patch

import httpx

from tests.test_remote_inference_policy import RemoteFixture, policy, EndpointConfigurationError
from service.inference.omlx_client import OMLXClient, _audio_estimates

GEMINI = "google/gemini-3.8-flash"
HAIKU = "anthropic/claude-haiku-5.5"
FULL = {"context_window": 32000, "max_tokens": 8192, "request_bytes": 8 * 1024 * 1024,
        "messages": 1000, "aggregate_bytes": 4 * 1024 * 1024, "tools": 128,
        "response_bytes": 8 * 1024 * 1024}


def png(w=4, h=4):
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    raw = b"".join(b"\x00" + b"\x80\x40\xc0" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def image_url(data=None, kind="png"):
    return f"data:image/{kind};base64," + base64.b64encode(data or png()).decode()


def wav(seconds=1.0):
    data = b"\x01\x00" * int(seconds * 16_000)
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(data), b"WAVE", b"fmt ", 16,
                       1, 1, 16_000, 32_000, 2, 16, b"data", len(data)) + data


def b64(data):
    return base64.b64encode(data).decode()


def run(coro):
    return asyncio.run(coro)


def reply(text, finish="stop"):
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": finish}]}


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class MediaBase(RemoteFixture):
    profile_overrides = {**FULL, "model_id": GEMINI}
    parity = True

    def setUp(self):
        super().setUp()
        self.cfg["remote_parity"] = self.parity
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("WISP_REMOTE_PARITY", None)
        self.bodies = []

    def use_model(self, model, *, parity=None):
        self.profile["model_id"] = model
        self.cfg["inference"]["remote_only"]["model_id"] = model
        if parity is not None:
            self.cfg["remote_parity"] = parity
        policy.remote_only_profile.cache_clear()

    def payload(self, content, **extra):
        return {"model": self.profile["model_id"], "stream": False, "max_tokens": 100,
                "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": content}],
                **extra}

    def validate(self, content):
        policy.remote_only_profile().validate_payload(self.payload(content))

    def refused(self, content, match=None):
        with self.assertRaises(EndpointConfigurationError) as caught:
            self.validate(content)
        if match:
            self.assertIn(match, str(caught.exception))

    def client(self, replies):
        def handle(request):
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": self.profile["model_id"]}]})
            self.bodies.append(json.loads(request.content))
            return httpx.Response(200, json=replies[len(self.bodies) - 1])
        with patch("service.inference.attributed_transport.CredentialTransport",
                   return_value=httpx.MockTransport(handle)):
            return OMLXClient(target=policy.remote_default_target("agent"))


class ValidatorRules(MediaBase):
    def test_text_image_and_audio_parts_pass_for_a_model_that_takes_them(self):
        self.validate([{"type": "text", "text": "what is this"},
                       {"type": "image_url", "image_url": {"url": image_url()}}])
        self.validate([{"type": "input_audio", "input_audio": {"data": b64(wav()), "format": "wav"}},
                       {"type": "text", "text": "transcribe"}])
        self.validate([{"type": "input_audio", "input_audio": {"data": b64(b"ID3" + b"\0" * 30), "format": "mp3"}}])
        self.validate("plain text still works")
        self.validate([{"type": "image_url", "image_url": {"url": image_url(kind="jpeg")}}])
        self.validate([{"type": "image_url", "image_url": {"url": image_url(kind="webp")}}])

    def test_parity_off_refuses_every_part_list(self):
        self.use_model(GEMINI, parity=False)
        self.refused([{"type": "text", "text": "x"}], "text and tool transcripts only")
        self.refused([{"type": "image_url", "image_url": {"url": image_url()}}])
        self.validate("plain text")

    def test_modality_follows_the_model(self):
        self.use_model(HAIKU)
        self.validate([{"type": "image_url", "image_url": {"url": image_url()}}])
        self.refused([{"type": "input_audio", "input_audio": {"data": b64(wav()), "format": "wav"}}],
                     "can't hear audio")
        self.use_model("example/unlisted-model")
        self.refused([{"type": "image_url", "image_url": {"url": image_url()}}], "can't read pictures")
        self.validate([{"type": "text", "text": "text parts are fine for any model"}])
        self.assertEqual(policy.remote_input_modalities("example/unlisted-model"), frozenset({"text"}))
        self.assertEqual(policy.remote_input_modalities(None), frozenset({"text"}))
        for model in ("google/gemini-3.8-flash", "google/gemini-4-carbon", "google/gemini-4-argon"):
            self.assertTrue({"image", "audio"} <= policy.remote_input_modalities(model))
        for model in (HAIKU, "anthropic/claude-sonnet-5.5", "openai/gpt-6-luna"):
            self.assertIn("image", policy.remote_input_modalities(model))
            self.assertNotIn("audio", policy.remote_input_modalities(model))

    def test_only_data_urls_of_three_picture_types(self):
        for url in ("https://example.invalid/cat.png", "http://example.invalid/x.png",
                    "file:///etc/passwd", "data:image/gif;base64," + b64(b"GIF89a"),
                    "data:image/svg+xml;base64," + b64(b"<svg/>"), "data:text/plain;base64,QUJD",
                    "data:image/png," + "AAAA", "data:image/png;base64,not base64!!",
                    "data:image/png;base64,", "data:image/png;base64,AAA"):
            self.refused([{"type": "image_url", "image_url": {"url": url}}])

    def test_part_shapes_are_exact(self):
        bad = [
            [],
            [{"type": "text"}],
            [{"type": "text", "text": 3}],
            [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}],
            [{"type": "image_url", "image_url": {"url": image_url(), "detail": "high"}}],
            [{"type": "image_url", "image_url": image_url()}],
            [{"type": "image_url", "image_url": {"url": image_url()}, "extra": 1}],
            [{"type": "input_audio", "input_audio": {"data": b64(wav()), "format": "flac"}}],
            [{"type": "input_audio", "input_audio": {"data": b64(wav())}}],
            [{"type": "input_audio", "input_audio": {"url": "https://example.invalid/a.wav", "format": "wav"}}],
            [{"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AAAA"}}],
            [{"type": "file", "file": {"file_data": "AAAA"}}],
            ["just a string"],
            [{"type": "text", "text": "x"}] * (policy.REMOTE_CONTENT_PARTS + 1),
        ]
        for content in bad:
            self.refused(content)

    def test_parts_only_on_user_messages(self):
        p = policy.remote_only_profile()
        for role in ("system", "assistant", "tool"):
            payload = self.payload("x")
            payload["messages"].append({"role": role, "content": [{"type": "text", "text": "x"}]})
            with self.assertRaises(EndpointConfigurationError):
                p.validate_payload(payload)

    def test_size_and_count_caps(self):
        big = b"\x89PNG" + b"\0" * (policy.REMOTE_IMAGE_MAX_BYTES + 1)
        self.refused([{"type": "image_url", "image_url": {"url": image_url(big)}}], "too large")
        just = b"\x89PNG" + b"\0" * (policy.REMOTE_IMAGE_MAX_BYTES - 4)
        self.validate([{"type": "image_url", "image_url": {"url": image_url(just)}}])
        one = {"type": "image_url", "image_url": {"url": image_url()}}
        self.validate([one] * policy.REMOTE_IMAGES_PER_REQUEST)
        self.refused([one] * (policy.REMOTE_IMAGES_PER_REQUEST + 1), "Too many pictures")
        clip = {"type": "input_audio", "input_audio": {"data": b64(wav()), "format": "wav"}}
        self.refused([clip, clip], "Too many recordings")
        long_audio = b"\0" * (policy.REMOTE_AUDIO_MAX_BYTES + 1)
        self.refused([{"type": "input_audio", "input_audio": {"data": b64(long_audio), "format": "wav"}}],
                     "too large")

    def test_all_media_in_a_request_share_one_cap(self):
        four = b"\x89PNG" + b"\0" * (4 * 1024 * 1024)
        part = {"type": "image_url", "image_url": {"url": image_url(four)}}
        # Three 4 MiB pictures (16 MiB encoded) fit; a fourth is past the shared cap.
        payload = self.payload([part, part, part])
        payload["messages"].append({"role": "user", "content": [part]})
        with self.assertRaises(EndpointConfigurationError) as caught:
            policy.remote_only_profile().validate_payload(payload)
        self.assertIn("too large for one cloud request", str(caught.exception))

    def test_picture_and_recording_counts_span_the_whole_request(self):
        clip = {"type": "input_audio", "input_audio": {"data": b64(wav()), "format": "wav"}}
        payload = self.payload([clip])
        payload["messages"].append({"role": "user", "content": [clip]})
        with self.assertRaises(EndpointConfigurationError) as caught:
            policy.remote_only_profile().validate_payload(payload)
        self.assertIn("Too many recordings", str(caught.exception))
        one = {"type": "image_url", "image_url": {"url": image_url()}}
        payload = self.payload([one] * policy.REMOTE_IMAGES_PER_REQUEST)
        payload["messages"].append({"role": "user", "content": [one]})
        with self.assertRaises(EndpointConfigurationError) as caught:
            policy.remote_only_profile().validate_payload(payload)
        self.assertIn("Too many pictures", str(caught.exception))

    def test_the_data_url_prefix_is_exact(self):
        upper = "DATA:IMAGE/PNG;BASE64," + b64(png())
        self.refused([{"type": "image_url", "image_url": {"url": upper}}], "never a link")

    def test_media_leave_the_transcript_budget_but_the_body_stays_bounded(self):
        # A small transcript budget still takes a 3 MiB screenshot: media are counted
        # against their own cap, not aggregate_bytes or request_bytes.
        self.profile_small = dict(self.cfg["inference"]["remote_only"], request_bytes=8192,
                                  aggregate_bytes=4096)
        self.cfg["inference"]["remote_only"] = self.profile_small
        policy.remote_only_profile.cache_clear()
        shot = b"\x89PNG" + b"\0" * (3 * 1024 * 1024)
        self.validate([{"type": "text", "text": "what is on my screen"},
                       {"type": "image_url", "image_url": {"url": image_url(shot)}}])
        # The text around it is still held to the transcript budget.
        self.refused([{"type": "text", "text": "x" * 5000},
                      {"type": "image_url", "image_url": {"url": image_url(shot)}}], "byte budget")

    def test_media_bytes_never_appear_in_a_refusal(self):
        secret = b"\x89PNG" + b"SECRET-PIXELS" * 10
        for content in ([{"type": "image_url", "image_url": {"url": image_url(secret), "detail": "x"}}],
                        [{"type": "input_audio", "input_audio": {"data": b64(b"SECRET-AUDIO" * 9), "format": "ogg"}}]):
            with self.assertRaises(EndpointConfigurationError) as caught:
                self.validate(content)
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertNotIn(b64(secret)[:20], str(caught.exception))

    def test_cloud_media_allowed_and_budget(self):
        self.assertTrue(policy.cloud_media_allowed("image") and policy.cloud_media_allowed("audio"))
        self.use_model(HAIKU)
        self.assertTrue(policy.cloud_media_allowed("image"))
        self.assertFalse(policy.cloud_media_allowed("audio"))
        self.use_model(GEMINI, parity=False)
        self.assertFalse(policy.cloud_media_allowed("image") or policy.cloud_media_allowed("audio"))
        self.assertEqual(policy.cloud_media_budget(240), policy.REMOTE_MEDIA_MIN_TOKENS)
        self.assertEqual(policy.cloud_media_budget(9000), 8192)   # never past the profile


class ClientFitsMedia(MediaBase):
    def test_a_30_second_clip_fits_the_window_and_goes_out_whole(self):
        data = b64(wav(30))
        messages = [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": data, "format": "wav"}},
            {"type": "text", "text": "transcribe"}]}]
        estimated = _audio_estimates(messages)
        self.assertIsNot(estimated, messages)
        self.assertNotIn(data[:100], json.dumps(estimated))
        client = self.client([reply("Hello there.")])
        run(client.chat(GEMINI, messages, temperature=0, max_tokens=4096))
        part = self.bodies[0]["messages"][0]["content"][0]
        self.assertEqual(part, {"type": "input_audio", "input_audio": {"data": data, "format": "wav"}})

    def test_text_only_messages_are_untouched_by_the_estimate(self):
        messages = [{"role": "user", "content": "x"}]
        self.assertIs(_audio_estimates(messages), messages)


class PicturesOnTheCloud(MediaBase):
    def setUp(self):
        super().setUp()
        import service.vision as V
        self.V = V

    def test_answer_sends_image_parts_with_the_local_prompt_and_untrusted_framing(self):
        client = self.client([reply("A flyer for a concert.")])
        result = run(self.V.answer("what is this?", [image_url()], client=client, model=GEMINI))
        self.assertEqual(result.answer, "A flyer for a concert.")
        body = self.bodies[0]
        self.assertEqual(body["model"], GEMINI)
        self.assertNotIn("chat_template_kwargs", body)
        self.assertEqual(body["max_tokens"], policy.REMOTE_MEDIA_MIN_TOKENS)
        self.assertEqual(body["temperature"], 0)
        system, user = body["messages"]
        self.assertIn("untrusted content", system["content"])
        self.assertEqual(user["content"][0], {"type": "image_url", "image_url": {"url": image_url()}})
        self.assertEqual(user["content"][-1]["type"], "text")

    def test_resolve_picks_the_cloud_model_only_with_parity_and_eyes(self):
        sentinel = object()
        with patch("service.tools.imessage_tools._c", return_value=sentinel):
            self.assertEqual(self.V._resolve(), (sentinel, GEMINI))
            self.use_model(HAIKU)
            self.assertEqual(self.V._resolve(), (sentinel, HAIKU))
            self.use_model("example/unlisted-model")
            with self.assertRaisesRegex(self.V.VisionUnavailable, "can't read pictures"):
                self.V._resolve()
            self.use_model(GEMINI, parity=False)
            with self.assertRaisesRegex(self.V.VisionUnavailable, "only with a model on this Mac"):
                self.V._resolve()

    def test_without_eyes_nothing_is_sent(self):
        client = self.client([])
        self.use_model("example/unlisted-model")
        client.target = policy.remote_default_target("agent")
        with self.assertRaises(self.V.VisionUnavailable):
            run(self.V.answer("?", [image_url()], client=client, model="example/unlisted-model"))
        self.assertEqual(self.bodies, [])

    def test_look_at_screen_reads_through_the_cloud_model(self):
        client = self.client([reply("TEXT:\nInvoice 42\nDESCRIPTION:\nA bill.")])
        reading = run(self.V.read_picture("add this to my reminders", image_url(),
                                          label="Mail — Invoice", client=client, model=GEMINI))
        self.assertTrue(reading.startswith("TEXT:\nInvoice 42"))
        body = self.bodies[0]
        self.assertIn("capture of the person's screen", body["messages"][0]["content"])
        self.assertIn("untrusted content", body["messages"][0]["content"])
        self.assertEqual(body["messages"][1]["content"][0]["type"], "image_url")
        self.assertNotIn("chat_template_kwargs", body)

    def test_cloud_picture_caps_are_a_plain_sentence(self):
        client = self.client([])
        large = image_url(png() + b"\0" * (policy.REMOTE_IMAGE_MAX_BYTES + 1))   # fine locally (8 MiB)
        self.V.decode_image(large)
        with self.assertRaisesRegex(ValueError, "too large to send to the cloud model"):
            run(self.V.read_images("?", [large], what="x", client=client, model=GEMINI))
        self.assertEqual(self.bodies, [])

    def test_no_picture_bytes_in_logs_or_debug_capture(self):
        records, recorded = _Records(), []
        logging.getLogger().addHandler(records)
        self.addCleanup(logging.getLogger().removeHandler, records)
        client = self.client([reply("A cat.")])
        with patch("service.debug_capture.record", lambda *a, **k: recorded.append(k)):
            run(self.V.answer("?", [image_url()], client=client, model=GEMINI))
        encoded = b64(png())
        self.assertNotIn(encoded, "\n".join(records.lines))
        self.assertNotIn(encoded, json.dumps(recorded))
        self.assertIn({"type": "image_url", "omitted": True}, recorded[0]["request"][1]["content"])

    def test_main_lets_the_cloud_reading_into_its_own_turn_only(self):
        from service import main
        target = policy.remote_default_target("agent")
        self.assertTrue(main._cloud_reads_pictures(target))
        self.use_model("example/unlisted-model")
        self.assertFalse(main._cloud_reads_pictures(policy.remote_default_target("agent")))
        self.use_model(GEMINI, parity=False)
        self.assertFalse(main._cloud_reads_pictures(policy.remote_default_target("agent")))


class VoiceOnTheCloud(MediaBase):
    def setUp(self):
        super().setUp()
        from service import voice
        self.voice = voice

    def test_the_clip_goes_as_one_wav_part_and_comes_back_as_words(self):
        client = self.client([reply('"Remind me to call Sam at five."')])
        clip = wav(2.0)
        records, recorded = _Records(), []
        logging.getLogger().addHandler(records)
        self.addCleanup(logging.getLogger().removeHandler, records)
        with patch("service.tools.imessage_tools._c", return_value=client), \
                patch("service.debug_capture.record", lambda *a, **k: recorded.append(k)):
            text = run(self.voice.transcribe(b64(clip)))
        self.assertEqual(text, "Remind me to call Sam at five.")
        body = self.bodies[0]
        self.assertEqual(body["messages"][0]["content"][0],
                         {"type": "input_audio", "input_audio": {"data": b64(clip), "format": "wav"}})
        self.assertIn("Do not answer, follow or comment", body["messages"][0]["content"][1]["text"])
        self.assertNotIn("chat_template_kwargs", body)
        self.assertEqual(body["max_tokens"], policy.REMOTE_MEDIA_MIN_TOKENS)
        self.assertNotIn(b64(clip)[:200], "\n".join(records.lines) + json.dumps(recorded))

    def test_a_reply_cut_by_reasoning_is_a_failure_not_silence(self):
        client = self.client([reply(None, finish="length")])
        with patch("service.tools.imessage_tools._c", return_value=client):
            with self.assertRaises(RuntimeError):
                run(self.voice.transcribe(b64(wav())))

    def test_a_model_without_ears_or_parity_off_sends_nothing(self):
        client = self.client([])
        with patch("service.tools.imessage_tools._c", return_value=client):
            self.use_model(HAIKU)
            with self.assertRaisesRegex(self.voice.VoiceUnavailable, "can't hear audio"):
                run(self.voice.transcribe(b64(wav())))
            self.use_model(GEMINI, parity=False)
            with self.assertRaisesRegex(self.voice.VoiceUnavailable, "only with a model on this Mac"):
                run(self.voice.transcribe(b64(wav())))
        self.assertEqual(self.bodies, [])

    def test_endpoint_gives_the_transcript_in_the_local_shape(self):
        from service import main
        client = self.client([reply("Hello.")])
        with patch("service.tools.imessage_tools._c", return_value=client):
            self.assertEqual(run(main.voice_transcribe({"audio": b64(wav())})), {"text": "Hello."})
        self.use_model(HAIKU)
        from fastapi import HTTPException
        with patch("service.tools.imessage_tools._c", return_value=client):
            with self.assertRaises(HTTPException) as caught:
                run(main.voice_transcribe({"audio": b64(wav())}))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("Gemini 3.8 Flash", caught.exception.detail)


class MeetOnTheCloud(MediaBase):
    def setUp(self):
        super().setUp()
        from service import meetings, meetings_api
        self.M, self.api = meetings, meetings_api

    def test_a_meeting_is_heard_and_written_up_by_the_cloud_model(self):
        notes = {"title": "Sync", "summary": "They planned the launch.", "decisions": [],
                 "action_items": [], "follow_up": "Thanks all."}
        client = self.client([reply("We ship Friday."), reply(json.dumps(notes))])
        with patch("service.tools.imessage_tools._c", return_value=client):
            meeting = run(self.M.start())
            progress = run(self.M.add_window(meeting["id"], 0, wav(1.0)))
            self.assertEqual(progress["text"], "We ship Friday.")
            self.assertTrue(progress["heard"])
            done = run(self.M.finish(meeting["id"]))
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["notes"]["summary"], "They planned the launch.")
        self.assertEqual(self.bodies[0]["messages"][0]["content"][0]["type"], "input_audio")
        notes_body = self.bodies[1]
        self.assertGreaterEqual(notes_body["max_tokens"], policy.REMOTE_MEDIA_MIN_TOKENS)
        self.assertIn("We ship Friday.", notes_body["messages"][1]["content"])
        self.assertNotIn("chat_template_kwargs", notes_body)

    def test_a_model_without_ears_refuses_the_start_with_a_plain_409(self):
        self.use_model(HAIKU)
        from fastapi import HTTPException
        with patch("service.tools.imessage_tools._c", return_value=self.client([])):
            with self.assertRaises(HTTPException) as caught:
                run(self.api.start_meeting(self.api.Start()))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertTrue(caught.exception.detail.startswith("The cloud model you chose"))
        self.assertIn("Meeting notes need one that can", caught.exception.detail)
        self.assertEqual(self.bodies, [])

    def test_the_list_says_where_the_audio_goes(self):
        self.assertEqual(self.api.audio_destination(), "inference.example")
        self.cfg["inference"]["remote_only"].update(base_url="https://openrouter.ai", api_prefix="/api/v1")
        policy.remote_only_profile.cache_clear()
        self.assertEqual(self.api.audio_destination(), "OpenRouter")
        self.assertEqual(run(self.api.list_meetings())["audio_destination"], "OpenRouter")
        # A model that can't hear: nothing will be sent, so the copy names no destination.
        self.cfg["inference"]["remote_only"]["model_id"] = HAIKU
        policy.remote_only_profile.cache_clear()
        self.assertIsNone(self.api.audio_destination())
        self.legacy()
        self.assertIsNone(self.api.audio_destination())

    def test_the_local_wording_reads_naturally(self):
        from service import voice
        error = self.api._refuse(voice.VoiceUnavailable("Voice works only with a model on this Mac."))
        self.assertEqual(error.detail, "Meeting notes work only with a model on this Mac.")


class DocumentsAndDigestOnTheCloud(MediaBase):
    def test_document_text_goes_to_any_cloud_model_with_parity(self):
        from service import documents
        sentinel = object()
        with patch("service.tools.imessage_tools._c", return_value=sentinel):
            self.use_model("example/unlisted-model")
            self.assertEqual(documents.local_model(), (sentinel, "example/unlisted-model"))
            self.use_model(GEMINI, parity=False)
            with self.assertRaisesRegex(Exception, "documents only with a model on this Mac"):
                documents.local_model()

    def test_document_answer_on_the_cloud(self):
        from service import documents
        client = self.client([reply("The total is $42.")])
        result = run(documents.answer_text("What is the total?", "bill.txt", "Total: $42",
                                           client=client, model=GEMINI))
        self.assertTrue(result.answer.startswith("The total is $42."))
        body = self.bodies[0]
        self.assertNotIn("chat_template_kwargs", body)
        self.assertIn("untrusted data", body["messages"][1]["content"])

    def test_long_email_digest_uses_the_cloud_model_with_parity(self):
        from service.tools import email_digest
        sentinel = object()
        with patch("service.tools.imessage_tools._c", return_value=sentinel):
            self.assertEqual(email_digest._resolve(), (sentinel, GEMINI, 32000))
            self.use_model(GEMINI, parity=False)
            with self.assertRaises(LookupError):
                email_digest._resolve()

    def test_digest_call_on_the_cloud(self):
        from service.tools import email_digest
        client = self.client([reply("Summary: the deadline moved to Friday.")])
        text = run(email_digest._call(client, GEMINI, {"sender": "a@example.invalid", "subject": "S"},
                                      "body " * 50, None, __import__("time").monotonic() + 10))
        self.assertIn("Friday", text)
        self.assertGreaterEqual(self.bodies[0]["max_tokens"], policy.REMOTE_MEDIA_MIN_TOKENS)
