# audio-log-project

Локальная диктовка для macOS. Зажал клавишу — говоришь — отпустил — текст вставляется в курсор. Работает везде: в браузере, мессенджерах, IDE, заметках.

- Полностью локально (Whisper small) или через OpenAI API
- Русский и английский язык (переключение в меню)
- Apple Silicon (M1/M2/M3) нативно
- Автозапуск при логине, иконка в menu bar
- История, заметки, поиск

## Требования

- macOS 12+ (Apple Silicon)
- Python 3.10+ (`brew install python@3.12`)
- Xcode Command Line Tools (`xcode-select --install`) — для сборки PasteHelper

## Установка

```bash
git clone <repo-url> ~/audio-log-project
cd ~/audio-log-project
bash setup.sh       # venv + зависимости + модель (~500 MB) + PasteHelper
bash install.sh     # установка как фоновый сервис
```

После установки — выдать разрешения (один раз):

1. **Accessibility** → System Settings → Privacy & Security → Accessibility → добавить приложение (install.sh покажет путь)
2. **Microphone** → появится автоматически при первой записи → нажать Allow

## Использование

Приложение работает в фоне. В menu bar видна иконка:
- 🎙 — готов
- 🔴 — запись
- ⏳ — распознавание

**Workflow:**
1. Поставь курсор куда нужно (любое приложение)
2. Зажми **Right Option** → говори
3. Отпусти → текст вставится в курсор

**Меню:**
- 🇷🇺/🇺🇸 — переключение языка (русский / английский)
- 📋 История — окно с историей транскрипций, заметками, поиском
- Клик на иконку в Dock — открывает историю

**Режим API (без локальной модели):**
Настраивается в `~/Library/Application Support/audio-log/settings.json`:
```json
{
  "transcription_mode": "api",
  "openai_api_key": "sk-...",
  "openai_model": "gpt-4o-mini-transcribe"
}
```
В API режиме модель не загружается — 0 MB RAM, быстрее, но нужен интернет.

## Управление сервисом

```bash
bash install.sh     # установить / перезапустить
bash uninstall.sh   # остановить и удалить автозапуск
```

Логи: `~/Library/Logs/audio-log/`

## Запуск вручную (для отладки)

```bash
source .venv/bin/activate
python run.py               # с menu bar
python run.py --no-menubar  # только терминал
```

## Структура

| Файл | Роль |
|------|------|
| `run.py` | Точка входа, single-instance lock |
| `menubar.py` | Menu bar UI (rumps) + переключение языков |
| `app.py` | Headless-оркестратор (--no-menubar) |
| `config.py` | Настройки (dataclass + JSON) |
| `recorder.py` | Запись аудио (sounddevice) |
| `transcriber.py` | Транскрипция: local (pywhispercpp) или API (OpenAI) |
| `hotkey.py` | Хоткей listener (NSEvent), hold/toggle режимы |
| `output.py` | Буфер обмена + PasteHelper (вставка в курсор) |
| `PasteHelper.swift` | Нативный helper для вставки текста (Accessibility API → Cmd+V fallback) |
| `overlay.py` | Плавающий оверлей статуса |
| `history_ui.py` | Окно истории (WKWebView) |
| `db.py` | SQLite: история, заметки |
| `feedback.py` | Звуковые сигналы |
| `ui/index.html` | Фронтенд истории |
| `setup.sh` | Установка: venv + зависимости + модель + PasteHelper |
| `install.sh` | LaunchAgent (автозапуск) |
