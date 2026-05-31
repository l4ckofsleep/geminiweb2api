# Changelog

Ниже — сводка наших совместных изменений по текущему состоянию проекта.

## 2026-05-31 - v1.3.2

### Fixed replies to previous messages
- Fixed replies to previous messages.

## 2026-05-28 — v1.3.1

### Исправления для chat.json и алиасов моделей
- В OpenAI-ветке `/v1/chat/completions` `content` теперь корректно нормализуется в строку. Раньше `content: null` (типично для assistant-сообщения с `tool_calls`) валил сервер с `AttributeError`, а массив частей (мультимодальный `[{text}, {image_url}]`) уходил в chat.json как структурированный объект и фактически терялся для Gemini — последнее сообщение юзера/префилл выглядело пустым.
- В Gemini-ветке `build_chat_history_from_gemini_contents` теперь не выбрасывает сообщения, состоящие только из `inlineData`/`fileData` (вместо текста подставляется плейсхолдер `[attachment]`), и `prefill_text` устойчиво приводится к строке.
- Добавлен SSE-эндпоинт `/v1beta/models/{model}:streamGenerateContent` — без него подключение «Google AI Studio» в SillyTavern со включённым стримом давало 404 на любой модели (включая `gemini-3.5-flash`).
- Учитывается поле `systemInstruction` — character card / persona из Google AI Studio больше не теряются и кладутся первым system-сообщением в chat.json.
- Алиас-матчер `normalize_requested_model` стал устойчивее: любая `*flash*` модель в `/v1beta` сводится к `gemini-3.5-flash-extended`, любая `*pro*` — к `gemini-3.1-pro-extended`, токены `extended/thinking` корректно разводятся; повторный префикс `models/` (в имени и URL) стрипается.

## 2026-04-24 — v1.2

### StreamGenerate / устойчивость текстовой генерации
- В `api.py` добавлено частичное сближение `StreamGenerate` request-envelope с Gemini Web.
- Реверс теперь вытаскивает из `/app` browser-context вроде `bl`, кандидата на `f.sid` и языка интерфейса, кэширует этот контекст и обновляет его при устаревании или нехватке данных.
- В URL `StreamGenerate` добавлен локальный монотонный `_reqid`.
- Заголовки `StreamGenerate` стали ближе к браузерным, включая расширенный `x-goog-ext-525001261-jspb` и `x-goog-ext-525005358-jspb`.

### Логирование и runtime-hardening
- Проект переведен на более надежное логирование через `logs/` с дневными и per-request логами.
- Исправлено падение логирования на Windows-консолях с `cp1251`, когда сообщение содержало emoji или другой Unicode.
- Исправлено падение SSE-стрима при сбросе request-scoped лог-контекста из другого async context.
- Типизация `log_utils.py` подчищена так, чтобы новые правки не добавляли свежий diagnostic noise.

### Проверка эндпоинтов
- Проведен route-level smoke для основных маршрутов проекта.
- Локально подтвержден проход для `/v1/models`, `/v1/chat/completions` (stream и non-stream), `/v1/images/generations`, Gemini-compatible `generateContent` для текста и картинок, а также `/images/{filename}`.

### README и метаданные
- README обновлен под актуальную систему логов и текущую версию.
- Версия проекта повышена до `1.2`.

## 2026-04-11

### Сессии, токены и восстановление
- Улучшено восстановление сессии при проблемах с токеном `SNlM0e`.
- На desktop скрипт теперь сначала пытается обновить токен по кукам, а если этого недостаточно — делает автоматический refresh сессии.
- На mobile/manual-flow вместо лишних desktop-попыток теперь выводится прямое указание запустить `start.py --reauth`.
- `keep_alive` больше не просто сообщает о смерти сессии, а участвует в восстановлении по актуальной логике платформы.

### Новый флаг запуска
- Добавлен флаг `--mobile`.
- Этот флаг принудительно переводит запуск в mobile-режим даже на desktop-системах.
- При `--mobile` отключаются desktop auto-refresh ветки и используется ручной cookie-flow.

### Linux flow
- Linux Desktop теперь считается mobile/manual-auth платформой по логике проекта.
- Для Linux добавлена ручная авторизация по кукам вместо ставки на Playwright-login.
- Пользователю на Linux теперь показываются отдельные инструкции по получению `__Secure-1PSID` и `SAPISID`.

### Roleplay safety
- В hidden prompt добавлена защита ролевой игры от утечки пользовательской IRL/OOC-информации.
- Gemini запрещено подтягивать внешние данные именно о пользователе, если они не были явно указаны в сцене, истории сообщений или прямом сообщении.
- Обычные внешние знания о мире при этом оставлены разрешёнными, если они не противоречат сцене.

### Desktop refresh / auto-refresh
- Desktop refresh и auto-refresh теперь умеют сначала пробовать тихий `headless`-refresh через сохранённый браузерный профиль.
- Для отладки добавлена проверка, были ли реально найдены ключевые куки `__Secure-1PSID` и `SAPISID`.
- Если тихий headless-refresh не помог, flow откатывается к обычному видимому окну браузера.

### Поддержка браузеров в auth flow
- Расширена цепочка fallback-браузеров для desktop auth/refresh.
- Текущий порядок попыток: Google Chrome → Microsoft Edge → Chromium → Opera → Brave → Vivaldi.
- Для path-based браузеров добавлены отдельные пути под Windows, macOS и Linux.

### README
- README обновлён под актуальный код проекта.
- Добавлена документация по `--mobile`.
- Исправлено описание Linux-авторизации: теперь там задокументирован ручной cookie-flow.
- Обновлено описание `--refresh`: на Windows / Mac сначала идёт headless-refresh, потом видимый fallback; на Linux / Android / `--mobile` — возврат к ручному вводу куков.
- Добавлены model aliases из кода.
- Добавлен отдельный блок про включение интернет-поиска в SillyTavern через `Google AI Studio` + proxy flow.
