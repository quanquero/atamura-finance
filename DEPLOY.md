# DEPLOY / RUNBOOK — ATAMŪRA Finance

Эксплуатационная памятка фин-ядра. Секреты (токены/пароли) в файле НЕ хранятся — только имена
переменных и где они лежат.

## Архитектура (две машины)

```
1С-сервер (Windows, внутренний)                 finance-сервер (Linux, 5.35.105.105)
  parser_1c_odata.py  ──PUSH HTTPS──►  /api/ingest → SQLite (ядро) → дашборд finance.atamura.group
  (Планировщик задач, каждые 2 часа)             (systemd atamura-finance, nginx/HTTPS)
```

1С-сервер закрыт наружу — не мы ходим в 1С, а **парсер сам шлёт срез** к нам (outbound). Базу 1С в
интернет не выставляем.

---

## 1С-сервер — авто-сбор (Windows Task Scheduler)

Файлы парсера лежат в **`C:\atamura-1c\`**: `parser_1c_odata.py`, `bases.json` (список баз + креды +
`push_url`/`push_key`), `.env`, `finance_1c.sqlite3` (локальное ядро парсера).

Python на сервере: `C:\Users\Администратор\AppData\Local\Programs\Python\Python314\python.exe`.

**Батник** `C:\atamura-1c\run_1c_push.bat` (создаётся через PowerShell; важны две вещи —
`PYTHONIOENCODING=utf-8`, иначе падает на символе `⚠` при записи в файл, и **полный путь к python**,
т.к. задача крутится под SYSTEM с другим PATH):

```bat
@echo off
cd /d C:\atamura-1c
set PYTHONIOENCODING=utf-8
echo ===== %date% %time% ===== >> push_log.txt
"C:\Users\Администратор\AppData\Local\Programs\Python\Python314\python.exe" parser_1c_odata.py >> push_log.txt 2>&1
```

**Задача** (создать один раз, PowerShell от админа):
```powershell
schtasks /create /tn "Atamura1C_Push" /tr "C:\atamura-1c\run_1c_push.bat" /sc hourly /mo 2 /ru SYSTEM /f
```
Периодичность: **каждые 2 часа**, под системой (без пароля).

**Операции:**
```powershell
schtasks /run /tn "Atamura1C_Push"                         # прогнать сейчас
Get-Content C:\atamura-1c\push_log.txt -Tail 5 -Encoding UTF8   # лог (UTF-8; без -Encoding будут кракозябры)
schtasks /query /tn "Atamura1C_Push" /v /fo LIST           # статус/последний запуск
```
Успех в логе: `Отправлено на https://finance.atamura.group/api/ingest: {'ok': True, 'saved': N}`.
Проверка снаружи: на дашборде обновилась дата среза (шапка «срез 1С: …»).

Базы `Atree / Atwo / Nuova / NextCity` пропускаются (HTTP 401 — нет пароля в `bases.json`), это ожидаемо.

---

## finance-сервер (Linux, 5.35.105.105)

- **Сервис:** systemd `atamura-finance`, `WorkingDirectory=/home/niyaz/atamura-finance`,
  `ExecStart=/usr/bin/python3 server.py` (порт 8013, за nginx/HTTPS `finance.atamura.group`).
- **Только stdlib** (сервер), плюс `openpyxl` (реестр) и `@anthropic-ai/claude-code` на хосте (чтение договоров).
- **`.env`** (gitignore) — `SERVICE_KEY`, `BITRIX_WEBHOOK`, `ADATA_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`,
  `AUTH_USERS` (`login:sha256hex;…`), `SESSION_SECRET`. `BITRIX_WEBHOOK` и `SERVICE_KEY` также в systemd `Environment=`.
- **Деплой:** `git pull && sudo systemctl restart atamura-finance`.

**Маршруты:** `/` (SPA), `/data.json`, `/nakopitel.json` — под входом; `/sync`, `/sync-full`,
`/refresh` — синхронизация Bitrix; `/api/ingest`, `/api/reestr` — приём (по `X-Service-Key`, без входа);
`/healthz`; `/login`, `/logout`.

**Вход (V1.0.0):** простой логин/пароль (`AUTH_USERS` + сессия в подписанной куке). Нет пользователей
→ вход выключен. SSO через Кронос — стадия 2. Хэш пароля:
`python3 -c "import hashlib,getpass;print(hashlib.sha256(getpass.getpass().encode()).hexdigest())"`.

---

## Накопитель (чтение договоров, на finance-сервере)

`claude-code` установлен на хост: `npm install -g @anthropic-ai/claude-code@2.1.217`, авторизация
`CLAUDE_CODE_OAUTH_TOKEN`. Чтение: `bx_reader.py` (заявка Bitrix 178 → вложения → `claude -p` читает
PDF/сканы) → `nakopitel.py` → таблица `nakopitel` + `adata_cache`.

```bash
python3 tools/nakopitel_read.py <№заявки> [БИН]   # одна заявка (диагностика)
python3 tools/nakopitel_batch.py <лимит>          # батч по активным заявкам (крупные первыми)
python3 tools/reestr_import.py <xlsx>             # импорт реестра финотдела
```
