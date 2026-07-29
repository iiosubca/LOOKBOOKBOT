# LOOKBOOKBOT for Windows

## Version 0.2.25

- Visual model retries, visual-layout corrections, native four-look batches and caption repair now continue only while a signed plan or durable checkpoint changes. Fixed attempt ceilings no longer stop a valid long-running release.
- A repeated identical model/COM failure now stops with a precise diagnostic instead of consuming a counter or endlessly repeating the same InDesign action.

## Version 0.2.24

- Credit-proof notes now recognise concrete visual cues in Russian and English, including explicit labels such as `garment=...; bag=...`.
- If a controller rejects a single credit proof because its note is too generic, the app re-opens only that exact proof card, requests a structured observation, and retries the same safe batch without resetting confirmed looks.

## Version 0.2.21

- LOOKBOOKBOT no longer resolves, packages, or depends on a Codex Skill. It ships only its own versioned automation scripts and embeds the visual-verification policy for Codex, Google AI Studio, Ollama, and llama.cpp.
- Codex proof workers are explicitly read-only and prohibited from loading external skill instructions; the application controller remains the sole writer of project evidence and InDesign actions.

## Version 0.2.18

- The final-PDF indicator now starts with an explicit preparation state instead of a misleading static `0%`, then switches to durable `written / total` checkpoints as each file completes.

## Version 0.2.17

- Gender PDFs now assign their InDesign page range as a scalar COM value, fixing the invalid-cast failure after the three full-lookbook PDFs have been checkpointed.
- Final-export progress now advances file by file from durable native checkpoints and names the PDF currently being written instead of staying at 0% until all five exports finish.

## Version 0.2.16

- A recovery-started InDesign instance now retains its normal document-free start window instead of being hidden. The hidden start could later report an empty title and trigger a false safety block even though the app had launched the instance itself.

## Version 0.2.15

- Windows PowerShell recovery now receives the controlled master path inside an encoded script rather than as a parameter after `-Command`. That parameter was silently lost in the packaged app, causing a false safety refusal even with one neutral, document-free InDesign instance.

## Version 0.2.14

- COM-recovery no longer aborts its own 45-second neutral-start check after 30 seconds. It now permits a full controlled launch window and, only when no InDesign process remains, one additional cold launch before reporting a safety block.

## Version 0.2.13

- The read-only `release` audit now self-recovers from `RPC_E_DISCONNECTED`: when InDesign has one provably document-free neutral start window, LOOKBOOKBOT restarts only that automation instance and retries the same armed release audit up to twice. It never re-arms, skips the audit, or changes the saved master.
- If the neutral window is already visible, recovery no longer starts with a fragile COM enumeration. It uses the verified process state, then waits for the new InDesign instance to be ready before retrying.

## Version 0.2.12

- Caption-clearance now measures the actual largest connected foreground component. Scattered JPEG/studio-background texture no longer blocks a safe credits position, while a contiguous garment, bag, or body remains a hard visual block.

## Version 0.2.11

- Caption-clearance now calculates printed credit width from the complete PDF text matrix, not the raw `Tf` value. This prevents a real 8-pt credits column from being misread as a 15-pt stripe and eliminates repeated false collision retries.
- The visual correction loop no longer treats an unchanged, unresolved plan as productive work; it reports the exact controller limitation rather than repeating a full proof cycle.

## Version 0.2.10

- The PDF-text geometry guard now also detects the nested-form failure mode where many credit rows are collapsed to one narrow horizontal coordinate.

## Version 0.2.9

- An unstarted clearance retry now automatically rebinds its delta baseline to the current same-master native composition evidence, preventing a stale archived identity from blocking InDesign before the correction starts.

## Version 0.2.8

- If a nested InDesign PDF form reports every credit row at one baseline, the clearance audit recognises that impossible geometry and uses the real existing credits-frame area for safe correction planning.
- Visual rejection recovery can also continue when the failed batch has not yet written any individual confirmations.

## Version 0.2.7

- Caption-clearance now composes nested PDF text coordinates correctly, so all rendered credit rows participate in collision detection rather than only their first apparent line.
- A grounded visual-model rejection automatically archives the incomplete confirmation queue, asks the controller for a bounded correction of only the rejected LOOK IDs, regenerates proof evidence, and resumes the review-PDF flow.

## Version 0.2.6

- A partially confirmed visual-proof session now resumes its exact existing proof cards. LOOKBOOKBOT no longer re-arms InDesign, reconciles a clearance plan, or rerenders proofs after confirmations have started.

## Version 0.2.5

- Visual-proof workers now receive the actual attached proof images in read-only mode and return strict decisions instead of being asked to write controller records themselves.
- The coordinator records accepted visual confirmations serially, then automatically retries only missing or declined proof cards up to three times before continuing to the review-PDF export.

## Version 0.2.4

- A stopped visual stage now resumes its saved composition plan instead of attempting to create it again. Before an unstarted retry, the app can recover the prior signed credits-frame position from native delta evidence without opening InDesign.

## Version 0.2.3

- Parallel Codex workers now always launch without a visible Windows Terminal window; their output remains captured by LOOKBOOKBOT.

## Version 0.2.2

- A first composition-correction batch now starts from an empty checkpoint as intended; the durable delta file is required only after the native InDesign worker has completed a batch.
- Multi-batch correction passes validate only the corrections already saved in the current batch, then validate the complete set before the final transaction.

## Version 0.2.1

- Codex proof reviewers now receive the actual attached proof-card images in read-only mode; local image paths alone are no longer relied upon.

## Version 0.2

- The tested automation engine is included with the application, so a release does not silently depend on changed global Codex files.
- PDF-reference and credit proof cards are inspected in independent bounded batches; one coordinator records the immutable confirmations.
- A caption-clearance retry re-applies only changed looks while retaining a full native verification record for the complete lookbook.
- The five approved PDFs export from one InDesign session with a checkpoint for each file and parallel post-export verification.

LOOKBOOKBOT — настольная программа для управляемой сборки лукбуков TSUM в Adobe InDesign. Она не считает сохранённый INDD или появившийся PDF завершением этапа: состояние берётся только из evidence-контроллера `lookbook-layout`.

## Что уже работает

- выбор папки с PDF-референсом, Excel-каталогом и `hires`;
- ввод даты и автоматическое имя проекта `TSUM_FS-0YYMMDD`;
- выбор исполнителя: Codex, Google AI Studio, Ollama или OpenAI-совместимый сервер llama.cpp;
- 12 последовательных этапов с сохранением статуса в локальной SQLite-базе;
- продолжение с первой незавершённой строки или запуск с выбранного этапа;
- таблица PDF-порядка и пар изображений с предпросмотром и ручной заменой;
- таблица соответствий `LOOK_### ↔ лист/номер карточки Excel` с отдельной ручной правкой;
- все временные материалы только в `control/work`, корень выпуска остаётся чистым;
- прямое управление InDesign через официальный COM-контроллер, без движения мыши;
- пакетное продолжение этапов `images`, `captions` и composition;
- финальный выпуск после галочки «Согласовано»: 10mb, 20mb, 40mb и `Gender` M/W.

## Первый запуск

1. Дважды нажмите `setup.bat`. Он создаст локальное окружение программы и установит зависимости.
2. После сообщения об успешной установке запускайте `run.bat`.
3. Выберите папку `SOURCES` (или `_mat`) с одним PDF-референсом, одним `.xlsx` и одной папкой `hires`.
4. Укажите дату, выберите ИИ и нажмите «Создать / открыть проект».
5. Нажмите «ПУСК». Без выбранной строки программа продолжает с места остановки. Если выбрать этап, он и все следующие этапы будут пересобраны — это нужно для правки старого выпуска или повторного экспорта PDF.

## Ручные исправления

На вкладке «Список луков» можно заменить только имена двух фотографий. Порядок PDF и номера разворотов заблокированы от случайного редактирования. Кнопка перестановки меняет назначение исходников, а не контейнеры страниц.

На вкладке «Список кредитов» изменяются только лист Excel и номер карточки ошибочного лука. После сохранения строка сразу получает статус `CONFIRMED` как вручную подтверждённая оператором: перед вёрсткой программа всё равно создаёт для неё новую точную proof-card и проводит контролируемую проверку. Остальные подтверждённые строки не пересчитываются. Одна пара «лист Excel + № карточки» не может принадлежать двум лукам: такие строки подсвечиваются красным и не сохраняются.

Если визуальная проверка показала сомнительные `CONFIRMED` строки, поставьте галочки в первом столбце только у этих луков и нажмите «Повторно сопоставить отмеченные». Программа запускает отдельный короткий этап, который работает только с отмеченными `LOOK_###`, не запускает общий `seed` и останавливается до InDesign. Карты остальных луков сохраняются и дополнительно защищены от изменений во время этой операции. Если правильная Excel-карточка занята другим ошибочным луком, отметьте оба: обмен будет выполнен одной связанной группой не более пяти луков.

Чтобы вернуться к уже собранному выпуску, нажмите «Открыть готовый проект» и выберите папку выпуска, в которой есть `control`. Программа восстановит таблицы и галочки только из сохранённых controller-evidence; затем можно выбрать нужный этап или нажать «Продолжить с места остановки».

## Модели

- **Codex**: можно оставить поле модели пустым — будет использована настроенная по умолчанию модель приложения Codex.
- **Google AI Studio**: выберите `Gemini 3.5 Flash Lite`, вставьте API-ключ из AI Studio и нажмите «Проверить модель». Ключ сохраняется в Диспетчере учётных данных Windows, а не в проекте и не в базе выпуска. Рядом с выбором модели видны локальные счётчики RPM, TPM и RPD; по умолчанию они ограничены 15 запросами/мин, 250K токенов/мин и 500 запросами/сутки.
- **Ollama**: запустите Ollama, выберите мультимодальную модель и нажмите «Проверить модель». По умолчанию используется `http://127.0.0.1:11434`.
- **llama.cpp**: запустите OpenAI-совместимый сервер с vision-моделью на `http://127.0.0.1:8080`.

Адреса локальных серверов хранятся в настройках приложения. Секреты и токены в репозиторий не записываются.

## Данные и безопасность

База состояния находится в `%LOCALAPPDATA%\LOOKBOOKBOT\lookbookbot.db`. Она не лежит рядом с исходниками и не попадает в Git. В корне каждого выпуска остаются только INDD, готовые PDF, `Gender`, `control` и `control-history`.

Известные уведомления InDesign о ссылках подавляются контролируемым opener в режиме `NEVER_INTERACT`. Неизвестные диалоги, открытый пользовательский документ и долгий `.idlk` не подтверждаются автоматически.
