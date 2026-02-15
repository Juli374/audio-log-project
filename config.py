"""Application configuration."""

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    # Whisper model
    model_name: str = "small"
    model_dir: str = str(Path(__file__).parent / "models")
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
    translate: bool = False
    min_duration: float = 0.5  # ignore recordings shorter than this (seconds)

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

    @property
    def model_path(self) -> str:
        return str(Path(self.model_dir) / f"ggml-{self.model_name}.bin")

    def ensure_data_dir(self) -> None:
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)

    def ensure_model_dir(self) -> None:
        Path(self.model_dir).mkdir(parents=True, exist_ok=True)

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
        }

    def save_settings(self, settings: dict) -> None:
        """Save persistent settings to JSON file."""
        self.ensure_data_dir()
        with open(self.settings_path, "w") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
