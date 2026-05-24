# Changelog

## [1.2.1] — 2026-04-24

### Fixed
- **Короткая диктовка теряла ~20–100 мс хвоста аудио** и съедала последнее
  слово/слог. Причина: в `Recorder.stop()` снапшот `_chunks` снимался ДО
  остановки стрима, а в callback проверка `_should_stop` стояла ПЕРЕД
  `append` — каждый callback между `_recording=False` и
  `_should_stop=True` дропал данные. Дополнительно `sd.CallbackAbort`
  (=`paAbort`) выбрасывает in-flight HAL ring buffer.
  - Фикс #1: callback сначала append, потом проверка стопа
  - Фикс #2: `sd.CallbackStop` (=`paComplete`) вместо `sd.CallbackAbort`
  - Фикс #3: `stop()` блокирует рабочий поток на `_stream_finished.wait()`
    ДО снапшота — гарантирует, что все callback'и уже отработали
  - Фикс #4: 150 мс padding тишины в конце аудио — Whisper's decoder
    нужна естественная граница сегмента, иначе теряется финальный токен
    ([HF transformers#23231](https://github.com/huggingface/transformers/issues/23231))
  - Файл: `recorder.py:30-235`
  - Регрессионный тест: `tests/test_recorder_drain.py` (8 тестов)
- **Короткая диктовка иногда выдавала YouTube-фразы вместо текста**
  («Спасибо за просмотр», «Редактор субтитров А.Семкин» и т.п.). Это
  хорошо задокументированные галлюцинации Whisper — при обучении модели
  было много YouTube-субтитров. Whisper-large-v3 выдаёт такие на 99.97%
  non-speech аудио ([Calm-Whisper, arXiv 2505.12969](https://arxiv.org/html/2505.12969v1)).
  Фильтр-регэкспы были удалены при рефакторинге v1.2.0 — вернули.
  - Расширенный community-список паттернов (русские + английские +
    subtitle-source credits) из
    [gist waveletdeboshir](https://gist.github.com/waveletdeboshir/8bf52f04bf78018194f25b2390c08309)
  - Критерий отсева: сумма длин всех матчей ≥ 65% от текста (было: ≥ 80%
    от одного матча). Защищает длинные легитимные диктовки с упоминанием
    «подписывайтесь» от ложного срабатывания.
  - Файл: `transcriber.py:20-82`, `transcriber.py:transcribe()`
  - Регрессионный тест: `tests/test_hallucination_filter.py` (14 тестов)

### Changed
- `LocalTranscriber._do_transcribe` теперь передаёт `no_context=True` в
  `pywhispercpp.Model.transcribe` (эквивалент `condition_on_previous_text=False`
  в upstream Whisper, `-mc 0` в whisper.cpp CLI). Предотвращает
  «снежный ком» галлюцинаций между внутренними сегментами и улучшает
  качество коротких высказываний
  ([whisper.cpp#1490](https://github.com/ggml-org/whisper.cpp/discussions/1490)).

## [1.2.0] — 2026-04-18

### Added
- **Длинный режим записи (session mode)** — Left Option двойной тап
  открывает долгую сессию (до 6 часов по умолчанию), которая стримится
  на диск, нарезается по тишине на ~10-минутные чанки, транскрибируется
  параллельно (4 worker'а) и сшивается обратно в один текст. Работает
  параллельно со старым коротким режимом диктовки (Right Option).
  - `session_recorder.py` — запись на диск через writer-thread с
    queue, константное потребление RAM
  - `long_transcriber.py` — VAD-like silence-based chunking
    (`find_silence_cuts`), retry логика для транзиентных ошибок
    (`_MAX_CHUNK_ATTEMPTS=3`), stitching через `session_chunk_joiner`
  - `SessionController` в `menubar.py` — state machine
    idle → recording → transcribing → idle; `cancel()` для отмены;
    hard-stop watchdog на `session_max_hours`
  - Sessions-таблица в SQLite с прогресс-репортингом
- **py2app-сборка**: `build.sh` + `setup.py` + `tools/create-dev-cert.sh`
  — standalone `AudioLog.app` без зависимости от Python/brew/xcode.
  Стабильная self-signed подпись «AudioLog Dev Local» сохраняет
  TCC-одобрение Accessibility между ребилдами.
- **In-process Cmd+V paste** через `Quartz.CGEventPost` в `output.py` —
  удалён внешний `PasteHelper.app`, одного Accessibility-гранта
  достаточно.
- Тонкая полоса-индикатор под menu-bar вместо всплывающих плашек
  (`overlay.py`) — запись подсвечивается волной, режимы different colors.

### Changed
- `hotkey.py`: добавлен второй keycode (`session_hotkey_keycode=58`,
  Left Option) с опциональным double-tap detection
  (`session_hotkey_require_double_tap=True`) для защиты от ложных
  срабатываний при наборе Option+arrow/delete.

## [1.1.2] — 2026-04-08

### Changed
- **Режим перевода на английский полностью переработан.** Встроенный `translate`
  у Whisper (и whisper.cpp, и OpenAI/Groq `/audio/translations`) на разговорной
  речи выдавал кашу из двух языков — это ограничение самой модели, а не
  конфигурации. Теперь two-stage pipeline:
  1. Whisper всегда транскрибирует в исходном языке (у русского это работает отлично).
  2. Текст переводится через **Anthropic Claude Haiku 4.5** — видит всю фразу
     целиком, сохраняет тон и контекст, тихо чинит оговорки и filler-слова,
     выдаёт натуральный английский.
- Новые поля в `Config`: `anthropic_api_key`, `anthropic_model` (по умолчанию
  `claude-haiku-4-5-20251001`). Ключ читается из
  `~/Library/Application Support/audio-log/settings.json`.
- `BaseTranscriber.transcribe()` стал обёрткой: вызывает `_transcribe_raw()`
  подкласса, затем при `translate=True` прогоняет текст через
  `_translate_via_claude()`. Все три подкласса (Local/OpenAI/Groq) больше НЕ
  используют whisper-level translate.
- Если Claude недоступен — fallback на исходный текст, запись не теряется.
  Пустой текст и уже-английский источник не переводятся (экономим вызовы).

### Added
- `tests/test_translation.py` — 9 unit-тестов на новую логику: passthrough при
  `translate=False`, passthrough для EN-источника, вызов Claude, fallback при
  ошибке API, пустой текст, корректность HTTP-запроса и заголовков к Anthropic,
  парсинг multi-block ответа, обработка 401.

## [1.1.1] — 2026-04-08

### Fixed
- **RU→EN переключатель через Groq давал кашу из двух языков.** `GroqTranscriber`
  в режиме translate ходил в `/audio/transcriptions` с `language="en"` и текстовым
  prompt "Translate...". Whisper промпт для перевода не использует, а `language="en"`
  указывал модели, что входное аудио уже английское — в результате часть сегментов
  выходила на русском, часть на английском. Теперь при `translate=True` используется
  отдельный endpoint `/openai/v1/audio/translations` (как уже было сделано для
  OpenAI API) — он всегда выдаёт английский и сам детектит исходный язык.
  Файл: `transcriber.py:305-316`.

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
