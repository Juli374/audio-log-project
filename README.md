# AudioLog — голосовая диктовка для macOS

> Зажал клавишу — говоришь — отпустил — текст вставляется туда, где стоит курсор.

**Репозиторий:** https://github.com/Juli374/audio-log-project

## Что это

Фоновое приложение для macOS. Работает в любом приложении: браузер, мессенджер, IDE, заметки. Живёт в menu bar, запускается при логине.

Два режима транскрипции:
- **API** (рекомендуется) — OpenAI API, без локальной модели, 0 MB RAM, быстро, нужен интернет + API ключ
- **Local** — Whisper small через whisper.cpp, полностью оффлайн, ~500 MB на диск + RAM

Возможности:
- Русский и английский (переключение в меню)
- История транскрипций с поиском
- Заметки
- Автовставка текста в активное поле (Accessibility API, fallback на Cmd+V)
- Hold или toggle режим хоткея

## Требования

- macOS 12+ (Apple Silicon: M1/M2/M3/M4)
- Python 3.10+
- Xcode Command Line Tools

Если не установлены:
```bash
brew install python@3.12
xcode-select --install
```

## Установка

### 1. Клонировать

```bash
git clone https://github.com/Juli374/audio-log-project.git ~/audio-log-project
cd ~/audio-log-project
```

### 2. Запустить setup

```bash
bash setup.sh
```

Скрипт спросит режим:
```
Transcription mode:
  1) API — OpenAI API (no local model, needs API key, fast)
  2) Local — Whisper model (~500 MB download, works offline)

Choose [1/2, default=1]:
```

- Выбор **1** (API): попросит ввести OpenAI API ключ (`sk-...`), модель не качается
- Выбор **2** (Local): скачает модель Whisper small (~500 MB)

Что делает `setup.sh`:
- Находит Python 3.10+ автоматически
- Создаёт виртуальное окружение (.venv)
- Ставит зависимости (requirements.txt)
- Скачивает модель (только для Local)
- Создаёт settings.json с выбранным режимом
- Компилирует PasteHelper.app из Swift-исходника (для автовставки текста)

### 3. Установить как сервис

```bash
bash build.sh       # собрать и подписать AudioLog.app
bash install.sh     # поставить в /Applications + LaunchAgent
```

Приложение живёт в `/Applications/AudioLog.app` (не в папке проекта — авто-обновление
подменяет весь бандл). LaunchAgent запускает его при логине.

### 4. Выдать разрешения (один раз)

В **System Settings → Privacy & Security**:

1. **Accessibility** → добавить `/Applications/AudioLog.app`. Нужно для: хоткей + вставка текста
2. **Microphone** → появится автоматически при первой записи → нажать Allow

Разрешения привязаны к подписи Developer ID, поэтому обновления их не сбрасывают —
это делается один раз.

## Использование

После установки в menu bar появится иконка:
- 🎙 готов
- 🔴 запись
- ⏳ распознавание

**Как диктовать:**
1. Поставь курсор в любое текстовое поле
2. Зажми **Right Option** — говори
3. Отпусти — текст вставится в курсор

**Меню (клик на иконку в menu bar):**
- 🇷🇺 Русский / 🇺🇸 English — переключение языка
- 📋 История — окно с историей, заметками, поиском
- Клик на иконку в Dock — тоже открывает историю

## Управление

```bash
bash install.sh     # установить или перезапустить сервис
bash uninstall.sh   # остановить и удалить автозапуск
```

Логи: `~/Library/Logs/audio-log/`

Настройки: `~/Library/Application Support/audio-log/settings.json`

### Запуск вручную (для отладки)

```bash
cd ~/audio-log-project
source .venv/bin/activate
python run.py               # с menu bar
python run.py --no-menubar  # только терминал
```

## Смена режима транскрипции

Отредактировать `~/Library/Application Support/audio-log/settings.json`:

```json
{
  "transcription_mode": "api",
  "openai_api_key": "sk-...",
  "openai_model": "gpt-4o-mini-transcribe"
}
```

Или для локального режима:
```json
{
  "transcription_mode": "local"
}
```

После изменения — перезапустить: `bash install.sh`

## Обновления

Установленное приложение обновляется само. Раз в 4 часа (и через 90 секунд после
запуска) оно читает фид последнего релиза на GitHub:

```
https://github.com/Juli374/audio-log-project/releases/latest/download/appcast.json
```

Если версия в фиде выше — скачивает архив, проверяет его тремя способами
(sha256 из фида · подпись Developer ID + Team ID · Gatekeeper, то есть нотаризация),
складывает рядом с данными приложения и подменяет бандл, когда ничего не пишется и
не расшифровывается. Приложение перезапускается само через launchd; переустановка
не нужна. Настройки и история лежат в `~/Library/Application Support/audio-log`
и обновлением не затрагиваются.

Вручную: меню в menu bar → **🔄 Проверить обновления**.
Выключить: `"auto_update": false` в `settings.json` (или `AUDIOLOG_NO_UPDATE=1`).
Лог обновлений: `~/Library/Logs/audio-log/update.log`.

### Выпустить новую версию

Один раз на машине — сохранить пароль для нотаризации (app-specific password
с appleid.apple.com):

```bash
xcrun notarytool store-credentials AudioLog --apple-id "you@example.com" --team-id BHZHRHKZY4
```

Дальше на каждый релиз:

```bash
bash release.sh 1.3.1
```

Скрипт сам: поднимает `VERSION` → собирает → подписывает Developer ID →
нотаризует у Apple и клеит тикет → пакует zip → пишет `appcast.json` →
коммитит, ставит тег, пушит → создаёт GitHub Release с обоими файлами.
Установленные копии подхватят обновление в течение 4 часов.

Прогнать всё, кроме публикации: `bash release.sh 1.3.1 --dry-run`.

## Структура проекта

| Файл | Назначение |
|------|------------|
| `run.py` | Точка входа, защита от дублей (file lock) |
| `menubar.py` | Menu bar UI (rumps), переключение языков |
| `app.py` | Headless-режим (--no-menubar) |
| `config.py` | Настройки (dataclass + JSON) |
| `recorder.py` | Запись аудио (sounddevice) |
| `transcriber.py` | Транскрипция: local (whisper.cpp) или API (OpenAI) |
| `hotkey.py` | Глобальный хоткей (NSEvent), hold/toggle |
| `output.py` | Clipboard + PasteHelper (автовставка) |
| `PasteHelper.swift` | Нативный helper: Accessibility API → Cmd+V fallback |
| `overlay.py` | Плавающий статус поверх всех окон |
| `history_ui.py` | Окно истории (WKWebView) |
| `db.py` | SQLite: история, заметки |
| `ui/index.html` | Фронтенд истории |
| `version.py` | Версия приложения (из VERSION / Info.plist) |
| `updater.py` | Авто-обновление: фид, проверки, подмена бандла |
| `setup.sh` | Установка: venv + зависимости + модель + PasteHelper |
| `build.sh` | Сборка py2app + подпись Developer ID + нотаризация |
| `install.sh` | Установка в /Applications + LaunchAgent |
| `release.sh` | Выпуск версии: сборка, нотаризация, GitHub Release |
| `entitlements.plist` | Разрешения hardened runtime (микрофон, JIT, библиотеки) |
| `uninstall.sh` | Удаление сервиса (`--app` — и самого приложения) |
