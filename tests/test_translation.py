"""Tests for the post-transcription translation pipeline.

Covers:
- BaseTranscriber.transcribe() wrapper behavior (passthrough vs translate)
- Fallback when Claude API fails
- Claude API request shape and response parsing
"""

import io
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Allow importing from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np  # noqa: E402

import transcriber  # noqa: E402
from config import Config  # noqa: E402
from transcriber import BaseTranscriber, _translate_via_claude  # noqa: E402


def _make_config(**overrides) -> Config:
    cfg = Config()
    cfg.language = "ru"
    cfg.anthropic_api_key = "test-key"
    cfg.anthropic_model = "claude-haiku-4-5-20251001"
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class FakeTranscriber(BaseTranscriber):
    """Test double — _transcribe_raw returns a canned value."""

    def __init__(self, config: Config, canned: str) -> None:
        super().__init__(config)
        self._canned = canned

    def load_model(self) -> None:
        pass

    def _transcribe_raw(self, audio):
        return self._canned


class BaseTranscriberWrapperTests(unittest.TestCase):
    def setUp(self):
        self.audio = np.zeros(16000, dtype=np.float32)  # 1s silence

    def test_passthrough_when_target_language_empty(self):
        cfg = _make_config(target_language="")
        t = FakeTranscriber(cfg, "привет мир")
        self.assertEqual(t.transcribe(self.audio), "привет мир")

    def test_passthrough_when_target_equals_source(self):
        cfg = _make_config(target_language="en", language="en")
        t = FakeTranscriber(cfg, "hello world")
        with patch.object(transcriber, "_translate_via_claude") as m:
            self.assertEqual(t.transcribe(self.audio), "hello world")
            m.assert_not_called()

    def test_empty_text_is_not_translated(self):
        cfg = _make_config(target_language="uk")
        t = FakeTranscriber(cfg, "")
        with patch.object(transcriber, "_translate_via_claude") as m:
            self.assertEqual(t.transcribe(self.audio), "")
            m.assert_not_called()

    def test_translate_invokes_claude_and_returns_result(self):
        cfg = _make_config(target_language="uk")
        t = FakeTranscriber(cfg, "привет мир")
        with patch.object(
            transcriber, "_translate_via_claude", return_value="привіт світ"
        ) as m:
            self.assertEqual(t.transcribe(self.audio), "привіт світ")
            m.assert_called_once()
            args, _ = m.call_args
            self.assertEqual(args[0], "привет мир")
            self.assertEqual(args[1], "uk")
            self.assertIs(args[2], cfg)

    def test_claude_failure_falls_back_to_source_text(self):
        cfg = _make_config(target_language="uk")
        t = FakeTranscriber(cfg, "привет мир")
        with patch.object(
            transcriber,
            "_translate_via_claude",
            side_effect=RuntimeError("boom"),
        ):
            # Should not raise; should return raw transcription
            self.assertEqual(t.transcribe(self.audio), "привет мир")


class ClaudeApiCallTests(unittest.TestCase):
    def _fake_response(self, content_text: str):
        payload = {
            "content": [{"type": "text", "text": content_text}],
            "stop_reason": "end_turn",
        }
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_builds_correct_request_and_parses_response(self):
        cfg = _make_config(target_language="en")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["timeout"] = timeout
            return self._fake_response("Hello, world!")

        with patch.object(transcriber.urllib.request, "urlopen", fake_urlopen):
            out = _translate_via_claude("привет мир", "en", cfg)

        self.assertEqual(out, "Hello, world!")
        self.assertEqual(captured["url"], "https://api.anthropic.com/v1/messages")
        # Headers are case-insensitive; urllib title-cases them
        headers_lower = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(headers_lower.get("x-api-key"), "test-key")
        self.assertEqual(headers_lower.get("anthropic-version"), "2023-06-01")
        self.assertEqual(headers_lower.get("content-type"), "application/json")

        body = captured["body"]
        self.assertEqual(body["model"], "claude-haiku-4-5-20251001")
        self.assertIn("system", body)
        self.assertIn("translator", body["system"].lower())
        self.assertEqual(len(body["messages"]), 1)
        self.assertEqual(body["messages"][0]["role"], "user")
        self.assertEqual(body["messages"][0]["content"], "привет мир")
        self.assertGreater(body["max_tokens"], 0)

    def test_missing_api_key_raises(self):
        cfg = _make_config(target_language="en", anthropic_api_key="")
        with self.assertRaises(RuntimeError):
            _translate_via_claude("что-то", "en", cfg)

    def test_http_error_raises_runtime_error(self):
        import urllib.error

        cfg = _make_config(target_language="en")

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 401,
                "Unauthorized",
                {},
                io.BytesIO(b'{"error":{"message":"bad key"}}'),
            )

        with patch.object(transcriber.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(RuntimeError):
                _translate_via_claude("привет", "en", cfg)

    def test_multiple_text_blocks_are_concatenated(self):
        cfg = _make_config(target_language="en")

        def fake_urlopen(req, timeout=None):
            payload = {
                "content": [
                    {"type": "text", "text": "Hello, "},
                    {"type": "text", "text": "world!"},
                ]
            }
            resp = MagicMock()
            resp.read.return_value = json.dumps(payload).encode("utf-8")
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch.object(transcriber.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(
                _translate_via_claude("привет мир", "en", cfg), "Hello, world!"
            )


if __name__ == "__main__":
    unittest.main()
