# freelance-ai-parser

Парсер заказов с [freelance.ru](https://freelance.ru) с управлением через Telegram.
Фильтры настраиваются в боте → **локальная фильтрация отсекает мусор** → AI
(**DeepSeek**) вызывается **только** для прошедших заказов → подходящие летят в Telegram.

> Главный принцип: **токены DeepSeek не тратятся на заведомо нерелевантные заказы.**
> На реальной ленте из 25 заказов до AI доходит ~3 — остальное (нейминг, проекты
> домов, отзывы, презентации, рассылки) отсекается локально бесплатно.

## Поток данных

```
Telegram-бот ──настройка фильтров──> SQLite (таблица filters)
                                          │
                                   читает пайплайн
                                          ▼
parser.freelance_ru ──> свежесть ──> ЛОКАЛЬНЫЙ ФИЛЬТР ──┬── не прошёл ─> в БД, AI НЕ вызывается
                                                        │
                                                  прошёл▼
                                              AI-анализ (DeepSeek)
                                                        ▼
                                          score >= min_ai_score?
                                                   │ да
                                              Telegram (push)
```

## Структура

```
freelance-ai-parser/
├── bot.py                       # вход: Telegram-бот (long polling) + health-сервер
├── main.py                      # вход: разовый прогон пайплайна (для cron)
├── requirements.txt
├── .env.example
├── render.yaml                  # Render Blueprint (web service + PostgreSQL)
├── README.md
├── src/
│   ├── config.py                # env + профиль + дефолтные фильтры
│   ├── health.py                # HTTP health-сервер (/ , /health) для Render
│   ├── models.py                # Project, Analysis, Filters
│   ├── pipeline.py              # parse → фильтр → AI → store → notify
│   ├── parser/freelance_ru.py
│   ├── filtering/local_filter.py  # ← локальная фильтрация ДО AI
│   ├── ai/analyzer.py           # AIProvider + DeepSeekProvider
│   ├── telegram/
│   │   ├── bot.py               # aiogram: меню, FSM, callback, пагинация
│   │   └── notifier.py          # push подходящих заказов
│   └── storage/
│       ├── db.py                # SQLite + фабрика get_storage()
│       └── postgres.py          # PostgreSQL (включается при DATABASE_URL)
└── data/parser.db               # создаётся автоматически (только SQLite-режим)
```

### Хранилище: SQLite или PostgreSQL

Хранилище выбирается автоматически по переменной `DATABASE_URL`:

```python
# src/storage/db.py
def get_storage():
    url = (config.database_url or "").strip()
    if url.startswith(("postgres://", "postgresql://")):
        return PostgresStorage(url)   # DATABASE_URL задан → PostgreSQL
    return Storage(config.db_path)    # иначе → SQLite (data/parser.db)
```

- **Локально** `DATABASE_URL` не задан → работает SQLite, ничего менять не нужно.
- **На Render** задаётся `DATABASE_URL` (Internal Database URL) → тот же код
  работает на PostgreSQL. Интерфейс обоих классов хранилища идентичен, таблицы
  создаются автоматически при старте.

## Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env             # вписать ключи
```

В `.env` нужны: `DEEPSEEK_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
(остальное — параметры по умолчанию, см. `.env.example`).

## Запуск

```bash
python bot.py        # запустить бота, дальше всё через Telegram
```

В Telegram → `/start` → главное меню:

- **📅 Найденные за неделю** — заказы, прошедшие фильтр и AI (с пагинацией);
  под каждым: «💬 Сгенерировать ответ» и «🔗 Перейти к заказу».
- **⚙️ Настроить фильтры** — пошагово (ключевые слова → стоп-слова → мин. бюджет →
  макс. бюджет → мин. AI Score), сохраняется в SQLite.
- **✏️ Редактировать фильтры** — изменить отдельные поля или сбросить.
- **🚀 Запустить проверку сейчас** — разовый прогон парсера.
- **❓ Помощь**.

### Регулярный парсинг (cron)

Бот можно держать запущенным для управления, а парсинг повесить на cron — он
читает те же фильтры из SQLite:

```cron
*/30 * * * * cd /путь/к/проекту && /путь/к/.venv/bin/python main.py >> data/run.log 2>&1
```

## Фильтрация: ориентир на ТИП задачи, не на стек

Ключевые слова описывают **тип** работы (сайт, лендинг, CRM, бот, парсер,
автоматизация, AI, MVP, личный кабинет, админка, интеграция…), а не технологию
(React/Vue/Node/Python), потому что стек реализации может быть любым.

Логика `src/filtering/local_filter.py`:
1. **стоп-слова** (1с, bitrix, wordpress, casino…) → мгновенный отказ;
2. **ключевые слова** → нужно хотя бы одно совпадение;
3. **бюджет** → проверка min/max (неизвестный «обсуждается» бюджет не блокирует — решает AI).

Совпадения ищутся по границе слова: «ai» не сработает внутри «email», «1с»
сработает в «1С-разработка».

## Деплой на Render (Free)

Бот рассчитан на круглосуточную работу как **Web Service** на Render Free:
вместе с long polling поднимается HTTP health-сервер (`/`, `/health`), а данные
хранятся в PostgreSQL. Docker не нужен.

### 1. Репозиторий на GitHub

```bash
git init
git add .
git commit -m "initial commit"
git remote add origin <repo-url>
git push -u origin main
```

> `.env` и локальные `*.db` уже в `.gitignore` — токены и база в GitHub не попадут.

### 2. PostgreSQL

1. Открой [Render Dashboard](https://dashboard.render.com) → **New → PostgreSQL**.
2. План — **Free**, создай базу.
3. Скопируй **Internal Database URL** (вид `postgresql://user:pass@host/db`).

### 3. Web Service

1. **New → Web Service**, подключи свой GitHub-аккаунт.
2. Выбери репозиторий проекта.
3. Render подхватит настройки из `render.yaml`. Если задаёшь вручную:
   - **Runtime:** Python
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot.py`
   - **Health Check Path:** `/health`

> Можно сразу использовать `render.yaml` через **New → Blueprint** — он создаёт
> и базу, и web-сервис, и связывает `DATABASE_URL` автоматически.

### 4. Переменные окружения

В разделе **Environment** web-сервиса задай:

| Переменная           | Значение                                  |
|----------------------|-------------------------------------------|
| `DATABASE_URL`       | Internal Database URL из шага 2           |
| `TELEGRAM_BOT_TOKEN` | токен бота                                 |
| `TELEGRAM_CHAT_ID`   | твой Telegram chat id                      |
| `DEEPSEEK_API_KEY`   | ключ DeepSeek                              |

`PORT` Render подставляет сам — задавать не нужно. Остальные параметры
(`DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL`, `PAGES_TO_PARSE` и т.д.) — по желанию,
см. `.env.example`.

### 5. Проверка

После деплоя открой:

```
https://<service>.onrender.com/health
```

Должно вернуть `OK`. Бот при этом автоматически подключится к Telegram
(long polling) — отправь `/start`.

## Замечания

- Парсер опирается на текущую вёрстку freelance.ru — селекторы в одном месте
  (`src/parser/freelance_ru.py`).
- Другой LLM вместо DeepSeek — добавь класс-наследник `AIProvider` и верни его
  из `get_provider()` в `src/ai/analyzer.py`.
- MVP рассчитан на одного пользователя (доступ к боту ограничен `TELEGRAM_CHAT_ID`).
```
