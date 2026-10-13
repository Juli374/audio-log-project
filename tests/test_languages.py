"""Tests for moving dictations between languages.

The history keeps what was said next to what was pasted; switching the
language right after a dictation hands it over again in the new language;
the history window translates entries on request. Everything here runs
against a temporary database and fake Claude calls — no network, and the
real history is never opened.
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import db
import languages


class TempDbTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        patcher = patch.object(db, "_db_path", os.path.join(self._dir.name, "history.db"))
        patcher.start()
        self.addCleanup(patcher.stop)


class DatabaseTests(TempDbTestCase):
    def test_old_database_is_upgraded_in_place(self):
        conn = sqlite3.connect(db._db_path)
        conn.execute("""CREATE TABLE transcriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now','localtime')),
            text TEXT NOT NULL, duration_sec REAL, audio_rms REAL, audio_peak REAL)""")
        conn.execute("INSERT INTO transcriptions (text) VALUES ('старая запись')")
        conn.commit()
        conn.close()

        db.init_db()
        db.init_db()   # running twice changes nothing

        rows = db.get_recent()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "старая запись")
        self.assertIsNone(rows[0]["lang"])
        self.assertIsNone(db.get_translation(rows[0]["id"], "en"))

    def test_original_is_kept_only_when_it_differs(self):
        db.init_db()
        same = db.save("привет", lang="ru", source_text="привет", source_lang="ru")
        other = db.save("hello", lang="en", source_text="привет", source_lang="ru")
        self.assertIsNone(db.get_entry(same)["source_text"])
        self.assertEqual(db.get_entry(other)["source_text"], "привет")
        self.assertEqual(db.get_entry(other)["source_lang"], "ru")

    def test_search_finds_the_original_too(self):
        db.init_db()
        db.save("hello there", lang="en", source_text="привет всем", source_lang="ru")
        self.assertEqual(len(db.search("привет")), 1)
        self.assertEqual(len(db.search("hello")), 1)

    def test_editing_or_deleting_drops_cached_translations(self):
        db.init_db()
        a = db.save("один")
        b = db.save("два")
        db.put_translation(a, "en", "one")
        db.put_translation(b, "en", "two")
        db.update_entry(a, "один!")
        self.assertIsNone(db.get_translation(a, "en"))
        db.delete(b)
        self.assertIsNone(db.get_translation(b, "en"))


class LanguageTests(unittest.TestCase):
    def test_output_language(self):
        cfg = types.SimpleNamespace(language="ru", target_language="")
        self.assertEqual(languages.output_language(cfg), "ru")
        cfg.target_language = "en"
        self.assertEqual(languages.output_language(cfg), "en")

    def test_guess_language(self):
        self.assertEqual(languages.guess_language("Привет, как дела?"), "ru")
        self.assertEqual(languages.guess_language("Нагадай мені про звіт"), "uk")
        self.assertEqual(languages.guess_language("Remind me tomorrow"), "en")
        self.assertIsNone(languages.guess_language("1234 — !!"))


class ConvertTests(TempDbTestCase):
    def setUp(self):
        super().setUp()
        db.init_db()
        self.cfg = types.SimpleNamespace(language="ru", target_language="")

    def test_same_language_and_original_need_no_call(self):
        entry = {"id": None, "text": "Okay, what now?", "lang": "en",
                 "source_text": "Окей, и что теперь?", "source_lang": "ru"}
        translate = MagicMock()
        self.assertEqual(languages.convert(entry, "en", self.cfg, translate), entry["text"])
        self.assertEqual(languages.convert(entry, "ru", self.cfg, translate), entry["source_text"])
        translate.assert_not_called()

    def test_fresh_translation_starts_from_the_original_and_is_cached(self):
        row = db.save("Okay, what now?", lang="en",
                      source_text="Окей, и что теперь?", source_lang="ru")
        entry = db.get_entry(row)
        translate = MagicMock(return_value="Окей, і що тепер?")
        self.assertEqual(languages.convert(entry, "uk", self.cfg, translate), "Окей, і що тепер?")
        translate.assert_called_once_with("Окей, и что теперь?", "uk", self.cfg)
        self.assertEqual(db.get_translation(row, "uk"), "Окей, і що тепер?")
        # Second time: from the cache, no call.
        translate.reset_mock()
        self.assertEqual(languages.convert(entry, "uk", self.cfg, translate), "Окей, і що тепер?")
        translate.assert_not_called()

    def test_old_entries_are_guessed(self):
        row = db.save("Привет")   # saved before languages were recorded
        entry = db.get_entry(row)
        translate = MagicMock(return_value="Hi")
        self.assertEqual(languages.convert(entry, "ru", self.cfg, translate), "Привет")
        self.assertEqual(languages.convert(entry, "en", self.cfg, translate), "Hi")


class TranscriberMemoryTests(unittest.TestCase):
    """transcribe() remembers what was said when it pastes a translation."""

    def _transcriber(self, target):
        from config import Config
        from transcriber import GroqTranscriber
        cfg = Config()
        cfg.language = "ru"
        cfg.target_language = target
        cfg.cleanup_mode = "off"
        cfg.anthropic_api_key = "test"
        t = GroqTranscriber(cfg)
        t._transcribe_raw = lambda audio: "привет, как дела"
        return t

    def test_translated_dictation_keeps_the_original(self):
        t = self._transcriber("en")
        with patch("transcriber._translate_via_claude", return_value="hi, how are you"):
            out = t.transcribe(object())
        self.assertEqual(out, "hi, how are you")
        self.assertEqual(t.last_lang, "en")
        self.assertEqual(t.last_source_text, "привет, как дела")
        self.assertEqual(t.last_source_lang, "ru")

    def test_plain_dictation_has_no_separate_original(self):
        t = self._transcriber("")
        self.assertEqual(t.transcribe(object()), "привет, как дела")
        self.assertEqual(t.last_lang, "ru")
        self.assertIsNone(t.last_source_text)

    def test_failed_translation_is_recorded_as_spoken(self):
        t = self._transcriber("en")
        with patch("transcriber._translate_via_claude", side_effect=RuntimeError("down")):
            out = t.transcribe(object())
        self.assertEqual(out, "привет, как дела")
        self.assertEqual(t.last_lang, "ru")
        self.assertIsNone(t.last_source_text)


class RelangTests(unittest.TestCase):
    """Switching the language right after dictating hands the text over again."""

    def _app(self, target, age=5.0, recording=False):
        import menubar
        cfg = types.SimpleNamespace(language="ru", target_language=target)
        entry = {"id": None, "text": "hi, how are you", "lang": "en",
                 "source_text": "привет, как дела", "source_lang": "ru"}
        app = types.SimpleNamespace(
            _config=cfg, _is_recording=recording, _is_processing=False,
            _last_dictation=(time.monotonic() - age, entry),
            _relang_worker=MagicMock())
        return menubar, app, entry

    def _run(self, menubar, app):
        started = []
        with patch.object(menubar.threading, "Thread") as thread:
            thread.side_effect = lambda target, args, **kw: types.SimpleNamespace(
                start=lambda: started.append((target, args)))
            menubar.MenuBarApp._relang_last(app)
        return started

    def test_switch_right_after_dictation_starts_a_hand_over(self):
        menubar, app, entry = self._app(target="")
        started = self._run(menubar, app)
        self.assertEqual(started, [(app._relang_worker, (entry, "ru"))])

    def test_late_switch_only_affects_the_next_dictation(self):
        menubar, app, _ = self._app(target="", age=menubar_window() + 1)
        self.assertEqual(self._run(menubar, app), [])

    def test_switch_to_the_language_it_already_is_in_does_nothing(self):
        menubar, app, _ = self._app(target="en")
        self.assertEqual(self._run(menubar, app), [])

    def test_no_hand_over_while_recording(self):
        menubar, app, _ = self._app(target="", recording=True)
        self.assertEqual(self._run(menubar, app), [])

    def test_hand_over_copies_and_shows_the_original(self):
        import menubar
        _, app, entry = self._app(target="")
        app._translator = types.SimpleNamespace(_popup=MagicMock())
        with patch.object(menubar, "copy_text") as copy, \
             patch.object(menubar.AppHelper, "callAfter",
                          side_effect=lambda f, *a, **k: f(*a, **k)):
            menubar.MenuBarApp._relang_worker(app, entry, "ru")
        copy.assert_called_once_with("привет, как дела")
        text, caption = app._translator._popup.show.call_args[0]
        self.assertEqual(text, "привет, как дела")
        self.assertIn("⌘V", caption)


def menubar_window():
    import menubar
    return menubar._RELANG_WINDOW


class DictationTranslationTests(unittest.TestCase):
    def test_uses_the_speech_prompt_and_the_chosen_model(self):
        import translate_popup
        cfg = types.SimpleNamespace(anthropic_api_key="test", translate_model="claude-sonnet-5")
        seen = {}

        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def read(self):
                return json.dumps({"content": [{"type": "text", "text": "Hello"}]}).encode()

        def fake_open(request, timeout):
            seen["payload"] = json.loads(request.data.decode())
            return Response()

        translate_popup._cache.clear()
        with patch.object(translate_popup, "_open_with_retry", side_effect=fake_open):
            self.assertEqual(translate_popup.translate_dictation("Привет", "en", cfg), "Hello")
            self.assertEqual(translate_popup.translate_dictation("Привет", "en", cfg), "Hello")
        self.assertEqual(seen["payload"]["model"], "claude-sonnet-5")
        self.assertIn("real-time translator", seen["payload"]["system"][0]["text"])
        translate_popup._cache.clear()


class SettingsSaveTests(unittest.TestCase):
    def test_partial_save_keeps_everything_else(self):
        from config import Config
        import history_ui
        cfg = Config()
        cfg.groq_api_key = "gsk_keep"
        cfg.anthropic_api_key = "sk-ant-keep"
        cfg.target_language = "en"
        cfg.save_settings = MagicMock()
        win = history_ui.HistoryWindow(cfg)
        callback = MagicMock()
        win._settings_saved_cb = callback
        win._on_message({"action": "save_settings", "cleanup_mode": "light"})
        saved = cfg.save_settings.call_args[0][0]
        self.assertEqual(saved["cleanup_mode"], "light")
        self.assertEqual(saved["groq_api_key"], "gsk_keep")
        self.assertEqual(saved["anthropic_api_key"], "sk-ant-keep")
        self.assertEqual(saved["target_language"], "en")
        callback.assert_called_once()

    def test_stats_line(self):
        from history_ui import stats_line
        self.assertEqual(
            stats_line({"total": 9257, "today": 33, "total_duration": 76 * 3600 + 29 * 60}),
            "9 257 записей · сегодня 33 · 76 ч 29 мин")
        self.assertEqual(stats_line({"total": 1, "today": 1, "total_duration": 3}),
                         "1 запись · сегодня 1 · 3 сек")
        self.assertEqual(stats_line({"total": 22, "today": 0, "total_duration": 125}),
                         "22 записи · сегодня 0 · 2 мин")


class MenuIconTests(unittest.TestCase):
    def test_badges_are_template_images(self):
        import menu_icons
        for code in languages.LANGS:
            image = menu_icons.language_badge(languages.BADGE[code])
            self.assertTrue(image.isTemplate())
            self.assertEqual(image.size().height, 15.0)

    def test_key_equivalents(self):
        import menu_icons
        self.assertEqual(menu_icons.key_equivalent(2), "d")    # ⌥D
        self.assertEqual(menu_icons.key_equivalent(21), "4")   # ⇧⌘4
        self.assertEqual(menu_icons.key_equivalent(122), "")   # F1: shown nowhere


if __name__ == "__main__":
    unittest.main()
