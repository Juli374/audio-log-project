"""Tests for the Whisper hallucination filter in transcriber._is_hallucination.

Background: Whisper was trained on a huge corpus of YouTube/movie subtitles
and emits training-set phrases like "Спасибо за просмотр" or
"Редактор субтитров А.Семкин" on silence and noise. The regression test
here guards the decision to keep the filter AND the ratio-based approach
(match must cover ≥ 80% of the transcript) to avoid false positives on
legitimate long dictations.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class HallucinationFilterTests(unittest.TestCase):
    def _is_hallucination(self, text: str) -> bool:
        from transcriber import _is_hallucination
        return _is_hallucination(text)

    # ── positive matches: known hallucinations ──

    def test_empty_string(self) -> None:
        self.assertFalse(self._is_hallucination(""))

    def test_spasibo_za_prosmotr(self) -> None:
        self.assertTrue(self._is_hallucination("Спасибо за просмотр"))
        self.assertTrue(self._is_hallucination("Спасибо за просмотр!"))
        self.assertTrue(self._is_hallucination("спасибо за смотр"))

    def test_podpishites(self) -> None:
        self.assertTrue(self._is_hallucination(
            "Подписывайтесь на канал"))
        self.assertTrue(self._is_hallucination(
            "подписывайтесь на нас"))

    def test_subtitle_credits(self) -> None:
        self.assertTrue(self._is_hallucination(
            "Редактор субтитров А.Семкин"))
        self.assertTrue(self._is_hallucination(
            "Корректор А.Егорова"))
        self.assertTrue(self._is_hallucination(
            "Субтитры сделал DimaTorzok"))

    def test_english_variants(self) -> None:
        self.assertTrue(self._is_hallucination("Thanks for watching"))
        self.assertTrue(self._is_hallucination("Thank you for watching"))
        self.assertTrue(self._is_hallucination("Subscribe"))
        self.assertTrue(self._is_hallucination(
            "Don't forget to like and subscribe"))

    def test_subtitle_sources(self) -> None:
        self.assertTrue(self._is_hallucination("Amara.org"))
        self.assertTrue(self._is_hallucination("castingwords"))

    def test_continuation_phrases(self) -> None:
        self.assertTrue(self._is_hallucination("Продолжение следует"))
        self.assertTrue(self._is_hallucination("Всем пока"))
        self.assertTrue(self._is_hallucination("Ставьте лайк"))

    # ── negative matches: legitimate dictation ──

    def test_real_dictation_untouched(self) -> None:
        texts = [
            "Привет, как дела?",
            "Напомни мне завтра позвонить маме",
            "Создать заметку о встрече в три часа",
            "Показывай мне погоду на сегодня",
            "Открой браузер и найди рецепт борща",
            "Hello world, this is a test",
        ]
        for text in texts:
            with self.subTest(text=text):
                self.assertFalse(self._is_hallucination(text))

    def test_long_dictation_mentioning_subscribe_not_filtered(self) -> None:
        """The KEY regression guard. A 200-char real dictation that happens
        to mention "подписывайтесь на канал" should NOT be dropped —
        only stand-alone hallucinations should."""
        long_text = (
            "Напомни мне в конце видео попросить зрителей "
            "подписываться на канал и поставить лайк, "
            "а пока расскажи подробнее про третий пункт плана, "
            "который мы обсуждали на прошлой неделе."
        )
        self.assertFalse(self._is_hallucination(long_text))

    def test_short_legit_dictation_near_pattern_is_kept(self) -> None:
        """Borderline: if the matched phrase is < 80% of the text, keep it."""
        # "спасибо" alone is not filtered (not in patterns)
        self.assertFalse(self._is_hallucination("спасибо"))
        # But "Спасибо за просмотр и ещё кое-что важное" — the match is
        # ~50% of the text, so it's kept (user might really be reviewing video)
        self.assertFalse(self._is_hallucination(
            "Спасибо за просмотр и ещё кое-что важное сегодня"))

    # ── ratio threshold ──

    def test_standalone_hallucination_is_filtered(self) -> None:
        """Exact match as entire transcript → 100% ratio → filtered."""
        self.assertTrue(self._is_hallucination("спасибо за просмотр"))

    def test_ratio_at_threshold(self) -> None:
        """Matches covering ~80% of text are filtered; below 80% are kept."""
        # "редактор субтитров" = 18 chars. To hit 80% ratio, the whole
        # text must be ≤ 18 / 0.8 ≈ 22 chars.
        # "редактор субтитров!!!" = 21 chars, match 18, ratio = 18/21 ≈ 0.857 → FILTER
        self.assertTrue(self._is_hallucination("редактор субтитров!!!"))
        # "редактор субтитров и что-то ещё" = 31 chars, match 18, ratio ≈ 0.58 → KEEP
        self.assertFalse(self._is_hallucination(
            "редактор субтитров и что-то ещё"))


class PatternCoverageTests(unittest.TestCase):
    """Smoke test — make sure the regex compiles and covers every pattern
    we claim to cover. If someone accidentally breaks a pattern, this
    test catches it before prod."""

    def test_regex_compiles(self) -> None:
        from transcriber import _HALLUCINATION_RE
        # Just touch the attribute — compilation error would raise at import
        self.assertIsNotNone(_HALLUCINATION_RE)

    def test_all_known_samples_match(self) -> None:
        """Each of these should be caught by at least one pattern."""
        from transcriber import _HALLUCINATION_RE
        samples = [
            "Редактор субтитров А.Семкин",
            "Корректор А.Егорова",
            "Субтитры подогнал Симон",
            "Субтитры добавил DimaTorzok",
            "Спасибо за просмотр!",
            "Подписывайтесь на канал",
            "Подпишись",
            "Ставьте лайк и колокольчик",
            "Всем пока",
            "Продолжение следует",
            "Смотрите продолжение в следующей части",
            "Thanks for watching",
            "Please subscribe",
            "Amara.org",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertIsNotNone(
                    _HALLUCINATION_RE.search(sample),
                    f"Pattern regex missed: {sample!r}",
                )


if __name__ == "__main__":
    unittest.main()
