# LOOKBOOKBOT for Windows

## Version 0.2.86

Оптимизация без ослабления подтверждений: цепочка PDF-порядка и исходных фотографий проверяется один раз за запрос состояния/проверку карты, а не шесть раз. Между командами результаты проверки источников не кешируются: новые команды заново читают действительные SHA-256 фотографий и доказательств. На текущем 50-луковом наборе неизменный `status` ускорился с 54,5 до 7,8 секунды.

Численный подбор Excel-карточек сохраняется отдельно от визуальных подтверждений и повторно используется только при тех же байтах всех фотографий, Excel-изображений, PDF-fallback, реестре, порядке каталога, политике и версии алгоритма. Он не ставит CONFIRMED и не отменяет независимую проверку ИИ. Изменение исходника или повреждение сохранённого расчёта приводит к новому подбору.

Нативная запись кредитов сохраняет порции по четыре лука. Уже проверенные блоки пропускают дорогую повторную проверку типографики только при совпадении полного SHA-256 сохранённого INDD, реестра, кредитных данных, session/nonce и состава подтверждённых LOOK. Старые точки, ручные изменения и несохранённое состояние получают полную проверку. Новая порция проверяется до и после сохранения, весь документ повторно проверяется перед PASS. Итоговая проверка использует один индекс объектов вместо повторного полного обхода для каждого лука; ожидания стилей читаются один раз на тип поля внутри блока, действительные параметры каждого абзаца проверяются как раньше.

Предел нативной операции теперь 15 минут вместо трёх; это не плановая длительность и не разрешение параллельной записи. На тайм-ауте сохраняется запрет на опасный перезапуск. `control/progress/native-activity.json` и `native-timing.jsonl` фиксируют текущее действие/лук, а журнал приложения показывает длительность каждой нативной операции. 299 автоматических тестов прошли, включая реальные функции PowerShell на 1/5/50-луковых моделях; отдельные живые проверки выполняются только на копиях, без запросов ИИ. Полный новый выпуск от исходников до PDF не подменяется этими проверками.

Живые caption-интеграционные проверки в InDesign завершены на отдельных 1- и 5-луковых копиях подготовленного шаблона. Копия настоящего 50-лукового INDD возобновилась с 28 сохранённых луков и дошла до `COM_GATE_PASS captions 50/50` за 385 секунд, включая полный аудит старой точки и итоговую проверку всех 50 блоков. Последние обычные порции заняли 14–30 секунд. Исходный INDD и его контрольные файлы не изменились. Результаты: `tmp/native-speed-0.2.86-001-template/benchmark.json`, `tmp/native-speed-0.2.86-005-template/benchmark.json`, `tmp/native-speed-0.2.86-050/benchmark.json`. Численный подбор на отдельной копии даёт одинаковые холодный/повторный результаты; повторный расчёт занял 13 секунд (`tmp/credit-speed-0.2.86/benchmark.json`). Запросов ИИ эти проверки не используют; итоговую длительность новой полной сборки они не обещают.

## Version 0.2.85

Неуверенное первичное PDF → hi-res сопоставление больше не приравнивается к отсутствию фотографии. Перед продолжением сборки бот показывает выбранной модели читаемые страницы неиспользованных hi-res, ищет точные левый/правый кадры и независимо проверяет найденную пару. Одежда на другом человеке или другой дубль не являются заменой фотографии; различие обработки и кадрирования допустимо. Подтверждённые результаты сохраняются с хешами PDF, фотографий и точной proof-карточки. Повторная численная проверка не сбрасывает эти назначения.

Разрешена независимая установка одного найденного кадра при отсутствии второго. Одно фото не может занять два места, ручные назначения защищены, уже инициализированный INDD не пересопоставляется автоматически. Подтверждённые независимые пары сохраняются даже при исчерпании лимита в другой проверке. Если после поиска остаются пустые места, быстрый/фото-режим завершает INDD с явным предупреждением и статусом «ФОТО НЕ ПОДТВЕРЖДЕНЫ»; полный режим требует подтверждённых фотографий до работы с InDesign.

После возврата native-операции бот проверяет оставшуюся блокировку. Закрывается только точный сохранённый скрытый master, при отсутствии живого native-работника, стабильном файле и `Modified=false`. `Saved=true` само по себе больше не считается доказательством отсутствия несохранённых изменений. Пользовательские документы, активные работники и неизвестные блокировки не закрываются/не удаляются; сообщение объясняет сохранение, закрытие и продолжение. Прогресс записи кредитов не сбрасывается.

Проверка на копиях исходников: численный построитель прошёл наборы из 1/5/50 луков без изменения исходных фотографий. На `gpt-6-luna`, `high`, в пяти-луковом наборе LOOK_003 восстановлен за два запроса; на полном наборе LOOK_003/029/047 восстановлены за шесть запросов, с отдельной проверкой трёх точных пар и реальной записью контроллером. В реестре 100 реальных фотографий, ноль заглушек; повторный численный запуск подтвердил сохранённый реестр без новых запросов ИИ. Результаты: `tmp/photo-recovery-0.2.85-live-005/result.json`, `tmp/photo-recovery-0.2.85-live-050/result.json`. Готовый рабочий INDD не изменялся; полный native-выпуск в этой проверке не запускался.

## Version 0.2.84

Сопоставление кредитов ищет комплект одежды, а не одного и того же человека: смена модели, очков, позы, положения сумки и складки запахнутого пальто не являются самостоятельными причинами отказа. Подтверждение по-прежнему требует конкретных совпадений основной одежды и нижней части комплекта; другая одежда, фактура, цвет и слои отклоняются. Реальные замены обуви и аксессуаров записываются отдельно в примечание «Проверить товары», без выдумывания новых кредитов. Проверка точного кадра PDF → hi-res не ослаблена.

Перед исключением отклонённой карточки выполняется одна независимая перепроверка тех же изображений. Её ответ также кэшируется; лимит не вызывает повторов или принудительного подтверждения. Старые отказы по прежним правилам архивируются только для незавершённых целей; готовые назначения не сбрасываются. Все провайдеры получают одинаковые правила кредитного каталога. Журнал отличает сохранённый ответ NONE от найденного кандидата.

Предупреждение о товарах показывается как `CONFIRMED*` с пояснением при наведении: комплект найден, сборка не блокируется, но обувь/аксессуары требуют проверки. Предупреждение связано с назначением Excel и хешем текущей proof-карточки; устаревшее предупреждение не переносится на другой лук, ручные примечания не изменяются.

Ограниченная живая проверка на `gpt-6-luna`, `high`: четыре правильные карточки текущего набора подтверждены за четыре запроса; заведомо неверный комплект отклонён обеими проверками за два запроса. Исходные карточки и INDD не изменены. Результаты: `tmp/credit-identity-0.2.84-live/result.json` и `tmp/credit-identity-0.2.84-negative/result.json`. Это проверка конкретных случаев, не утверждение о безошибочности полного выпуска.

Прошли 258 автоматических тестов. Настоящие команды контроллера на изолированном наборе сохранили и подтвердили три замены при четвёртом намеренно нерешённом луке (`tmp/independent-credit-save-0.2.84`); ответы в этой проверке воспроизводятся, дополнительных запросов ИИ нет. Полный native-выпуск InDesign не запускался.

## Version 0.2.83

Новые проекты используют первичный подбор, учитывающий распределение цвета одежды наряду с попиксельным сходством: различие позы и кадрирования больше не является главным сигналом. Повторный поиск показывает полный каталог в порядке сходства с обоими кадрами лука, а не в порядке листов M/W. Фон оценивается по самому изображению; ранжирование не исключает карточки и не подтверждает совпадение. На странице поиска теперь четыре крупных карточки вместо восьми. Политика первичного подбора фиксируется в проекте; старый seed оставлен воспроизводимым для существующих карт, поэтому обновление не обнуляет их подтверждения.

Незавершённый поиск одного лука больше не теряет результаты остальных: успешно выбранные кандидаты проходят точную трёхпанельную проверку, независимые назначения и замкнутые обмены сохраняются отдельно. Незамкнутые цепочки и двойные претензии не записываются. При исчерпании лимита сохраняются уже доказанные альтернативы без новых запросов, но окончательное CONFIRMED ставится только после сверки обычной карточки. В интерфейсе неподтверждённый вариант явно обозначен «ПОДБИРАЕТСЯ».

Проверки: 239 автоматических тестов; на новом наборе из 57 карточек визуально сверенные LOOK_005/W:33, LOOK_014/W:6 и LOOK_043/M:6 находятся на местах 1/2/1 поискового каталога. `tools/evaluate_independent_credit_save.py` на изолированной копии проверяет реальные команды построения доказательств, выбора и подтверждения: три замены сохраняются при четвёртом нерешённом луке. Ответы ИИ в этом тесте воспроизводятся из проверенных тестовых входов, живые запросы не выполняются; это проверка контроллера, не заявление о безошибочной живой сборке. Рабочий проект и его INDD не меняются.

## Version 0.2.82

Перед сопоставлением PDF → hi-res объединяются точные копии JPG/JPEG (размер и SHA-256 полного файла). Одинаковые файлы больше не конкурируют друг с другом при расчёте уверенности одиночного кадра и пары. Имена используются только для стабильного выбора представителя уже доказанной группы: предпочтение существующей привязке, затем имени без суффикса copy/копия. Разные ретуши и кадры не объединяются. Все исходные файлы сохраняются без удаления, переименования и изменения; при возобновлении существующее имя из реестра сохраняется.

Исправление общее для обычного, быстрого режима и режима только фотографий. В журнале и manifest.json теперь видны число файлов, число уникальных фотографий и группы точных копий. Уникальные кандидаты проверяются прежними порогами; реально сомнительные пары не объявляются подтверждёнными только из-за удаления дубликатов из конкуренции.

`tools/evaluate_registry_duplicates.py` запускает настоящий построитель реестра на изолированных наборах из 1, 5 и всех PDF-луков. Подготовка коротких PDF использует штатную функцию создания тестового набора, фотографии в тестовых папках доступны через hardlink (с копированием при невозможности создать ссылку). Скрипт проверяет сохранность исходных байтов, уникальность выбранных кадров и отсутствие двойного использования одинаковой фотографии. Он не обращается к ИИ, не открывает InDesign и не восстанавливает пользовательские проекты.

Проверка 04.10.2026 на реальных `SOURCES/_mat`: 212 файлов → 106 уникальных кадров и 106 точных копий. Для 1/5/52 луков выбраны 2/10/104 фотографии соответственно, без заглушек. Исходные фотографии проверены по SHA-256 до и после теста. Результат: `tmp/registry-duplicates-0.2.82/result.json`. Прошли 222 автоматических теста, включая все три режима построителя и сохранение существующего имени при возобновлении. Проверены состав EXE и запуск Qt. Полный native-цикл InDesign в этой проверке не запускался: исправлен и проверен этап построения фото-реестра.

## Version 0.2.81

Сопоставление кредитов отличает идентичность образа от позы и видимости аксессуаров. Сложенная мягкая сумка с закрытыми ручками не объявляется другим клатчем только по способу её держать; снятые очки сами по себе не означают другую одежду. Требуются три конкретных совпадения, включая отличительную основную одежду и ещё один признак одежды. Реально другая модель, крой, цвет, слои одежды или конструкция сумки/обуви остаются причинами отказа. Это правило применяется только к Excel-кредитам, не к проверке точного кадра PDF/hi-res.

Новая версия поиска повторно рассматривает старые отказы от кандидатов один раз, сохраняет журнал для аудита и не трогает уже подтверждённые луки. Исправлено ошибочное чтение фразы о различии аксессуара как отказа от всего совпадения. Контроллер по-прежнему отклоняет любой ответ с настоящими contradictions.

`tools/evaluate_catalogue_recovery.py --case accessories --live --effort high` проверяет реальные пары LOOK_048/W:10 и LOOK_050/M:1 на изолированной копии, начиная с неверных W:31/M:13 и исключённых правильных карточек. Сначала проверяются отрицательные примеры, затем полный поиск, трёхпанельная сверка, атомарная запись карты и обычные подтверждения. Исходный проект и INDD не изменяются.

Проверка 04.10.2026 на `gpt-6-luna`, `high`: обе неверные карточки отклонены, правильные назначения восстановлены и подтверждены настоящим контроллером. Результат: `tmp/accessory-recovery-0.2.81-live/result.json`, 9 визуальных запросов. Прошли 211 автоматических тестов; состав EXE проверен против исходников, запуск Qt проверен в отдельной тестовой папке. Это проверка описанного сбоя, не новый полный прогон всех 52 луков в InDesign.

## Version 0.2.80

Спорные кредиты ищутся по полному Excel-каталогу, включая уже занятые карточки. На читаемом листе показываются восемь карточек, запрос содержит пару фото и не более двух листов; отсутствие совпадения на листе продолжает поиск, а не заставляет модель выбирать похожую одежду. Точное трёхпанельное подтверждение может открыть только связанного владельца карточки для атомарного обмена; несвязанные строки и ручные назначения защищены. Два положительных претендента сравниваются вместе, а не по номеру LOOK. Старые журналы принудительных выборов сохраняются для аудита, но не запрещают повторный поиск. Видимая внешность модели — разрешённый признак; лист W/M и порядок не являются доказательством.

`tools/evaluate_catalogue_recovery.py` воспроизводит конфликт LOOK_003/W:22 и LOOK_005/W:20 на изолированном двухлуковом тесте с полным каталогом. Режим `--live` использует выбранную модель, настоящий контроллер, альтернативные proof cards, атомарный обмен и обычные подтверждения. INDD и исходный проект не изменяются. Это проверка конкретного конфликта, не доказательство качества всей сборки из 52 луков.

Проверка 04.10.2026 на `gpt-6-luna`, `low`: конфликт исправлен автоматически, обе строки подтверждены контроллером; результат сохранён в `tmp/catalogue-recovery-0.2.80-live-v4/result.json`. Общие проверки включают закрытый NONE-ответ, крупный каталог, защиту ручных назначений, возобновление по старому журналу и связанное переоткрытие подтверждённой строки.

## Version 0.2.77

Исправлено возобновление кредитов: quick-autonomous больше не перезаписывает существующие CONFIRMED/PENDING строки и законные альтернативные назначения. Каждый успешно завершённый визуальный ответ и выбор кандидата сохраняется отдельно, с привязкой к точным изображениям, инструкции, модели и reasoning effort. При возобновлении можно восстановить и подходящие завершённые ответы из ai-exchanges версии 0.2.76. Свежая ручная отметка неверного лука инвалидирует его предыдущий ответ.

UsageLimitExceeded обрабатывается отдельно от ошибок формата: повторные запросы и эскалация не запускаются, ещё не начавшиеся параллельные проверки отменяются, уже завершённые ответы сохраняются. Даже при частичном сбое успешные первичные проверки записываются в карту до остановки, а интерфейс получает текущие статусы. Сохраняется история попыток точечного пересопоставления. В журнале показано число сохранённых подтверждений и оставшихся карточек. Модель, выбранная пользователем, не меняется.

Повреждённая или неполная промежуточная карта не заменяет список в интерфейсе и не скрывает исходную причину остановки. Если лимит закончился во время уточнения визуальных признаков, остальные пригодные подтверждения сохраняются без дополнительных запросов. Проверки возобновления выполняются с имитацией ответов провайдера; качество полного цикла на GPT-6 Luna отдельно не подтверждалось.

## Version 0.2.76

Сверка через Codex теперь использует App Server: реальные изображения передаются отдельными multimodal input items, модель и уровень размышлений задаются явно, итог принимается только после успешного завершения хода. Один лук проверяется в отдельном контексте; одно соединение обслуживает параллельные запросы. Структурированный ответ содержит независимые описания Excel/PDF и противоречия. Журнал запросов, хешей изображений, ответов, времени и доступной статистики токенов записывается в `control/work/ai-exchanges`.

Кадры PDF для сопоставления берутся из отрисованной страницы, чтобы учитывать цвета, clipping и PDF Decode arrays. В карточках с отсутствующими hi-res показываются реальные кадры PDF, а не белые заглушки; контроллер проверяет и их хеши. Сбой ИИ больше не заменяет проверку автоматическим `CONFIRMED`: выполняется ограниченный повтор только незавершённых проверок. Команды quick-seed/fallback также не подтверждают предложения по сходству.

`tools/evaluate_codex_matching.py` проверяет известные правильные и ошибочные совпадения на копиях изображений без открытия или изменения INDD. `--live` выполняет шесть сравнений, `--choices-only` проверяет выбор среди шести кандидатных карточек. Это ограниченная проверка качества сопоставления, а не подтверждение готовности всего лукбука.

Для визуальных запросов отключены лишние инструменты Codex. Релиз собирается с `--clean`; `tools/verify_frozen_release.py <exe>` сравнивает все упакованные модули приложения и файлы движка с текущими исходниками и отклоняет устаревший EXE. Кэш Python из тестов не включается в ресурсы движка.

## Version 0.2.75

В быстрой сборке все режимы повторного сопоставления кредитов автоматически используют сохранённые кадры PDF-референса для луков с пустыми фото-заглушками. Отсутствие отдельного параметра в промежуточном вызове больше не останавливает проверку на первом таком луке; повторять построение списка луков не требуется.

## Version 0.2.74

При открытии LOOKBOOKBOT список моделей Codex обновляется через официальный `model/list` установленного Codex. Меню показывает модели с поддержкой изображений, доступные текущему аккаунту, а отдельное поле «Размышления» — уровни, которые Codex объявляет для выбранной модели. Проверка идёт в фоне; кнопку «Обновить модели» можно использовать повторно. При недоступном каталоге программа явно помечает последний сохранённый список как непроверенный.

Выбранные модель и уровень сохраняются для проекта и применяются к следующему запуску через Codex CLI. Для старых проектов сохранён прежний выбор Terra по умолчанию; только при выборе Terra спорные карточки по старому правилу перепроверяются на 5.6 Sol. Явно выбранные GPT-6 Sol, Luna и другие модели не подменяются. До завершения обновления каталога новый Codex запуск не начинается.

## Version 0.2.73

После повторного сопоставления карта кредитов теперь сопоставляется с сохранённым доказательством до запуска продолжения. Если изменился текст `caption-data.tsv`, контроллер архивирует только устаревшие подтверждения карты, кредитов и следующих проверок, затем действительно записывает новые кредиты в INDD. Подтверждения структуры разворотов, даты, фреймов и фотографий сохраняются. Ложное завершение всех этапов за несколько секунд без изменения INDD устранено.

## Version 0.2.72

Старое сохранённое значение `gpt-6-astra` в интерфейсе автоматически заменяется на Terra, поэтому отображаемая модель и фактическая модель запуска теперь совпадают.

## Version 0.2.71

Модели Codex разделены по роли: обычная сборка, включая все стандартные пакеты визуальных доказательств, выполняется на `gpt-5.6-terra`. `gpt-5.6-sol` вызывается только для ограниченного набора проблемных карточек: спорных credit-перепривязок, неответивших пакетов доказательств, недостаточно конкретных подписей и повторной проверки отклонённых PDF/visual proof. Старые проекты с пустой моделью или сохранённой `gpt-6-astra` также автоматически продолжаются на Terra.

## Version 0.2.70

Исправлен выбор Codex CLI: LOOKBOOKBOT теперь предпочитает актуальный CLI запущенного приложения Codex либо системный CLI и обращается к старой sandbox-копии только как к запасному варианту. Это устраняет ложные ошибки повторного сопоставления, когда выбранная в приложении модель новее старого CLI и он завершал запрос до ответа модели.

## Version 0.2.69

Точечное повторное сопоставление кредитов с Codex теперь получает финальный ответ через надёжный отдельный канал CLI и использует строгую JSON-схему с допустимыми метками только из текущей closed candidate board. Если параллельный worker временно не вернул ответ, бот самостоятельно повторяет только затронутые луки последовательно в новых одноразовых сессиях, не меняя остальные карточки и не записывая неподтверждённый выбор.

## Version 0.2.68

Добавлен провайдер **OpenRouter** с моделью `google/gemini-3.8-flash`. Ключ хранится в Windows Credential Manager, а запросы используют официальный OpenRouter Chat Completions API с поддержкой текстовых и визуальных проверок.

## Version 0.2.67

Добавлен режим **«Только фотографии — INDD»**. Он фиксирует порядок PDF-референса и пары фотографий, выполняет структуру, дату, привязку фреймов и расстановку фотографий в локальном INDD, после чего останавливается. Excel-сопоставление, кредиты, визуальная проверка, PDF на проверку и финальные PDF в этом режиме не запускаются; пропущенные этапы отображаются в интерфейсе как намеренно пропущенные.

## Version 0.2.66

Excel credit-card matching now scores the complete PDF look pair: the representative Excel image is compared with both ordered PDF photos, while the combined pair appearance is used as a conservative tie-breaker in the global one-to-one assignment. Strong direct matches remain stable, and visual proof/confirmation is still required before credits are written.

## Version 0.2.65

Reference-photo matching now uses foreground appearance evidence and evaluates the left/right pair jointly. A close-up can anchor a visually similar full-length image without the quick build incorrectly blanking the entire look; one-to-one assignment and the PDF-authoritative order remain unchanged.

## Version 0.2.64

The project-root `_MAT/hires` folder is the authoritative source of photographs for matching, UI previews and InDesign placement. New projects copy hires there only, not into `control/work/_mat/hires`. Retouched photographs may be replaced there manually and are never overwritten on resume. Legacy migration copies only missing files once, preserving existing manual replacements. PDF, Excel and template remain frozen in `control/work/_mat` with user-facing copies in `_MAT`; SOURCES is never reread on resume.

## Version 0.2.63

Targeted credit rematching now uses the extracted PDF-reference photos when a quick build has a temporary `__lbb_missing_...` hire placeholder, so the model is never asked to identify a look from a blank board. The closed-board selector also accepts the compact label response used during schema repair, retries format-only failures with the same candidate set, and retains the existing one-to-one mapping for a quick-build look whose reference photo is genuinely unavailable.

## Version 0.2.62

The quick build now uses **autonomous credit matching**. Every initial Excel-card proposal is checked by the selected vision provider before credits are written; rejected matches are rematched from the complete unused-card pool while preserving the one-card-per-look rule. This internal proof does not start InDesign visual clearance or create a PDF, and it does not require manual confirmation. A temporary provider/response failure is retried and, only if necessary, falls back to the deterministic one-to-one map so the quick INDD build does not stop on a modal review error.

## Version 0.2.61

Added the explicit **Быстрая сборка — только INDD** mode. It runs the source, PDF-order, credit-map, structure, image and credits stages, then stops after the INDD pass. It does not run visual clearance or create any PDF. When a required retouched photo is absent or ambiguous, the complete look receives a documented blank placeholder in its fixed image frames; the PDF-reference pixels are used to map credits, and the selected vision provider internally rechecks that mapping before the INDD is saved. The selected mode is saved per project; intentionally skipped stages remain visible as skipped rather than failed. Switch back to **Полная сборка** when the project should continue through visual review and PDF export.

## Version 0.2.60

Added **OpenAI API** as a separate AI provider. Select it in the `ИИ` list, enter an `OPENAI_API_KEY` in the field that appears, choose a model (by default `gpt-5.6`) and press `Проверить модель`. The key is stored in Windows Credential Manager rather than the project database or lookbook folder. Text requests and image-proof checks use the direct OpenAI Responses API; Codex is not required for this mode.

## Version 0.2.37

Every new project freezes its PDF reference, Excel catalogue and automation template under `control/work/_mat` before creating the master. Photographs live in `_MAT/hires`; their identities are verified by the relevant matching/proof gates, not by the immutable document snapshot. On resume, LOOKBOOKBOT neither reads nor overwrites from the currently selected SOURCES folder.

## Version 0.2.36

Correction revisions are now explicitly two-phase: the copied INDD is marked as being built until the native CREDiTs pass has written and verified every saved edit. LOOKBOOKBOT shows “Версия готова к просмотру” only after the verified draft hash and exact current-master captions evidence agree, so a just-copied or subsequently overwritten source file cannot be mistaken for the finished correction version.

## Version 0.2.34

- Fixed revision continuity after a verified review PDF: a current `_NN_review.pdf` that is present **and** accepted by controller evidence now unlocks creation of the next `_NN+1.indd`. A merely similarly named or stale PDF never does.

## Version 0.2.33

- Credit-correction release is now split into two deliberate actions: **Create new version** creates and formats the next sequential INDD only; **Write PDF of selected version** runs the remaining release path and writes that revision's review PDF.
- `ПРОВЕРИТЬ ИЗМЕНЁННЫЕ ЛУКИ ПЕРЕД PDF` is now an explicit, saved revision option. It is on by default and renders/model-checks only the corrected LOOK pairs. When it is off, the controller records a clearly labelled scope-only route: native InDesign still proves unchanged spreads, image links, fixed frames and non-overset credits, but it never claims the changed credits were visually inspected.

## Version 0.2.32

- A credits-only correction no longer launches a second full visual audit. The app uses native InDesign comparison to prove every unedited look retained its images, crop geometry and credits, then renders and visually checks only the changed LOOK pairs before writing the next full review PDF.
- The visual progress indicator now switches to the number of corrected looks for this targeted revision cycle.

## Version 0.2.31

- The corrections release button now compares the actual edited credit cards before deciding whether a revision is required. A lost editor `textChanged` event can no longer discard an added product or make the button appear to do nothing.
- If the text is truly unchanged or a release is still active, the interface explains the exact reason instead of presenting the generic “no corrections” result.

## Version 0.2.30

- Creating a corrected `_02` revision now automatically rebinds the native structure profile to that exact copied INDD before credits are applied. InDesign regenerates internal frame IDs when a document is copied; the rebind verifies the existing structure and frames without changing the layout, preventing a false “credits frame moved” stop.
- This rebind is also applied automatically when reopening an interrupted older revision that has not yet begun the captions transaction.

## Version 0.2.29

- Caption corrections are now a non-destructive draft: saving any number of LOOK edits never modifies the reviewed `caption-data.tsv` or the current INDD.
- Before creating `_02`, the app automatically finishes the missing release and review-PDF proof for `_01`; only then does it copy the master and apply the saved draft to the new revision.
- Projects created by older builds are recovered automatically: if a legacy draft had already altered `caption-data.tsv`, its audited baseline is restored before the original review PDF is released, while the intended corrections remain preserved for `_02`.

## Version 0.2.28

- Credit corrections now accumulate automatically in one revision draft while moving between looks. The next sequential INDD and its matching review PDF remain bound to the same revision.
- The corrections panel shows the exact upcoming INDD/PDF names before release; final PDFs always derive their names from the current approved master revision.

## Version 0.2.27

- Fixed release-stage COM stability: all InDesign page items are now read through indexed collection access instead of the unstable COM enumerator.
- Release and review-PDF recovery now continue after repeated COM disconnects only while the saved master remains byte-identical; no fixed retry count can stop a safe, unchanged release.

- Added the `ПРАВКИ` workspace between visual audit and log. It shows the selected full-length and close-up images vertically beside an editable product list.
- Saving a correction creates a controlled source draft; `СОХРАНИТЬ И НАПИСАТЬ PDF НА ПРОВЕРКУ` copies the reviewed INDD to the next `_NN` revision, restores captions through the native `CREDiTs` style and writes the new revision's review PDF. Previous review files remain intact.
- The controller verifies that only the explicitly recorded LOOK credits differ from the approved Excel-derived data; all other looks stay frozen.

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
- автоматическое определение необязательной обложки PDF-референса: первая страница с двумя видимыми фото не пропускается;
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
