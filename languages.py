"""The dictation languages, how they are shown, and moving text between them.

Three languages, each shown as a two-letter badge the way macOS shows
keyboard layouts: РУ, EN, УК. The menu, the menu bar and the history
window all use the same badges.

A dictation can be handed over in another language at any time — from the
history window, or by switching the language right after dictating. The
cheapest answer wins: the text as pasted, the original as spoken, a cached
translation, and only then a fresh one from Claude.
"""

LANGS = ("ru", "en", "uk")

BADGE = {"ru": "РУ", "en": "EN", "uk": "УК"}
NAME = {"ru": "Русский", "en": "English", "uk": "Українська"}
# "…на русском" — for messages about a result in that language.
IN_NAME = {"ru": "на русском", "en": "на английском", "uk": "на украинском"}

_UKRAINIAN_ONLY = frozenset("іїєґІЇЄҐ")


def output_language(config) -> str:
    """The language the next dictation will be pasted in."""
    target = (getattr(config, "target_language", "") or "").strip().lower()
    return target or (getattr(config, "language", "") or "ru")


def guess_language(text: str) -> str | None:
    """Rough guess for entries saved before languages were recorded."""
    cyrillic = latin = 0
    for ch in text or "":
        low = ch.lower()
        if "а" <= low <= "я" or low in "ёіїєґ":
            cyrillic += 1
        elif "a" <= low <= "z":
            latin += 1
    if not cyrillic and not latin:
        return None
    if cyrillic >= latin:
        return "uk" if any(c in _UKRAINIAN_ONLY for c in text) else "ru"
    return "en"


def entry_language(entry: dict) -> str | None:
    """The language an entry's text is in: recorded, or guessed."""
    return entry.get("lang") or guess_language(entry.get("text") or "")


def ready_text(entry: dict, target: str) -> str | None:
    """`entry` in `target` without a network call, or None if that needs one."""
    text = entry.get("text") or ""
    if entry_language(entry) == target:
        return text
    if entry.get("source_lang") == target and entry.get("source_text"):
        return entry["source_text"]
    entry_id = entry.get("id")
    if entry_id:
        import db
        return db.get_translation(entry_id, target)
    return None


def convert(entry: dict, target: str, config, translate=None) -> str:
    """`entry` in `target`: as pasted, as spoken, from cache, or translated.

    A fresh translation starts from the original when there is one, and is
    cached on the entry. Raises when Claude cannot be reached — callers say
    so rather than hand back the wrong language.
    """
    ready = ready_text(entry, target)
    if ready:
        return ready
    if translate is None:
        from translate_popup import translate_dictation as translate
    source = entry.get("source_text") or entry.get("text") or ""
    out = translate(source, target, config)
    entry_id = entry.get("id")
    if entry_id and out:
        import db
        db.put_translation(entry_id, target, out)
    return out
