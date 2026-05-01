# Build from source

Этот документ — для **разработчика или SRE**, который хочет:

- (a) собрать `FamiliaAdmin-vX.Y.Z.exe` локально из исходников,
- (b) поднять стек на ВМ вручную через `docker compose` (без
  админки), либо
- (c) понять архитектуру source-pack'а и update-flow для отладки.

Если вы конечный пользователь и хотите просто поднять familia за
5 минут — см. [`quickstart.md`](quickstart.md).

## Repo layout

```
family-assistant/
├── nanobot/        — vendored fork of nanobot (agent runtime, channels, MCP)
├── familia/        — наш слой: principals, policy, identity_resolver, tools
├── memx/           — vendored memX (memory backend, Redis-backed)
├── admin/          — Tauri 2 + React desktop app (admin .exe sources)
├── bin/            — release tooling (regen-lock, build-source-pack, release-admin)
├── docs/           — этот документ и соседи
├── docker-compose.yml          — gateway + redis + ingress
├── docker-compose.memx.yml     — memX backend (отдельный compose-проект)
├── Dockerfile                  — мульти-стейдж сборка gateway-образа
├── bootstrap.sh                — установка/апдейт на ВМ (запускается админкой)
└── principals.example.json     — стартовый шаблон для нового деплоя
```

## Building the admin .exe

### Prereqs

- **Rust 1.77+** (для Tauri 2).
- **Node 20+** и **pnpm 10+**.
- **Docker** — нужен для шага `build-source-pack` (компиляция в
  digest-pinned ephemeral container'е).
- Windows-машина, если нужен именно `.exe`. Cross-compile из Linux
  возможен, но в release-скрипте не поддержан.

### Полный релиз

```bash
bin/release-admin.sh 0.5.61
```

Скрипт:

1. Прогоняет `vitest` и Rust-тесты.
2. Запускает `bin/build-source-pack.sh`, кладёт результат в
   `admin/src-tauri/resources/familia-source.tar.gz`.
3. Поднимает версию в `admin/src-tauri/tauri.conf.json` и
   `admin/package.json`.
4. Запускает `pnpm tauri build` — получает
   `dist/admin/FamiliaAdmin-v0.5.61.exe` плюс `WebView2Loader.dll`.
5. Считает SHA-256 для каждого файла, пишет в
   `dist/admin/CHANGELOG.md` заготовку под release notes.

### Ручная сборка (без релиза)

```bash
cd admin
pnpm install
pnpm tauri build
```

Если файл `admin/src-tauri/resources/familia-source.tar.gz`
отсутствует, `bootstrap_source_pack()` в `lib.rs` падает на пустой
placeholder — `.exe` соберётся, но при установке source-pack
доставлять будет нечего. Это нормально для dev-сборок, когда вы
указываете админке уже готовый pack из другого пути.

### CI guard: `ALLOW_STALE_BACKEND_VERSION`

`release-admin.sh` отказывается выпускать релиз, если в трекаемых
backend-каталогах (`nanobot/`, `familia/src/`, `memx/`, `Dockerfile`,
`bootstrap.sh`) что-то менялось с прошлого тега, **но**
`familia/pyproject.toml::version` остался прежним. Это защита от
ситуации «выкатили admin 0.5.61 с новым backend'ом, но забыли
поднять backend semver — теперь update-flow думает, что обновляться
не на что».

Bypass для admin-only релиза, где backend намеренно не трогали:

```bash
ALLOW_STALE_BACKEND_VERSION=1 bin/release-admin.sh 0.5.61
```

## Manual install on a VM (no admin .exe)

Если админкой пользоваться не хочется (CI, headless, audit) —
ставится руками. На ВМ:

```bash
git clone https://github.com/<owner>/family-assistant.git
cd family-assistant

# 1. Конфиги:
cp principals.example.json ~/.nanobot/principals.json
cp memx-config/acl.example.json memx-config/acl.json
cp .env.example .env
cp familia/policy.example.yaml familia/policy.yaml

# 2. Сгенерировать уникальный 64-hex memx_key для каждого principal'а.
#    Заменить <replace_with_unique_key> в обоих файлах
#    (principals.json и acl.json) — они должны совпадать.
openssl rand -hex 32

# 3. memX поднимаем первым — gateway от него зависит:
docker compose -f docker-compose.memx.yml up -d --build

# 4. Затем gateway:
docker compose up -d --build

# 5. Smoke test:
docker compose logs -f familia-gateway
```

Sanity-проверки:

```bash
# memX отвечает изнутри gateway-контейнера:
docker exec familia-gateway curl -s \
  -H "X-API-Key: <owner_memx_key>" \
  http://memx-backend:8100/get?key=shared:test
# Ожидание: 404 (ключа нет), не connection refused.

# audit-лог пишется:
tail -f /opt/familia/audit.jsonl
```

Послать `/start` боту в Telegram/VK — должно вернуться приветствие
с вашим principal id.

## Backend bump rule

`familia/pyproject.toml::version` — это **backend** semver, отдельный
от admin .exe release-версии. Update-flow читает его при подключении
и сравнивает с тем, что вшит в `.exe`.

**Поднимать на каждое изменение** в:

- `nanobot/`
- `familia/src/`
- `memx/`
- `Dockerfile`
- `bootstrap.sh`

**Не поднимать** на admin-only изменения (frontend, локали, Tauri,
тесты админки, docs).

`bin/release-admin.sh` это enforce'ит — если backend изменился, а
pyproject не двинулся, релиз падает. Bypass — `ALLOW_STALE_BACKEND_VERSION=1`
(см. выше).

## `requirements.lock` regeneration

Direct-deps в `familia/pyproject.toml` и `nanobot/pyproject.toml`
зафиксированы по диапазонам (`httpx>=0.27,<1.0`). Транзитивные —
заморожены через `familia/requirements.lock`, который регенерируется:

```bash
bin/regen-lock.sh
```

Скрипт прокидывает текущие pyproject-файлы в **тот же
digest-pinned base image**, что и production Dockerfile, прогоняет
`uv pip compile --generate-hashes`, и переписывает
`familia/requirements.lock`. Гарантия — что lock соответствует
реально устанавливаемым колёсам в build-time.

Регенерируйте после:

- Поднятия любого диапазона прямой зависимости.
- Периодически (раз в месяц-два) для подтягивания security-фиксов
  в транзитивных.

Когда lock-файла нет, Dockerfile откатывается на range-resolve из
pyproject'ов — ставится по-прежнему то же самое в major-границах,
но drift в транзитивных уже возможен. Production-сборки должны
ехать с актуальным lock'ом.

## Source-pack architecture

Раньше admin тянул `ghcr.io/<owner>/familia-assistant:X.Y.Z` на ВМ.
Сейчас этого пути нет — образ собирается прямо на ВМ из вшитого
в `.exe` tarball'а. Это даёт:

- Полный контроль над содержимым релиза (никаких внешних registry).
- Reproducible builds — всё, что нужно для сборки, лежит рядом
  с `.exe`.
- Простота восстановления: tarball + `bootstrap.sh` достаточно для
  поднятия стека с нуля.

### Build time (`bin/build-source-pack.sh`)

Упаковывает в детерминированный `tar.gz` (sorted entries, fixed
mtime):

- `nanobot/{nanobot,bridge,pyproject.toml,...}`
- `familia/{src,pyproject.toml,policy.example.yaml,requirements.lock}`
- `memx/{src,Dockerfile,...}`
- `Dockerfile`, `docker-compose.yml`, `docker-compose.memx.yml`,
  `principals.example.json`, скрипты из `bin/`.

Результат — `admin/src-tauri/resources/familia-source.tar.gz`
(~3.6 МБ). Tauri через `include_bytes!` в `src-tauri/src/lib.rs`
вшивает его в финальный `.exe` на этапе `cargo build`.

### Runtime extraction

При первом запуске `.exe` функция `bootstrap_source_pack()` пишет
tarball в `%LOCALAPPDATA%\FamiliaAdmin\source\familia-source.tar.gz`
(один раз — повторный запуск проверяет SHA и пропускает запись).

### Install / update flow

При нажатии **Install** или **Update VM** админка:

1. SFTP'ит `familia-source.tar.gz` → `/opt/familia/source.tar.gz`.
2. SFTP'ит `bootstrap.sh` (тоже вшит в `.exe`) → `/tmp/bootstrap.sh`.
3. SSH-исполняет `bash /tmp/bootstrap.sh MODE=install` (или `update`).
4. Bootstrap распаковывает tarball в `/opt/familia/source/`, затем
   `docker compose build` собирает образ прямо на ВМ.

## Update flow (детально)

`bootstrap.sh MODE=update` отличается от `MODE=install`:

- **Skip** этапов `dirs` (каталоги уже есть) и `seed_graph` (граф
  семьи уже есть).
- **Keep** `prereqs` / `docker` / `probe_mirrors` — на случай, если
  с прошлого деплоя что-то на ВМ испортилось или появились новые
  зеркальные требования.
- `compose up -d --force-recreate` гарантирует пересоздание
  контейнеров под новый образ.

### Atomic `SOURCE_VERSION`

Файл `/opt/familia/SOURCE_VERSION` (содержит backend-semver)
**пишется только после** `wait_healthy` — то есть после того, как
gateway-контейнер прошёл healthcheck. Если update упал на середине,
файл остаётся со старым значением, и при следующем подключении
admin честно покажет «на ВМ старая версия, нужно повторить
обновление».

### Откуда читается версия на ВМ

Backend-версия для сравнения с admin'ом читается **из живого
контейнера**:

```bash
docker exec familia-gateway python3 -c \
  'import importlib.metadata; print(importlib.metadata.version("familia"))'
```

Не с диска. Это критично: если update частично прошёл (новые файлы
на диске, но контейнер не пересобрался) — версия в контейнере
останется старой, и admin это увидит.

## SIGHUP hot-reload contract

Большинство мутаций конфига (add/remove channel, approve pending
principal, set STT provider) **не перезапускают контейнер**. Вместо
этого:

1. `nanobot.cli.commands::_run_gateway` при старте регистрирует
   `loop.add_signal_handler(signal.SIGHUP, _on_reload)`.
2. `_on_reload` вызывает:
   - `familia.principals.reload_registry()` — перечитывает
     `principals.json` с диска, обновляет in-memory registry.
   - `ChannelManager.reload_from_disk(new_config)` — диффает
     `config.json` против текущих channel-инстансов, добавляет
     новые / убирает удалённые / переинициализирует изменённые.
3. Всё это сериализовано через `asyncio.Lock` с **one-deep coalescing**:
   если SIGHUP пришёл во время уже идущего reload'а, он встаёт в
   очередь как ровно один follow-up (idempotent re-read диска).
   Лавину сигналов это глушит.

### Со стороны админки

`signal_gateway_reload` (Rust) выполняет:

```bash
docker kill --signal=HUP familia-gateway
```

Если signal-путь падает (контейнер мёртв, docker недоступен) —
fallback на `restart_gateway_quiet`, полный `docker restart
familia-gateway`. Оба пути логируются через `tracing::info!` /
`tracing::warn!`, видно в `Diagnostics` в админке.

**Бюджет времени**: SIGHUP-reload — ~120 мс, full restart — ~30 с.

## Mirror fallbacks (quick reference)

Подробное описание — [`operations.md`](operations.md), раздел
*Mirror fallbacks*. Краткий список переменных окружения, которые
читает `bootstrap.sh`:

| Env var | Перебивает |
|---|---|
| `APT_MIRROR` | `deb.debian.org` (apt внутри образа) |
| `PIP_INDEX_URL` | PyPI для `pip` / `uv` внутри образа |
| `NPM_REGISTRY` | npm для сборки WhatsApp-bridge |
| `DOCKER_INSTALL_METHOD` | как ставится Docker: `auto` (по умолч.), `apt`, `get.docker.com` |

Если ни одна не задана и upstream недоступен (5-секундный probe) —
`bootstrap.sh` сам подбирает зеркало из baked-in списка
(Tsinghua / Yandex / aliyun / mirror.gcr.io / dockerhub.timeweb.cloud)
и пишет `+ APT_MIRROR (auto): <url>` в лог.

## Where to dig further

- [`quickstart.md`](quickstart.md) — пользовательский путь установки.
- [`operations.md`](operations.md) — backup/restore, диагностика,
  ротация ключей, mirror-фолбеки.
- [`architecture.md`](architecture.md) — почему gateway/memX/policy
  устроены именно так.
- [`policy.md`](policy.md) — модель privilege/ACL и peer-edge.
- [`security.md`](security.md) — threat model, что считается
  privileged-операцией.
- [`release.md`](release.md) — pipeline релиза admin'а и backend'а.
