"""Manual smoke test — record N seconds from a real microphone.

Usage:
    .venv/bin/python tests/smoke_session_recorder.py [seconds]

Defaults to 5 seconds. Prompts Mic permission on first run.
Writes WAV to a temp directory and opens it via `afplay` to verify.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import db
from config import Config
from session_recorder import SessionRecorder


def main() -> None:
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 5

    config = Config()
    config.ensure_data_dir()
    db.init_sessions_table()

    rec = SessionRecorder(config)
    print(f"→ Starting {seconds}s recording. Speak into the mic…")
    meta = rec.start()
    print(f"  session_id={meta.session_id}")
    print(f"  audio_path={meta.audio_path}")

    for remaining in range(seconds, 0, -1):
        print(f"  {remaining}s… (elapsed={rec.elapsed_seconds:.1f}s, "
              f"bytes={rec.bytes_written})", end="\r")
        time.sleep(1)

    print()
    print("→ Stopping…")
    final = rec.stop()

    print(f"  duration_sec  = {final.duration_sec:.2f}")
    print(f"  rms           = {final.rms:.4f}")
    print(f"  peak          = {final.peak:.4f}")
    print(f"  audio_path    = {final.audio_path}")
    print(f"  size          = {final.audio_path.stat().st_size / 1024:.1f} KB")

    row = db.session_get(final.session_id)
    print(f"  DB status     = {row['status']}")
    print(f"  DB duration   = {row['duration_sec']:.2f}")

    print()
    print("→ Playing back via `afplay`… (Ctrl-C to skip)")
    try:
        os.system(f"afplay '{final.audio_path}'")
    except KeyboardInterrupt:
        pass

    print("✅ Smoke test complete.")


if __name__ == "__main__":
    main()
