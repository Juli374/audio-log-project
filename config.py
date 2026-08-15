"""Application configuration."""

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    # Speech recognition language (sent to the transcription API)
    language: str = "ru"

    # Audio recording
    sample_rate: int = 16000
    channels: int = 1
    dtype: str = "float32"
    # Frames per callback. Left at 0 (PortAudio's choice) CoreAudio hands us
    # ~15-sample buffers — over a thousand Python callbacks per second, each
    # needing the GIL. Whenever the main thread is busy (transcribing, UI,
    # an API call) those callbacks are late and audio is dropped: measured
    # ~30% loss, i.e. 300s of speech arriving as 213s. A 100 ms block cuts
    # the callback rate ~100x and the loss with it.
    blocksize: int = 1600

    # Hotkey (Right Option / Right Alt)
    hotkey_keycode: int = 61  # macOS virtual keycode for Right Option

    # System sounds (macOS)
    sound_start: str = "/System/Library/Sounds/Tink.aiff"
    sound_stop: str = "/System/Library/Sounds/Pop.aiff"
    sound_error: str = "/System/Library/Sounds/Basso.aiff"

    # Transcription
    # Post-hoc translation target via Claude. Empty string = off.
    # Supported codes: "en", "ru", "uk" (match Whisper source codes).
    target_language: str = ""
    min_duration: float = 0.5  # ignore recordings shorter than this (seconds)

    # Transcription mode: "groq" (Groq API) or "api" (OpenAI API).
    # Both are cloud services — there is no local model.
    transcription_mode: str = "groq"
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

    # Post-transcription cleanup via Claude.
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

    # ── Instant translation popup ──
    # Select text in any app, double-tap the translate key, and the
    # translation appears centred on screen (see translate_popup.py).
    # A real key combination, not a bare modifier: double-tapping Control
    # or Command collides with other apps' global shortcuts and with normal
    # typing. Valid ids live in hotkey.TRANSLATE_COMBOS.
    translate_enabled: bool = True
    translate_hotkey_combo: str = "ctrl+opt+t"  # ⌃⌥T
    translate_target: str = "ru"                # language to translate into

    # ── Auto-update ──
    # Only active in an installed .app bundle (see updater.py). auto_update
    # controls checking/downloading; auto_update_silent controls whether the
    # verified update is applied on its own (app restarts while idle) or waits
    # for the menu item.
    auto_update: bool = True
    auto_update_silent: bool = True

    # Logging
    log_level: str = "INFO"
    log_format: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    @property
    def data_dir(self) -> str:
        return str(Path.home() / "Library" / "Application Support" / "audio-log")

    @property
    def db_path(self) -> str:
        return str(Path(self.data_dir) / "history.db")

    @property
    def settings_path(self) -> str:
        return str(Path(self.data_dir) / "settings.json")

    def ensure_data_dir(self) -> None:
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)

    def load_settings(self) -> dict:
        """Load persistent settings from JSON file."""
        p = Path(self.settings_path)
        if p.exists():
            with open(p) as f:
                return json.load(f)
        return {
            "language": self.language,
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
            "translate_enabled": self.translate_enabled,
            "translate_hotkey_combo": self.translate_hotkey_combo,
            "translate_target": self.translate_target,
            "auto_update": self.auto_update,
            "auto_update_silent": self.auto_update_silent,
        }

    def apply_settings(self, settings: dict) -> None:
        """Apply settings dict to Config fields (in-memory)."""
        if "language" in settings:
            self.language = str(settings["language"])
        if "max_recording_seconds" in settings:
            self.max_recording_seconds = int(settings["max_recording_seconds"])
        if "hotkey_mode" in settings:
            self.hotkey_mode = str(settings["hotkey_mode"])
        if "hotkey_keycode" in settings:
            self.hotkey_keycode = int(settings["hotkey_keycode"])
        if "transcription_mode" in settings:
            mode = str(settings["transcription_mode"])
            # Legacy settings.json may still say "local" — that backend is
            # gone, so anything unknown lands on the cloud default.
            self.transcription_mode = mode if mode in ("groq", "api") else "groq"
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
        if "translate_enabled" in settings:
            self.translate_enabled = bool(settings["translate_enabled"])
        if "translate_hotkey_combo" in settings:
            self.translate_hotkey_combo = str(settings["translate_hotkey_combo"])
        if "translate_target" in settings:
            self.translate_target = str(settings["translate_target"])
        if "auto_update" in settings:
            self.auto_update = bool(settings["auto_update"])
        if "auto_update_silent" in settings:
            self.auto_update_silent = bool(settings["auto_update_silent"])

    def save_settings(self, settings: dict) -> None:
        """Save persistent settings to JSON file and apply to Config.

        Merges over what's already stored: callers (the settings UI) only
        send the fields they render, and a plain overwrite would silently
        drop everything else (auto_update_silent, anthropic_model, …)
        back to defaults on every save.
        """
        self.apply_settings(settings)
        self.ensure_data_dir()
        merged = self.load_settings()
        merged.update(settings)
        with open(self.settings_path, "w") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
