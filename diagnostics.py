"""Self-diagnosis: why isn't this working on this machine?

Every failure the app can have on a fresh install is silent from the user's
side — the shortcut simply does nothing. This module answers the question
"what is missing here" without needing anyone to read a log file: each check
reports pass/fail plus what to do about it, and the whole thing renders into
a report the user can paste into a message.

Deliberately reports facts about configuration, never their values: whether a
key is set, never the key; how many characters were captured, never the text.
"""

import platform
import subprocess
import time
from datetime import datetime, timedelta
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
    """Tail the log for lines that indicate an actual failure.

    Keeps the traceback that follows an exception line — without it a report
    says "could not spawn update helper" and nothing about why.
    """
    if not _LOG_PATH.exists():
        return []
    try:
        with open(_LOG_PATH, errors="replace") as f:
            lines = f.readlines()[-4000:]
    except Exception:
        return []

    # Only today and yesterday: a week-old failure that has since been fixed
    # is noise, and it pushes the line that actually matters off the report.
    recent_days = {
        (datetime.now() - timedelta(days=d)).strftime("%Y-%m-%d")
        for d in (0, 1)
    }

    hits: list[str] = []
    trailing = 0
    for line in lines:
        if line[:10] not in recent_days and line[:1].isdigit():
            trailing = 0
            continue
        if any(marker in line for marker in _INTERESTING):
            hits.append(line.rstrip())
            trailing = 6 if "ERROR" in line else 0
        elif trailing and (line.startswith((" ", "\t")) or "Error" in line
                           or line.startswith("Traceback")):
            hits.append(line.rstrip())
            trailing -= 1
        else:
            trailing = 0
    return hits[-(limit * 3):]


def _audio_inputs() -> tuple[list[dict], str]:
    """Every input device macOS reports, and which one we would record from."""
    try:
        import sounddevice as sd
        from recorder import _is_virtual_device
    except Exception as e:
        return [], f"список устройств недоступен: {e}"

    try:
        devices = sd.query_devices()
        default_idx = sd.default.device[0]
    except Exception as e:
        return [], f"аудиосистема не отвечает: {e}"

    inputs = []
    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) < 1:
            continue
        inputs.append({
            "index": i,
            "name": d["name"],
            "is_default": i == default_idx,
            "filtered": _is_virtual_device(d["name"]),
        })

    if not inputs:
        return inputs, "микрофонов не найдено"

    try:
        from recorder import Recorder
        from config import Config
        chosen = Recorder(Config())._pick_input_device()
        picked = next((d["name"] for d in inputs if d["index"] == chosen),
                      "не выбран")
    except Exception as e:
        picked = f"выбор не удался: {e}"
    return inputs, picked


def probe_microphone(seconds: float = 2.0) -> dict:
    """Actually open the mic briefly and report what came back.

    More trustworthy than asking macOS for a permission status: a denied
    microphone still opens a stream, it just delivers digital silence. This
    distinguishes "no device", "device delivers nothing", "device delivers
    silence" and "working".
    """
    try:
        import numpy as np
        import sounddevice as sd

        from config import Config
        from recorder import Recorder
    except Exception as e:
        return {"ok": False, "message": f"аудиосистема недоступна: {e}"}

    cfg = Config()
    idx = Recorder(cfg)._pick_input_device()
    if idx is None:
        try:
            visible = ", ".join(
                d["name"] for d in sd.query_devices()
                if d.get("max_input_channels", 0) > 0) or "ни одного"
        except Exception:
            visible = "не удалось перечислить"
        return {"ok": False,
                "message": "Ни один микрофон не открылся. Видимые входы: "
                           f"{visible}. Если список пуст — подключи микрофон "
                           "или выбери наушники в Системных настройках → Звук "
                           "→ Вход (на Mac mini и Mac Studio своего микрофона "
                           "нет). Если список не пуст — проверь Системные "
                           "настройки → Конфиденциальность и безопасность → "
                           "Микрофон."}

    name = sd.query_devices()[idx]["name"]
    chunks: list = []
    try:
        with sd.InputStream(device=idx, samplerate=cfg.sample_rate, channels=1,
                            dtype="float32", blocksize=cfg.blocksize,
                            callback=lambda i, f, t, s: chunks.append(i.copy())):
            time.sleep(seconds)
    except Exception as e:
        return {"ok": False,
                "message": f"Микрофон «{name}» не открывается: {e}. "
                           "Проверь Системные настройки → Конфиденциальность "
                           "и безопасность → Микрофон."}

    if not chunks:
        return {"ok": False,
                "message": f"Микрофон «{name}» открылся, но не отдал ни одного "
                           "звукового блока."}

    audio = np.concatenate(chunks).flatten()
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak == 0.0:
        return {"ok": False,
                "message": f"Микрофон «{name}» отдаёт полную тишину. Причины "
                           "по убыванию вероятности: не выдан доступ к "
                           "микрофону (Системные настройки → "
                           "Конфиденциальность и безопасность → Микрофон); "
                           "наушники сейчас не в режиме микрофона; выбран не "
                           "тот вход в Системные настройки → Звук."}

    return {"ok": True,
            "message": f"Микрофон «{name}» пишет (уровень {peak:.3f})."}


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
    inputs, picked = _audio_inputs()

    checks = [
        _ok("Микрофон найден", bool(inputs),
            f"пишем с «{picked}»" if inputs else "ни одного входа в системе",
            "" if inputs else
            "Подключи микрофон или наушники. На Mac mini и Mac Studio "
            "встроенного микрофона нет."),
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
        "audio_inputs": inputs,
        "audio_picked": picked,
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

    if data.get("audio_inputs"):
        lines += ["", "Звуковые входы:"]
        for d in data["audio_inputs"]:
            marks = []
            if d["is_default"]:
                marks.append("системный по умолчанию")
            if d["filtered"]:
                marks.append("отсеян как виртуальный")
            suffix = f"  ({', '.join(marks)})" if marks else ""
            lines.append(f"  [{d['index']}] {d['name']}{suffix}")
    else:
        lines += ["", "Звуковые входы: ни одного"]

    if data["problems"]:
        lines += ["", "Последние ошибки в логе:"]
        lines += [f"  {p}" for p in data["problems"]]
    else:
        lines += ["", "Ошибок в логе нет."]

    lines += ["", f"Полный лог: {data['log_path']}"]
    return "\n".join(lines)


def self_test(config) -> dict:
    """Actively exercise recording and translation, and report where it breaks.

    Runs the real steps — open the mic, then a live API call — so the result
    reflects what happens in use rather than what the configuration claims.
    """
    from output import is_trusted
    import translate_popup

    mic = probe_microphone()
    if not mic["ok"]:
        return {"ok": False, "stage": "microphone", "message": mic["message"]}

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
            "message": f"{mic['message']} Перевод работает — проверочная "
                       f"фраза: «{out.strip()}»"}
