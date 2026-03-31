# Changelog

## [1.1.0] — 2026-03-25

### Added
- **Model heartbeat** — фоновый поток каждые 30 минут прогоняет 0.1с тишины через модель,
  чтобы все страницы памяти оставались "горячими" и macOS не свопила их на диск.
  Решает проблему деградации скорости транскрипции через 1-2 дня без перезагрузки.
  - Warmup-транскрипция при старте — подгружает все страницы памяти модели
  - `mlockall(MCL_CURRENT)` — попытка закрепить память (не работает на macOS, errno=78 ENOSYS)
  - Heartbeat как fallback — если mlockall не сработал, запускается фоновый heartbeat
  - Heartbeat НЕ мешает записи: пропускается если транскрипция в процессе (`_busy` lock)
- **Inference speed logging** — каждая транскрипция логирует ratio (время inference / длина аудио)
  и помечает SLOW если ratio > 1.0. Помогает отслеживать деградацию.
- `VERSION` файл для отслеживания версий
- `CHANGELOG.md` — лог изменений

### Changed
- `transcriber.py` → `LocalTranscriber`:
  - `load_model()`: добавлен warmup + mlockall + heartbeat
  - `transcribe()`: вынесена логика в `_do_transcribe()`, добавлен `_busy` lock
  - Новые поля: `_busy` (Lock), `_heartbeat_stop` (Event)
  - Новые методы: `_warmup()`, `_start_heartbeat()`, `_do_transcribe()`

### Technical details
- Корневая причина: whisper.cpp загружает модель через mmap. Через 1-2 дня macOS вытесняет
  неактивные страницы (~1.5GB для medium) в swap/compressed memory. Каждый inference потом
  тянет данные с диска — отсюда 4-10с вместо <1с.
- Ссылки: whisper.cpp#2605, llama.cpp#1876, ollama#4151
- Откат: заменить transcriber.py из git/бэкапа, удалить VERSION и CHANGELOG.md

## [1.0.0] — до 2026-03-25

Исходная версия: локальная диктовка, меню-бар, история, заметки, дневник, задачи.
Режимы: local (pywhispercpp) и API (OpenAI). Toggle/hold хоткей.
