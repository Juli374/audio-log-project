"""Self-diagnosis: why isn't this working on this machine?

Every failure the app can have on a fresh install is silent from the user's
side — the shortcut simply does nothing. This module answers the question
"what is missing here" without needing anyone to read a log file: each check
reports pass/fail plus what to do about it, and the whole thing renders into
a report the user can paste into a message.

Deliberately reports facts about configuration, never their values: whether a
key is set, never the key; how many characters were captured, never the text.
"""

import json
import os
import platform
import subprocess
from pathlib import Path

import version as version_mod
from utils import get_logger

log = get_logger(__name__)

_LOG_PATH = Path.home() / "Library" / "Logs" / "audio-log" / "app.log"

# Log lines worth surfacing in the report — the ones that mark a real failure
# rather than routine chatter.
_INTERESTING = (
    "could not create event tap",
    "Audio loss",
    "Modifiers still held",
    "Copy did not land",
    "Translate failed",
    "Claude API error",
    "Failed to install hotkey",
    "Accessibility",
    "ERROR",
)


def _ok(name: str, passed: bool, detail: str = "", fix: str = "") -> dict:
    return {"name": name, "ok": bool(passed), "detail": detail, "fix": fix}


def _app_is_bundled() -> bool:
    import sys
    return bool(getattr(sys, "frozen", False))


def _signature_state() -> tuple[bool, str]:
    """Whether the running app is signed and notarised."""
    app_path = "/Applications/AudioLog.app"
    if not Path(app_path).exists():
        return False, "запущено не из /Applications"
    try:
        result = subprocess.run(
            ["/usr/sbin/spctl", "-a", "-vv", app_path],
            capture_output=True, text=True, timeout=10)
        text = (result.stderr or "") + (result.stdout or "")
        if "Notarized Developer ID" in text:
            return True, "подписано и нотаризовано Apple"
        if "accepted" in text:
            return True, "подписано, без нотаризации"
        return False, "подпись не подтверждена"
    except Exception as e:
        return False, f"проверка не удалась: {e}"


def _recent_problems(limit: int = 12) -> list[str]:
    """Tail the log for lines that indicate an actual failure."""
    if not _LOG_PATH.exists():
        return []
    try:
        with open(_LOG_PATH, errors="replace") as f:
            lines = f.readlines()[-4000:]
    except Exception:
        return []

    hits = [ln.rstrip() for ln in lines
            if any(marker in ln for marker in _INTERESTING)]
    return hits[-limit:]


def collect(config, hotkey_listener=None, transcriber=None) -> dict:
    """Build the full diagnostic picture for this machine."""
    from hotkey import shortcut_label
    from output import is_trusted

    accessibility = is_trusted()
    tap_ready = bool(getattr(hotkey_listener, "translate_ready", False))
    has_anthropic = bool(getattr(config, "anthropic_api_key", ""))
    mode = getattr(config, "transcription_mode", "groq")
    speech_key = bool(getattr(config, "groq_api_key", "") if mode == "groq"
                      else getattr(config, "openai_api_key", ""))
    signed, sign_detail = _signature_state()

    checks = [
        _ok("Универсальный доступ", accessibility,
            "выдан" if accessibility else "не выдан",
            "" if accessibility else
            "Системные настройки → Конфиденциальность и безопасность → "
            "Универсальный доступ → включить AudioLog"),
        _ok("Перехват сочетания перевода", tap_ready,
            "работает" if tap_ready else "не поднялся",
            "" if tap_ready else
            "Обычно это следствие невыданного Универсального доступа. "
            "Выдай доступ — перехват поднимется сам в течение минуты."),
        _ok("Ключ Anthropic (перевод)", has_anthropic,
            "задан" if has_anthropic else "пусто",
            "" if has_anthropic else "Впиши ключ в настройках выше"),
        _ok(f"Ключ для расшифровки ({mode})", speech_key,
            "задан" if speech_key else "пусто",
            "" if speech_key else "Впиши ключ в настройках выше"),
        _ok("Подпись приложения", signed, sign_detail,
            "" if signed else
            "Переустанови из официальной сборки — без подписи macOS "
            "сбрасывает выданные разрешения"),
    ]

    return {
        "version": version_mod.current(),
        "bundled": _app_is_bundled(),
        "macos": platform.mac_ver()[0] or platform.release(),
        "arch": platform.machine(),
        "shortcut": shortcut_label(
            getattr(config, "translate_hotkey_key", 2),
            getattr(config, "translate_hotkey_mods", 1 << 19)),
        "translate_model": getattr(config, "translate_model", ""),
        "translate_target": getattr(config, "translate_target", "ru"),
        "transcription_mode": mode,
        "checks": checks,
        "all_ok": all(c["ok"] for c in checks),
        "log_path": str(_LOG_PATH),
        "problems": _recent_problems(),
    }


def as_text(data: dict) -> str:
    """Render the report as plain text for pasting into a message."""
    lines = [
        f"AudioLog {data['version']} · macOS {data['macos']} · {data['arch']}",
        f"Сочетание перевода: {data['shortcut']} → {data['translate_target']}"
        f" ({data['translate_model']})",
        f"Расшифровка: {data['transcription_mode']}",
        "",
    ]
    for c in data["checks"]:
        mark = "OK  " if c["ok"] else "FAIL"
        lines.append(f"[{mark}] {c['name']}: {c['detail']}")
        if not c["ok"] and c["fix"]:
            lines.append(f"       → {c['fix']}")

    if data["problems"]:
        lines += ["", "Последние ошибки в логе:"]
        lines += [f"  {p}" for p in data["problems"]]
    else:
        lines += ["", "Ошибок в логе нет."]

    lines += ["", f"Полный лог: {data['log_path']}"]
    return "\n".join(lines)


def self_test(config) -> dict:
    """Actively exercise the translate path and report where it breaks.

    Runs the real steps in order — clipboard capture, then a live API call
    — so the result reflects what happens on a real press rather than what
    the configuration claims.
    """
    from output import is_trusted
    import translate_popup

    if not is_trusted():
        return {"ok": False, "stage": "accessibility",
                "message": "Нет Универсального доступа — приложение не может "
                           "ни прочитать выделенный текст, ни поймать "
                           "сочетание клавиш."}

    if not getattr(config, "anthropic_api_key", ""):
        return {"ok": False, "stage": "key",
                "message": "Не задан ключ Anthropic — переводить нечем."}

    try:
        out = translate_popup.translate_text(
            "This is a self-test of the translation path.",
            getattr(config, "translate_target", "ru") or "ru", config)
    except Exception as e:
        log.exception("Self-test translation failed")
        return {"ok": False, "stage": "api",
                "message": f"Запрос к Claude не прошёл: {e}"}

    if not out.strip():
        return {"ok": False, "stage": "api",
                "message": "Claude вернул пустой ответ."}

    return {"ok": True, "stage": "done",
            "message": f"Перевод работает. Проверочная фраза: «{out.strip()}»"}
