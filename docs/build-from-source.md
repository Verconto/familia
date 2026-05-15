# Сборка из исходников

Этот документ для **разработчика или SRE**, который хочет:

- собрать `FamiliaAdmin-vX.Y.Z.exe` локально из исходников;
- поднять стек на ВМ вручную через `docker compose`, без админки;
- понять архитектуру архива исходников и поток обновления для отладки.

Если вы конечный пользователь и хотите просто поднять familia за 5 минут, см. [`quickstart.md`](quickstart.md).

## Структура репозитория

```text
family-assistant/
├── nanobot/        — встроенный форк nanobot (цикл агента, каналы, MCP)
├── familia/        — наш слой: principals, policy, identity_resolver, tools
├── memx/           — встроенный memX (хранилище памяти на Redis)
├── bin/            — инструменты выпуска (regen-lock, build-source-pack, release-admin)
├── docs/           — этот документ и соседние документы
├── docker-compose.yml          — gateway + redis + ingress
├── docker-compose.memx.yml     — бэкенд memX (отдельный compose-проект)
├── Dockerfile                  — многостадийная сборка образа gateway
├── admin/src-tauri/resources/bootstrap.sh
│                               — установка/обновление на ВМ, запускается админкой
└── principals.example.json     — стартовый шаблон нового развертывания
```

> **Заметка про `admin/`.** Исходники Tauri/React админки **не публикуются в этом репозитории**. Публикуются только готовые артефакты: `.exe` + `WebView2Loader.dll` в [Releases][rel]. Это сознательное решение: подписи кода для неподписанных бинарников хобби-проекта нет, а поддерживать одновременно публичную кодовую базу интерфейса и бэкенд — вне бюджета автора. Если это нужно, форкайте бэкенд и пишите свой клиент. Контракт IPC описан в `nanobot/nanobot/cli/commands.py` (`familia rpc-server`) и `bin/build-source-pack.sh`.
>
> [rel]: https://github.com/Verconto/familia/releases/latest

## Что собирается из этого репозитория

Собирается то, что работает **внутри контейнера `gateway`**: Python-пакеты `nanobot`, `familia`, `memx`. Сборка выполняется на целевой ВМ через `docker compose build`: ее запускает админка как часть потока обновления или оператор вручную после `git clone` на ВМ; см. [ручную установку на ВМ](#ручная-установка-на-вм-без-admin-exe).

`bin/build-source-pack.sh` создает архив исходников (`nanobot/`, `familia/src/`, `memx/`, `Dockerfile`, compose YAML-файлы, `bootstrap.sh`). При сборке админка встраивает этот архив в `.exe` через `include_bytes!`. Если вы форкаете проект и пишете свой клиент, именно этот набор нужно доставить на ВМ.

### Защита CI: `ALLOW_STALE_BACKEND_VERSION`

`bin/release-admin.sh` запускается на стороне автора при выпуске. В публичном репозитории его видимый эффект — поднятие версии бэкенда. Скрипт отказывается выпускать релиз, если с прошлого тега менялись `nanobot/`, `familia/src/`, `memx/`, `Dockerfile` или `bootstrap.sh`, но `familia/pyproject.toml::version` остался прежним. Это защита от сценария: "выпустили админку с новым бэкендом, забыли поднять semver бэкенда, поток обновления решил, что обновлять нечего".

Обход: `ALLOW_STALE_BACKEND_VERSION=1 bin/release-admin.sh ...`.

## Ручная установка на ВМ без `admin .exe`

Если админкой пользоваться не хочется (CI, без графического интерфейса, аудит), установите вручную на ВМ:

```bash
git clone https://github.com/<owner>/family-assistant.git
cd family-assistant

# 1. Конфиги:
cp principals.example.json ~/.nanobot/principals.json
cp memx-config/acl.example.json memx-config/acl.json
cp .env.example .env
cp familia/policy.example.yaml familia/policy.yaml

# 2. Сгенерировать уникальный 64-hex memx_key для каждого участника.
#    Заменить <replace_with_unique_key> в обоих файлах:
#    principals.json и acl.json. Значения должны совпадать.
openssl rand -hex 32

# 3. Сначала поднять memX: gateway зависит от него.
docker compose -f docker-compose.memx.yml up -d --build

# 4. Затем gateway:
docker compose up -d --build

# 5. Дымовая проверка:
docker compose logs -f familia-gateway
```

Проверки здравого смысла:

```bash
# memX отвечает изнутри контейнера gateway:
docker exec familia-gateway curl -s \
  -H "X-API-Key: <owner_memx_key>" \
  http://memx-backend:8100/get?key=shared:test
# Ожидание: 404 (ключа нет), а не connection refused.

# журнал аудита пишется:
tail -f /opt/familia/audit.jsonl
```

Отправьте `/start` боту в Telegram/VK. В ответ должно прийти приветствие с вашим principal id.

## Правило поднятия версии бэкенда

`familia/pyproject.toml::version` — это SemVer **бэкенда**, отдельный от версии выпуска админки `.exe`. Поток обновления читает его при подключении и сравнивает с версией, встроенной в `.exe`.

**Поднимать версию при каждом изменении** в:

- `nanobot/`
- `familia/src/`
- `memx/`
- `Dockerfile`
- `bootstrap.sh`

**Не поднимать версию** при изменениях только админки: интерфейс, локали, Tauri, тесты админки, документация.

`bin/release-admin.sh` это проверяет. Если бэкенд изменился, а pyproject не сдвинулся, выпуск падает. Обход: `ALLOW_STALE_BACKEND_VERSION=1`; см. выше.

## Пересборка `requirements.lock`

Прямые зависимости в `familia/pyproject.toml` и `nanobot/pyproject.toml` закреплены диапазонами (`httpx>=0.27,<1.0`). Транзитивные зависимости заморожены через `familia/requirements.lock`, который пересобирается командой:

```bash
bin/regen-lock.sh
```

Скрипт подставляет текущие pyproject-файлы в **тот же базовый образ, закрепленный по digest**, который использует production Dockerfile, запускает `uv pip compile --generate-hashes` и переписывает `familia/requirements.lock`. Это гарантирует, что lock соответствует колесам, которые реально установятся во время сборки.

Пересобирайте после:

- поднятия любого диапазона прямой зависимости;
- периодически, раз в месяц-два, чтобы подтянуть security-исправления транзитивных зависимостей.

Если lock-файла нет, Dockerfile откатывается к разрешению диапазонов из pyproject-файлов. Прямые зависимости остаются в границах мажорных версий, но дрейф транзитивных зависимостей уже возможен. Production-сборки должны идти с актуальным lock.

## Архитектура архива исходников

Раньше админка тянула `ghcr.io/<owner>/familia-assistant:X.Y.Z` на ВМ. Сейчас этого пути нет: образ собирается прямо на ВМ из архива, встроенного в `.exe`. Это дает:

- полный контроль над содержимым релиза, без внешнего registry;
- воспроизводимые сборки: все нужное для сборки лежит рядом с `.exe`;
- простое восстановление: архива и `bootstrap.sh` достаточно, чтобы поднять стек с нуля.

### Этап сборки (`bin/build-source-pack.sh`)

Скрипт упаковывает детерминированный `tar.gz`: отсортированные записи, фиксированный mtime.

Внутри:

- `nanobot/{nanobot,bridge,pyproject.toml,...}`
- `familia/{src,pyproject.toml,policy.example.yaml,requirements.lock}`
- `memx/{src,Dockerfile,...}`
- `Dockerfile`, `docker-compose.yml`, `docker-compose.memx.yml`, `principals.example.json`, скрипты из `bin/`.

Результат — архив ~3.6 МБ, который кладется в ресурсы Tauri-проекта вне этого публичного репозитория. Tauri встраивает его в итоговый `.exe` через `include_bytes!` на этапе `cargo build`. Если вы форкаете проект и пишете свой клиент, встраивайте архив так же или скачивайте отдельным файлом при установке и передавайте путь.

### Распаковка при запуске

При первом запуске `.exe` функция `bootstrap_source_pack()` пишет архив в `%LOCALAPPDATA%\FamiliaAdmin\source\familia-source.tar.gz`. Запись выполняется один раз: при следующих запусках проверяется SHA, и если архив не изменился, запись пропускается.

### Поток установки и обновления

При нажатии **Install** или **Update VM** админка:

1. загружает `familia-source.tar.gz` по SFTP в `/opt/familia/source.tar.gz`;
2. загружает встроенный `bootstrap.sh` по SFTP в `/tmp/bootstrap.sh`;
3. выполняет по SSH `bash /tmp/bootstrap.sh MODE=install` или `MODE=update`;
4. bootstrap распаковывает архив в `/opt/familia/source/`, затем `docker compose build` собирает образ прямо на ВМ.

## Поток обновления подробно

`bootstrap.sh MODE=update` отличается от `MODE=install`:

- пропускает `dirs`: каталоги уже есть;
- пропускает `seed_graph`: семейный граф уже есть;
- сохраняет `prereqs`, `docker`, `probe_mirrors` на случай, если с прошлого развертывания на ВМ что-то изменилось или появились новые требования к зеркалам;
- `compose up -d --force-recreate` гарантирует пересоздание контейнеров под новый образ.

### Атомарный `SOURCE_VERSION`

`/opt/familia/SOURCE_VERSION` содержит SemVer бэкенда и пишется **только после** `wait_healthy`, то есть после успешного healthcheck контейнера gateway. Если обновление падает на середине, файл остается со старым значением, и при следующем подключении админка честно показывает, что на ВМ все еще старая версия и обновление нужно повторить.

### Откуда читается версия на ВМ

Версия бэкенда для сравнения с админкой читается **из живого контейнера**:

```bash
docker exec familia-gateway python3 -c \
  'import importlib.metadata; print(importlib.metadata.version("familia"))'
```

Не с диска. Это важно: если обновление частично прошло (новые файлы на диске есть, контейнер не пересобрался), версия внутри контейнера останется старой, и админка это увидит.

## Договор горячей перезагрузки через SIGHUP

Большинство изменений конфига (добавить/удалить канал, подтвердить ожидающего участника, выбрать STT-поставщика) **не перезапускают контейнер**. Вместо этого:

1. `nanobot.cli.commands::_run_gateway` при старте регистрирует `loop.add_signal_handler(signal.SIGHUP, _on_reload)`.
2. `_on_reload` вызывает:
   - `familia.principals.reload_registry()` — перечитывает `principals.json` с диска и обновляет in-memory registry;
   - `ChannelManager.reload_from_disk(new_config)` — сравнивает `config.json` с текущими экземплярами каналов, добавляет новые, удаляет удаленные и переинициализирует измененные.
3. Весь путь сериализован через `asyncio.Lock` с **одним отложенным повтором**. Если SIGHUP пришел во время уже идущей перезагрузки, он превращается ровно в один следующий запуск: идемпотентное перечитывание с диска. Это гасит лавину сигналов.

### Со стороны админки

`signal_gateway_reload` в Rust выполняет:

```bash
docker kill --signal=HUP familia-gateway
```

Если путь через сигнал падает (контейнер мертв, Docker недоступен), запасной путь — `restart_gateway_quiet`, полный `docker restart familia-gateway`. Оба пути пишутся через `tracing::info!` / `tracing::warn!`; это видно в **Diagnostics** в админке.

**Бюджет времени**: перезагрузка через SIGHUP — около 120 мс, полный перезапуск — около 30 с.

## Зеркала: краткая справка

Подробности: [`operations.md`](operations.md), раздел "Зеркала для ВМ с ограниченным исходящим доступом". Короткий список переменных окружения, которые читает `bootstrap.sh`:

| Переменная окружения | Что переопределяет |
|---|---|
| `APT_MIRROR` | `deb.debian.org` для apt внутри образа |
| `PIP_INDEX_URL` | PyPI для `pip` / `uv` внутри образа |
| `NPM_REGISTRY` | npm для сборки WhatsApp bridge |
| `DOCKER_INSTALL_METHOD` | способ установки Docker: `auto` (по умолчанию), `apt`, `get.docker.com` |

Если ни одна переменная не задана и основной источник недоступен (проверка 5 секунд), `bootstrap.sh` сам выбирает зеркало из встроенного списка (Tsinghua / Yandex / aliyun / mirror.gcr.io / dockerhub.timeweb.cloud) и пишет `+ APT_MIRROR (auto): <url>` в журнал.

## Куда копать дальше

- [`quickstart.md`](quickstart.md) — пользовательский путь установки.
- [`operations.md`](operations.md) — резервные копии, восстановление, диагностика, ротация ключей, зеркала.
- [`architecture.md`](architecture.md) — почему gateway/memX/policy устроены именно так.
- [`policy.md`](policy.md) — модель привилегий, список доступа и связи между участниками.
- [`security.md`](security.md) — модель угроз и что считается привилегированной операцией.
- [`release.md`](release.md) — выпуск версий админки и бэкенда.
