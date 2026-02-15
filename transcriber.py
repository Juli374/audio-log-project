"""Whisper transcription via pywhispercpp."""

import re

import numpy as np
from pywhispercpp.model import Model

from config import Config
from utils import get_logger

log = get_logger(__name__)

# Known Whisper hallucinations (short/silent audio produces these)
_HALLUCINATION_PATTERNS = [
    r"редактор\s+субтитров",
    r"субтитры\s+(сделал|делал|выполнил)",
    r"корректор\s+\w\.\s*\w+",
    r"продолжение\s+следует",
    r"спасибо\s+за\s+просмотр",
    r"подписывайтесь\s+на\s+канал",
    r"www\.",
    r"http",
]
_HALLUCINATION_RE = re.compile(
    "|".join(_HALLUCINATION_PATTERNS), re.IGNORECASE
)


class Transcriber:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._model: Model | None = None

    def load_model(self) -> None:
        log.info("Loading whisper model '%s' from %s …",
                 self._config.model_name, self._config.model_path)
        self._model = Model(
            self._config.model_path,
            n_threads=self._config.n_threads,
        )
        log.info("Model loaded successfully")

    def transcribe(self, audio: np.ndarray) -> str:
        if self._model is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        if len(audio) == 0:
            return ""

        duration = len(audio) / self._config.sample_rate
        if duration < self._config.min_duration:
            log.warning("Audio too short (%.2fs < %.2fs), skipping",
                        duration, self._config.min_duration)
            return ""

        segments = self._model.transcribe(
            audio,
            language=self._config.language,
            translate=self._config.translate,
        )

        text = " ".join(seg.text.strip() for seg in segments if seg.text.strip())

        if _HALLUCINATION_RE.search(text):
            log.warning("Filtered hallucination: %.80s", text)
            return ""

        log.info("Transcribed: %.80s…", text)
        return text
