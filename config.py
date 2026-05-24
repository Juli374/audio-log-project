"""Application configuration."""

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    # Whisper model
    model_name: str = "small"
    model_dir: str = ""  # set in __post_init__
    language: str = "ru"

    # Audio recording
    sample_rate: int = 16000
    channels: int = 1
    dtype: str = "float32"

    # Hotkey (Right Option / Right Alt)
    hotkey_keycode: int = 61  # macOS virtual keycode for Right Option

    # System sounds (macOS)
    sound_start: str = "/System/Library/Sounds/Tink.aiff"
    sound_stop: str = "/System/Library/Sounds/Pop.aiff"
    sound_error: str = "/System/Library/Sounds/Basso.aiff"

    # Transcription
    n_threads: int = 4
    # Post-hoc translation target via Claude. Empty string = off.
    # Supported codes: "en", "ru", "uk" (match Whisper source codes).
    target_language: str = ""
    min_duration: float = 0.5  # ignore recordings shorter than this (seconds)

    # Transcription mode: "local" (pywhispercpp) or "api" (OpenAI API)
    transcription_mode: str = "local"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini-transcribe"
    groq_api_key: str = ""
    groq_model: str = "whisper-large-v3-turbo"

    # Post-transcription translation via Claude (fast, high quality).
    # Whisper's built-in translate is low quality and mixes languages on
    # conversational audio — we transcribe in source language, then translate
    # text with Claude Haiku (cheap + fast).
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5-20251001"

    # Post-transcription cleanup via Claude. Short dictation only
    # (LongTranscriber bypasses this with apply_cleanup=False).
    # Modes:
    #   "off"        — no cleanup, raw transcript pasted
    #   "light"      — strict: punctuation, capitalization, obvious
    #                  errors. Preserves words verbatim when uncertain.
    #   "aggressive" — editor-style: replaces context-mismatched words,
    #                  fixes grammar, may restructure minimally. Goal is
    #                  a correct readable text. Meaning preserved.
    cleanup_mode: str = "off"

    # Safety: max recording duration (seconds)
    max_recording_seconds: int = 300

    # Hotkey mode: "hold" (hold to record) or "toggle" (press to start/stop)
    hotkey_mode: str = "hold"

    # ── Session recording (long mode) ──
    # Parallel pipeline: Left Option (keycode 58) toggles a long continuous
    # recording that streams to disk, is chunked on silence, transcribed in
    # parallel, and stitched back together. Does not affect short dictation.
    session_enabled: bool = True
    session_hotkey_keycode: int = 58            # Left Option
    # Double-tap avoids false triggers when user types Option+arrow/delete
    # etc. Set False if you don't use Left Option in keyboard shortcuts.
    session_hotkey_require_double_tap: bool = True
    session_chunk_minutes: int = 10             # target chunk length
    session_chunk_min_minutes: int = 5          # min chunk when seeking silence
    session_chunk_max_minutes: int = 15         # max chunk when seeking silence
    session_silence_rms: float = 0.005          # below this = silence
    session_silence_min_ms: int = 300           # silence duration for cut point
    session_parallel_workers: int = 4           # concurrent chunk transcriptions
    session_max_hours: int = 6                  # hard-stop safety
    session_warn_hours: int = 5                 # warn user at this point
    session_chunk_joiner: str = "\n\n"          # how to join chunk texts
    session_auto_delete_audio: bool = False     # keep WAV after transcription
    session_translate: bool = False             # v1: no translation for sessions

    # Logging
    log_level: str = "INFO"
    log_format: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    def __post_init__(self):
        if not self.model_dir:
            from utils import resource_path
            self.model_dir = str(resource_path() / "models")

    @property
    def data_dir(self) -> str:
        return str(Path.home() / "Library" / "Application Support" / "audio-log")

    @property
    def db_path(self) -> str:
        return str(Path(self.data_dir) / "history.db")

    @property
    def settings_path(self) -> str:
        return str(Path(self.data_dir) / "settings.json")

    @property
    def model_path(self) -> str:
        return str(Path(self.model_dir) / f"ggml-{self.model_name}.bin")

    @property
    def session_audio_dir(self) -> str:
        return str(Path(self.data_dir) / "sessions")

    def ensure_data_dir(self) -> None:
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)

    def ensure_model_dir(self) -> None:
        Path(self.model_dir).mkdir(parents=True, exist_ok=True)

    def ensure_session_dir(self) -> None:
        Path(self.session_audio_dir).mkdir(parents=True, exist_ok=True)

    def load_settings(self) -> dict:
        """Load persistent settings from JSON file."""
        p = Path(self.settings_path)
        if p.exists():
            with open(p) as f:
                return json.load(f)
        return {
            "model": self.model_name,
            "language": self.language,
            "n_threads": self.n_threads,
            "max_recording_seconds": self.max_recording_seconds,
            "hotkey_mode": self.hotkey_mode,
            "hotkey_keycode": self.hotkey_keycode,
            "transcription_mode": self.transcription_mode,
            "openai_api_key": self.openai_api_key,
            "openai_model": self.openai_model,
            "groq_api_key": self.groq_api_key,
            "groq_model": self.groq_model,
            "anthropic_api_key": self.anthropic_api_key,
            "anthropic_model": self.anthropic_model,
            "cleanup_mode": self.cleanup_mode,
            "target_language": self.target_language,
            "session_enabled": self.session_enabled,
            "session_hotkey_keycode": self.session_hotkey_keycode,
            "session_hotkey_require_double_tap": self.session_hotkey_require_double_tap,
            "session_chunk_minutes": self.session_chunk_minutes,
            "session_parallel_workers": self.session_parallel_workers,
            "session_max_hours": self.session_max_hours,
            "session_auto_delete_audio": self.session_auto_delete_audio,
        }

    def apply_settings(self, settings: dict) -> None:
        """Apply settings dict to Config fields (in-memory)."""
        if "language" in settings:
            self.language = str(settings["language"])
        if "n_threads" in settings:
            self.n_threads = int(settings["n_threads"])
        if "max_recording_seconds" in settings:
            self.max_recording_seconds = int(settings["max_recording_seconds"])
        if "hotkey_mode" in settings:
            self.hotkey_mode = str(settings["hotkey_mode"])
        if "hotkey_keycode" in settings:
            self.hotkey_keycode = int(settings["hotkey_keycode"])
        if "transcription_mode" in settings:
            self.transcription_mode = str(settings["transcription_mode"])
        if "openai_api_key" in settings:
            self.openai_api_key = str(settings["openai_api_key"])
        if "openai_model" in settings:
            self.openai_model = str(settings["openai_model"])
        if "groq_api_key" in settings:
            self.groq_api_key = str(settings["groq_api_key"])
        if "groq_model" in settings:
            self.groq_model = str(settings["groq_model"])
        if "anthropic_api_key" in settings:
            self.anthropic_api_key = str(settings["anthropic_api_key"])
        if "anthropic_model" in settings:
            self.anthropic_model = str(settings["anthropic_model"])
        if "cleanup_mode" in settings:
            mode = str(settings["cleanup_mode"])
            if mode in ("off", "light", "aggressive"):
                self.cleanup_mode = mode
        elif "cleanup_enabled" in settings:
            # Backward compat: old boolean toggle migrates to "light".
            self.cleanup_mode = "light" if settings["cleanup_enabled"] else "off"
        if "target_language" in settings:
            self.target_language = str(settings["target_language"])
        if "session_enabled" in settings:
            self.session_enabled = bool(settings["session_enabled"])
        if "session_hotkey_keycode" in settings:
            self.session_hotkey_keycode = int(settings["session_hotkey_keycode"])
        if "session_hotkey_require_double_tap" in settings:
            self.session_hotkey_require_double_tap = bool(
                settings["session_hotkey_require_double_tap"])
        if "session_chunk_minutes" in settings:
            self.session_chunk_minutes = int(settings["session_chunk_minutes"])
        if "session_parallel_workers" in settings:
            self.session_parallel_workers = int(settings["session_parallel_workers"])
        if "session_max_hours" in settings:
            self.session_max_hours = int(settings["session_max_hours"])
        if "session_auto_delete_audio" in settings:
            self.session_auto_delete_audio = bool(settings["session_auto_delete_audio"])
        # model_name is not applied here — requires model reload on restart

    def save_settings(self, settings: dict) -> None:
        """Save persistent settings to JSON file and apply to Config."""
        self.apply_settings(settings)
        self.ensure_data_dir()
        with open(self.settings_path, "w") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
