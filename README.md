# audio-log-project

Локальная диктовка для macOS. Зажал клавишу — говоришь — отпустил — текст вставляется в курсор. Работает везде: в браузере, мессенджерах, IDE, заметках.

- Полностью локально, без облака и подписок
- Whisper small для русского языка
- Apple Silicon (M1/M2/M3) нативно
- Автозапуск при логине, иконка в menu bar

## Установка (один раз)

```bash
cd ~/audio-log-project
bash setup.sh       # venv + зависимости + модель (~500 MB)
bash install.sh     # установка как фоновый сервис
```

После `install.sh` — выдать разрешения (один раз):

1. **Accessibility** → System Settings → Privacy & Security → Accessibility
   → добавить `.venv/bin/python` (путь покажет install.sh)
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
| `run.py` | Точка входа |
| `menubar.py` | Menu bar иконка (rumps) |
| `config.py` | Настройки |
| `app.py` | Оркестратор (режим --no-menubar) |
| `recorder.py` | Запись аудио (sounddevice) |
| `transcriber.py` | Транскрипция (pywhispercpp) + фильтр галлюцинаций |
| `hotkey.py` | Хоткей listener (pynput) |
| `output.py` | Буфер обмена + вставка (pbcopy → Cmd+V) |
| `feedback.py` | Звуковые сигналы |
