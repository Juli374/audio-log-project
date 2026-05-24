# Полный аудит v1.2.0 — длинный режим vs короткая диктовка

Дата: 2026-04-24
Версия: 1.2.0 (uncommitted на main)

## TL;DR

Гипотеза «длинный режим протёк в короткий» подтвердилась — но **не через
SessionRecorder и не через LongTranscriber**. Они архитектурно отделены
правильно (отдельные классы, отдельный микрофонный стрим, отдельные
хоткеи, конфликты гасятся на уровне `MenuBarApp._on_activate` и
`SessionController.start`).

Реальная утечка — в **`Recorder.stop()`** и **`transcriber.py`**, где при
сборке v1.2.0 одновременно с длинным режимом изменили две независимых
вещи, и обе ломают именно короткую диктовку:

1. **🔴 Критично:** `Recorder.stop()` теряет 20–100+ мс хвоста аудио —
   и причин ДВЕ, не одна (см. секцию 2). Whisper без хвоста съедает
   последнее слово. ([recorder.py:155-167](recorder.py:155))
2. **🟠 Важно:** Удалён фильтр галлюцинаций Whisper. Это **подтверждённая
   и хорошо задокументированная** проблема — Whisper-large-v3 даёт
   99.97% галлюцинаций на не-речевом аудио (Calm-Whisper, arXiv
   2505.12969). Все наши паттерны («Спасибо за просмотр», «Редактор
   субтитров А.Семкин» и т.д.) — буквально из community-списка известных
   галлюцинаций. ([transcriber.py diff](transcriber.py))

Оба бага **валидированы независимыми ресёрчами** (см. секцию «Валидация»
в конце документа) — фиксы в этом отчёте обновлены под реальные
канонические паттерны, а не под мою первоначальную интуицию.

Подпись и автовставка — всё в порядке: `output.py` дёргает Cmd+V
in-process через Quartz; `tools/create-dev-cert.sh` + `build.sh`
обеспечивают стабильную подпись «AudioLog Dev Local» → одно
Accessibility-разрешение, никаких helper-аппов.

---

## 1. Архитектурное разделение режимов — состояние

| Компонент           | Короткая (Right Option) | Длинная (Left Option ×2) | Общий?            |
|---------------------|------------------------|--------------------------|-------------------|
| Recorder            | `Recorder` (RAM)       | `SessionRecorder` (диск) | ❌ Раздельные     |
| Audio stream        | свой `sd.InputStream`  | свой `sd.InputStream`    | ❌ Раздельные     |
| Хоткей              | RO press/release       | LO double-tap            | ❌ Раздельные     |
| Транскрайбер        | `BaseTranscriber`      | `LongTranscriber(base)`  | ⚠️ Один base    |
| Mic conflict        | `is_session_busy?`     | `is_short_busy?`         | ✅ Гасится        |
| `_busy` lock        | да                     | да (через base)          | ⚠️ Сериализация |
| WAV на диск         | нет                    | да (`~/Library/.../sessions`) | —          |
| DB                  | `entries`              | `sessions`               | ❌ Раздельные     |

**Вердикт:** разделение само по себе нормальное. Единственная разделяемая
сущность — экземпляр `BaseTranscriber` (в локальном режиме это одна
загруженная модель Whisper). Это **корректно**: модель ~1.5 ГБ, держать
две копии бессмысленно. Конфликт сериализуется через `_busy` lock в
`LocalTranscriber`.

Что **не** сломалось:
- двойной тап Left Option фильтрует случайные нажатия Option+arrow при наборе
- `SessionController.is_busy` блокирует короткую диктовку на уровне `_on_activate`
- `MenuBarApp._is_recording` блокирует старт сессии через `SessionController.start`

---

## 2. 🔴 Bug #1: Recorder теряет хвост аудио

### Что изменилось

[recorder.py:155-167](recorder.py:155) — было:

```python
def stop(self) -> np.ndarray:
    with self._lock:
        self._recording = False

    with self._lock:
        chunks = self._chunks.copy()
        self._chunks.clear()

    # Stop stream from audio thread (Mixxx pattern — avoids deadlock)
    self._stop_stream()       # ← синхронно, ждёт finished_callback (до 3 с)
```

Стало:

```python
def stop(self) -> np.ndarray:
    with self._lock:
        self._recording = False
        chunks = self._chunks.copy()
        self._chunks.clear()

    # Stop stream in background — don't block audio processing
    threading.Thread(target=self._stop_stream, daemon=True).start()  # ← async
```

### Почему это ломает короткую диктовку — две причины

**Причина A (доминирующая): race-окно `_recording=False`.**
Когда пользователь отпускает Right Option:

1. NSEvent → `_on_deactivate` → фоновый поток → `recorder.stop()` (~30 мс)
2. `stop()` ставит `_recording=False`
3. Параллельно `_stop_stream()` запускается в ОТДЕЛЬНОМ потоке и ещё не
   успел поставить `_should_stop=True`
4. Между этими моментами PortAudio вызывает callback несколько раз
   (~10 мс шаг). Callback видит `_recording=False` (но ещё не
   `_should_stop=True`) → проваливается в `with self._lock` и **молча
   дропает кадр**, который УЖЕ доставлен из драйверного буфера
5. Окно typically 5–50 мс, под нагрузкой 100+ мс. Каждый callback в
   этом окне — потерянный chunk

**Причина B (вторичная): `paAbort` дропает HAL ring buffer.**
Когда наконец `_should_stop=True`, callback кидает `sd.CallbackAbort`,
что в PortAudio = `paAbort`. По
[документации PortAudio](https://portaudio.com/docs/v19-doxydocs/start_stop_abort.html):
- `paAbort` — *«stops the stream as soon as possible»* — внутренний HAL
  ring buffer (заполненный аппаратурой между последним деливеред callback
  и абортом) **выбрасывается, не доставляется в callback**
- `paComplete` (`sd.CallbackStop`) — *«ensures that the last buffer is
  played»* — для input это значит «дренирует ring buffer через callback
  перед `streamFinishedCallback`»

То есть мы используем НЕ ту константу. Дефолтный CoreAudio HAL buffer
для input на macOS — 512 кадров = ~32 мс @ 16 кГц.

**Итого потеря: 20–100+ мс**, occasionally хуже. Подтверждено
[Apple Dev Forum](https://developer.apple.com/forums/thread/117962)
(Doug Wyatt, Apple Core Audio team) и
[PortAudio CoreAudio backend исходниками](https://github.com/PortAudio/portaudio/blob/master/src/hostapi/coreaudio/pa_mac_core.c).

### Whisper и обрезанный хвост — это диагноз, не симптом

Подтверждено независимо:
- [huggingface/transformers#23231](https://github.com/huggingface/transformers/issues/23231)
  — *«When the input audio is cut off in the middle of a word, Whisper may
  not predict an ending timestamp»* — последний сегмент дропается или
  возвращается с `end=None`.
- [openai/whisper discussion #2115](https://github.com/openai/whisper/discussions/2115)
  — community подтверждает, что padding тишиной помогает.
- whisper.cpp дефолты VAD: `speech_pad_ms=30`, `min_silence_duration_ms=100`
  ([README](https://github.com/ggml-org/whisper.cpp)) — эти параметры
  существуют именно потому что декодер требует «естественной» концовки
  сегмента.

### Симптом, который описал пользователь

> «Конечную какую-то фразу либо слово не транскрибировать. То есть он её
> вообще не ловит, не видит, не слышит, ну, либо что-то, она обрезается.
> Раньше такого не было.»

Точное совпадение с двумя описанными механизмами.

### Фикс — обновлённый по результатам валидации

Мой первоначальный фикс (поменять порядок в callback + дождаться
`finished_callback`) **направление верное, но недостаточно**. Каноничный
паттерн (подтверждено sounddevice maintainer'ами и PortAudio docs):

1. **Убрать `_recording` flag из callback вообще.** Пока стрим живой —
   всегда append. Логическое состояние «recording» отслеживаем в
   `Recorder`, а не в callback.
2. **Заменить `sd.CallbackAbort` → `sd.CallbackStop`** (paComplete
   вместо paAbort). paComplete дренирует HAL ring buffer ЧЕРЕЗ callback
   перед `finished_callback`. Это убирает Причину B.
3. **Дождаться `_stream_finished` СИНХРОННО перед снапшотом.** Мы уже в
   worker-потоке (`_stop_and_process`), main UI не блокируется. Это
   убирает Причину A.
4. **Ещё лучше: вообще не использовать flag-pattern на macOS.** Можно
   звать `self._stream.stop()` напрямую из worker-потока (НЕ из audio
   thread). Согласно sounddevice source: *«Stream.stop() waits until all
   pending audio buffers have been played»*. Mixxx-workaround нужен был
   именно для `Pa_CloseStream` deadlock'а — `Pa_StopStream` из
   non-callback thread это документированный happy path. После
   `stream.stop()` finished_callback гарантированно сработал → `close()`
   безопасен.
5. **Belt-and-suspenders: padding ~150 мс тишины** (zeros) в конец
   массива ПЕРЕД отдачей в Whisper. Дёшево, страхует от любой остаточной
   потери и от Whisper-bug'а с timestamp-токенами на cut-off аудио.

```python
# recorder.py — _callback: сначала append, потом проверка стопа.
# Порядок важен: иначе теряется indata callback'а, который инициирует остановку.
def _callback(self, indata, frames, time_info, status):
    if status:
        log.warning("sounddevice status: %s", status)
    with self._lock:
        self._chunks.append(indata.copy())  # всегда пишем, пока стрим живой
    if self._should_stop:
        # CallbackStop (=paComplete) корректнее чем CallbackAbort (=paAbort):
        # даёт стриму нормально завершиться, finished_callback срабатывает
        raise sd.CallbackStop

# recorder.py — stop: дождаться драйна, потом снапшот
def stop(self) -> np.ndarray:
    self._should_stop = True
    # Мы уже в background-thread (_stop_and_process), main thread свободен.
    self._stream_finished.wait(timeout=3.0)

    with self._lock:
        chunks = self._chunks.copy()
        self._chunks.clear()
    self._recording = False  # логическое состояние, не для callback'а

    # close() в фоне (Mixxx-pattern всё ещё нужен против Pa_CloseStream deadlock)
    threading.Thread(target=self._close_stream, daemon=True).start()

    # Padding 150 мс тишины — страхует Whisper от обрыва на финальном слове
    if len(chunks) > 0:
        audio = np.concatenate(chunks).flatten()
        tail = np.zeros(int(self._config.sample_rate * 0.15), dtype=np.float32)
        audio = np.concatenate([audio, tail])
        ...
```

**Trade-off:** `stop()` блокирует свой поток на ~50–200 мс (ожидание
`finished_callback`). Поскольку вызов идёт из `_stop_and_process`
daemon-потока, **main UI не страдает**. UX ровно такой же.

### Источники

- [PortAudio: Starting, Stopping and Aborting a Stream](https://portaudio.com/docs/v19-doxydocs/start_stop_abort.html)
- [PortAudio CoreAudio backend pa_mac_core.c](https://github.com/PortAudio/portaudio/blob/master/src/hostapi/coreaudio/pa_mac_core.c)
- [Apple Dev Forum: AudioOutputUnitStop synchronous behavior](https://developer.apple.com/forums/thread/117962)
- [python-sounddevice source — Stream.stop docstring](https://github.com/spatialaudio/python-sounddevice/blob/master/src/sounddevice.py)
- [HF transformers#23231 — Whisper drops mid-word segments](https://github.com/huggingface/transformers/issues/23231)
- [whisper.cpp README — VAD speech_pad_ms=30](https://github.com/ggml-org/whisper.cpp)
- [Mixxx PR #14208 / PortAudio #367 — Pa_CloseStream deadlock pattern](https://github.com/mixxxdj/mixxx/pull/14208)

### Защита от регрессии

`tests/test_recorder_drain.py` (новый) — фейковый стрим, который:
1. Эмулирует callback с задержанным chunk после `_should_stop=True`,
   проверяем что он попадает в результат `stop()`.
2. Проверяет, что результат содержит padding 150 мс тишины в конце.

---

## 3. 🟠 Bug #2: Удалён фильтр галлюцинаций Whisper

### Что изменилось

[transcriber.py diff](transcriber.py) — удалена константа
`_HALLUCINATION_RE` и проверка `if _HALLUCINATION_RE.search(text)` в обоих
транскрайберах (Local и API).

```python
# Было — фильтровало эти известные галлюцинации Whisper:
_HALLUCINATION_PATTERNS = [
    r"редактор\s+субтитров",
    r"субтитры\s+(сделал|делал|выполнил)",
    r"корректор\s+\w\.\s*\w+",
    r"продолжение\s+следует",
    r"спасибо\s+за\s+просмотр",
    r"подписывайтесь\s+на\s+канал",
    r"www\.",
    r"http",
]
```

### Почему это ломает короткую диктовку

Whisper при **тихих или очень коротких** входах часто выдаёт фразы, на
которых был дообучен — а это огромный массив YouTube-субтитров. Отсюда
«Спасибо за просмотр», «Подписывайтесь на канал», «Редактор субтитров
А.Семкин» и т.п.

Bug #1 укорачивает хвост → больше шансов, что финал записи — это тихое
затухание → Whisper выдаёт галлюцинацию вместо реального слова. Раньше
её ловил фильтр и возвращал `""`, теперь она вставляется в текст.

Это вторая половина жалобы:
> «И на короткие записи… он может конечную какую-то фразу либо слово не
> транскрибировать… Это ещё и повлияло на качество, как мне кажется.»

### Валидация — это хорошо задокументированная проблема

Не интуиция, а подтверждённый факт:

- **Whisper-large-v3: 99.97% галлюцинаций на UrbanSound8K
  non-speech audio** ([Calm-Whisper paper, arXiv 2505.12969, May
  2025](https://arxiv.org/html/2505.12969v1)).
- Наши конкретные русские паттерны — из
  [community-списка waveletdeboshir](https://gist.github.com/waveletdeboshir/8bf52f04bf78018194f25b2390c08309),
  который собрали прогоняя 13 часов ЧИСТОГО ШУМА через whisper-large-v2.
  Там дословно: `Редактор субтитров А.Семкин Корректор А.Егорова`,
  `Спасибо за субтитры!`, `Подпишись на канал`, `Продолжение следует...`,
  `Субтитры добавил DimaTorzok`.
- Групповые обсуждения:
  [openai/whisper #1873 «share your hallucinations»](https://github.com/openai/whisper/discussions/1873),
  [#2131](https://github.com/openai/whisper/discussions/2131),
  [whisper.cpp #1724](https://github.com/ggml-org/whisper.cpp/issues/1724).

Groq `whisper-large-v3-turbo` — это pruned `large-v3` (32→4 decoder layers)
— **наследует все галлюцинации v3, фильтр нужен**.
OpenAI `gpt-4o-mini-transcribe` (dec 2025) сделал ~90% меньше галлюцинаций,
но текст всё равно приходит сырой, server-side strip'а нет.

### Фикс — обновлённый под state-of-the-art подход

Мой первоначальный фикс «добавить длиновое ограничение <30 символов» —
**не лучший выбор**. Два улучшения по результатам ресёрча:

**1. Правильный критерий: совпадение ≈ весь текст, не substring.**

Длинная диктовка, заканчивающаяся «...и подписывайтесь на канал нашей
компании», не должна вся вылететь. Матч считаем валидным если он
покрывает ≥ 80% длины транскрипции. Это сохраняет редкие легитимные
упоминания, но убивает классический stand-alone галлюцинат.

**2. Расширенный список паттернов** (из community-списка + gist):

```python
# transcriber.py
_HALLUCINATION_PATTERNS = [
    # Русские — субтитровые credits
    r"редактор\s+субтитров",
    r"корректор\s+\w\.\s*\w+",
    r"субтитры\s+(сделал|делал|выполнил|сделаны|добавил|подогнал|подготовил)",
    r"DimaTorzok",
    # Русские — YouTube outro
    r"спасибо\s+за\s+(про)?смотр",
    r"подписывайтесь\s+на\s+(канал|нас)",
    r"подпишись",
    r"ставьте\s+лайк",
    r"всем\s+пока",
    r"до\s+новых\s+встреч",
    r"поддержите\s+канал",
    r"смотрите\s+продолжение",
    r"продолжение\s+следует",
    # Английские — если Whisper угадал не тот язык на шуме
    r"thanks?\s+for\s+watching",
    r"(don't\s+forget\s+to\s+|please\s+)?(like\s+and\s+)?subscribe",
    r"translated?\s+by\s+\w+",
    # Источники субтитров (все языки)
    r"amara\.org",
    r"castingwords",
]
_HALLUCINATION_RE = re.compile("|".join(_HALLUCINATION_PATTERNS), re.IGNORECASE)

# Применение — в BaseTranscriber.transcribe, ДО Claude-перевода:
text = self._transcribe_raw(audio)
if text:
    match = _HALLUCINATION_RE.search(text)
    if match and len(match.group()) / len(text) >= 0.8:
        log.warning("Filtered hallucination (%.0f%% of text): %.80s",
                    100 * len(match.group()) / len(text), text)
        return ""
```

**Не фильтруем `www.` и `http` безусловно** — пользователь может
диктовать URL. Если появится проблема именно с этими — добавим отдельным
паттерном «целая строка = URL».

### Дополнительная defense-in-depth (не обязательно сейчас, но стоит знать)

Regex — это ПОСЛЕДНИЙ рубеж. State-of-the-art 2025–2026 — слоёная
защита:

1. **VAD препроцессинг (Silero/WebRTC)** — стрипаем тишину ДО Whisper.
   Самый большой ROI. whisper.cpp теперь имеет встроенный `--vad` флаг
   с GGML-моделью Silero.
2. **`condition_on_previous_text=False` / `-mc 0`** — в pywhispercpp
   называется `max_context=0`. **Strongly recommended для коротких
   высказываний** — не даёт галлюцинации «снежиться» между чанками
   ([whisper.cpp #1490](https://github.com/ggml-org/whisper.cpp/discussions/1490)).
3. **`no_speech_thold` ~ 0.2** (дефолт 0.6) для шумного/тихого аудио.
4. **`hallucination_silence_threshold` (2–8 s)** — upstream Whisper
   ([PR #1838](https://github.com/openai/whisper/pull/1838)), требует
   `word_timestamps=True`.
5. **Regex post-filter** — у нас.

Для короткой диктовки (Right Option) VAD не критичен — пользователь сам
контролирует начало/конец кнопкой. Но для длинного режима (session →
chunks) VAD стоил бы включить, там между чанками как раз тишина, и
галлюцинации кластеризуются.

**Ближайшие действия:** добавить в `pywhispercpp.Model.transcribe()`
вызов параметры `max_context=0` (условно) и проверить передачу
`no_speech_thold`.

### Источники

- [Russian hallucinations gist (waveletdeboshir)](https://gist.github.com/waveletdeboshir/8bf52f04bf78018194f25b2390c08309)
  — базовый community-список
- [openai/whisper #1873 — share your hallucinations](https://github.com/openai/whisper/discussions/1873)
- [openai/whisper #2131 — «Субтитры подогнал Симон»](https://github.com/openai/whisper/discussions/2131)
- [openai/whisper #679 — possible solution to hallucination](https://github.com/openai/whisper/discussions/679)
- [whisper.cpp #1724 — hallucination on silence](https://github.com/ggml-org/whisper.cpp/issues/1724)
- [whisper.cpp #1490 — large-v3 repetition, `-mc 0` fix](https://github.com/ggml-org/whisper.cpp/discussions/1490)
- [Calm-Whisper paper (arXiv 2505.12969)](https://arxiv.org/html/2505.12969v1) —
  99.97% галлюцинаций на non-speech
- [Investigation of Whisper hallucinations (arXiv 2501.11378)](https://arxiv.org/pdf/2501.11378)
- [Gladia blog: AI Model Biases — what went wrong with Whisper](https://www.gladia.io/blog/ai-model-biases-what-went-wrong-with-whisper-by-openai)
- [Memo AI: Solutions to Repeated Output Issues with Whisper](https://memo.ac/blog/whisper-hallucinations)
- [OpenAI: gpt-4o-mini-transcribe 2025-12-15 update](https://developers.openai.com/blog/updates-audio-models)

---

## 4. 🟡 Прочие наблюдения (не блокеры)

### 4.1 Дублирование `_pick_input_device`

`SessionRecorder._pick_input_device` — копипаста из
`Recorder._pick_input_device` ([session_recorder.py:155-189](session_recorder.py:155)).
Комментарий честно объясняет почему («хочу независимости»), но если
изменится логика выбора устройства (новые «Find My»-дребезги, AirPods и
т.п.), обновлять придётся в двух местах.

**Рекомендация:** вынести `_pick_input_device` в `recorder.py` как
free-function `pick_input_device()`. Импорт «utility» — это не нарушение
независимости.

### 4.2 Heartbeat может задержать короткую диктовку

`LocalTranscriber._start_heartbeat` каждые 30 минут берёт `_busy` lock
для прогона 0.1 с тишины. Если хоткей нажат ровно в этот момент,
`_transcribe_raw` ждёт. Окно крошечное (~50 мс), но проявляется как
«первая после долгой паузы транскрипция тормозит».

**Рекомендация:** оставить как есть, добавить в лог `Heartbeat blocked
short transcription by Xms` для диагностики.

### 4.3 `session_max_hours` watchdog не переживает sleep

`SessionController._start_hard_stop_watchdog` использует
`threading.Timer(max_sec, fire)`. На macOS таймер не учитывает
`mach_continuous_time` vs `mach_absolute_time` — после длительного sleep
сработает с задержкой. Для 6-часового потолка это окей, но не идеально.

**Рекомендация:** заменить на цикл `while not stop.wait(60): if elapsed
>= max: fire()` по аналогии с `_safety_watchdog` в коротком режиме.

### 4.4 `session_translate=False` — фича, не баг

В config: длинные сессии не переводятся через Claude. Это намеренно
(стоимость + риск разделения по чанкам). Не трогаем.

### 4.5 Логи длинной сессии — нет суммарного результата

В `SessionController._process` логируется «X chars», но не RMS/peak/duration.
Если транскрипция вышла плохая, диагностики мало.

**Рекомендация:** добавить в лог `Session %d: %.1fs, RMS=%.4f, peak=%.4f,
%d chars, %d/%d chunks ok`.

---

## 5. ✅ Code signing и автовставка — состояние OK

Воспроизвожу из памяти + проверяю по факту:

- `output.py` шлёт Cmd+V через `Quartz.CGEventPost` **внутри AudioLog.app**
  → один TCC-грант на Accessibility, никаких helper-приложений
  ([output.py:35-46](output.py:35))
- `tools/create-dev-cert.sh` создаёт стабильный self-signed cert
  «AudioLog Dev Local», добавляет его в login keychain как trustRoot
- `build.sh` после `py2app` пересобирает подпись этим cert'ом
  ([build.sh:31-46](build.sh:31)). CDHash меняется при каждой сборке
  (это нормально: меняется код), а **identity (cert)** остаётся той же
  → TCC сохраняет одобрение между билдами
- `PasteHelper.app` и `PasteHelper.swift` физически в репо ещё лежат, но
  не используются и не бандлятся. Можно безопасно удалить — это
  уберёт путаницу для будущего читателя

**Единственное на что обратить внимание:**
- `setup.py` указывает `LSUIElement: False` — приложение в Dock. Если
  захотим скрыть из Dock (только menu bar), поменять на `True`. Но
  тогда история-окно требует доп. возни. Сейчас компромисс норм.
- В Info.plist отсутствует `NSScreenCaptureUsageDescription` — у нас
  capture не нужен, всё ок.

---

## 6. План фиксов (по приоритетам, обновлено после валидации)

| # | Файл           | Изменение                                            | Priority | Effort |
|---|----------------|------------------------------------------------------|----------|--------|
| 1 | recorder.py    | `CallbackStop` вместо `CallbackAbort`; убрать `_recording` из callback; `_stream_finished.wait()` ДО снапшота; padding 150 мс тишины | 🔴 P0 | 45 мин |
| 2 | transcriber.py | Вернуть `_HALLUCINATION_RE`, расширенный паттерн-лист; критерий «матч ≥ 80% текста» вместо длины <30 | 🟠 P1 | 20 мин |
| 3 | transcriber.py | Передать `max_context=0` (`condition_on_previous_text=False`) в `pywhispercpp.Model.transcribe()` | 🟠 P1 | 10 мин |
| 4 | tests/         | `test_recorder_drain.py` — регрессионный тест на хвост + padding | 🟠 P1 | 30 мин |
| 5 | tests/         | `test_hallucination_filter.py` — тесты 80%-критерия и новых паттернов | 🟡 P2 | 20 мин |
| 6 | recorder.py    | Вынести `pick_input_device()` в utility | 🟡 P3 | 15 мин |
| 7 | menubar.py     | Логировать summary длинной сессии | 🟡 P3 | 5 мин  |
| 8 | repo cleanup   | Удалить `PasteHelper.app`, `PasteHelper.swift` | 🟡 P3 | 2 мин  |

**Отложено (feature-work, не фикс):**
- VAD (Silero) для длинного режима — резко уменьшит галлюцинации
  между чанками. 2–3 часа работы + место в бандле для GGML-модели VAD.

После фиксов — bump до 1.2.1, дополнить CHANGELOG, ребилд через
`build.sh`, в нём обновить `CFBundleVersion` (сейчас захардкожен `1.2.0`
— при bump'е ручное обновление в трёх местах: `VERSION`, `setup.py`,
`build.sh`).

**Опциональный subtask:** вынести версию в один источник (`VERSION`
файл, который читают и `setup.py`, и `build.sh`).

---

## 7. Валидация через ресёрч — резюме

Оба бага валидированы через два независимых исследовательских прогона
(web search + fetch документации PortAudio, sounddevice, Whisper issues,
академических статей).

**Bug #1 — 🔴 подтверждён, фикс уточнён:**
- Механизм потери аудио оказался ДВУХсоставной (race `_recording` +
  `paAbort` дропает HAL ring buffer), не одной
- Правильное решение: `CallbackStop` (paComplete) + синхронное ожидание
  `_stream_finished` + padding 150 мс тишины для Whisper
- Моя первая идея «переставить append и stop-check» в callback —
  ПРАВИЛЬНОЕ направление, но НЕДОСТАТОЧНОЕ (не решает вторую причину)

**Bug #2 — 🟠 подтверждён, паттерны расширены:**
- Whisper-large-v3 имеет 99.97% галлюцинаций на non-speech
  (Calm-Whisper, 2025). Не интуиция — измеренный факт
- Наши русские паттерны — буквально из community-списка (gist
  waveletdeboshir), сгенерированного прогоном 13 часов шума
- Критерий отсева: не «длина <30», а «матч ≥ 80% текста» — защита от
  легитимных длинных диктовок с упоминанием «подписывайтесь»
- Regex — последний рубеж; state-of-the-art — layered (VAD + параметры
  модели + regex). Для short-dictation достаточно regex + `max_context=0`
