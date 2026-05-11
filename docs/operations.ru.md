# Эксплуатация

Как вести `familia` каждый день: резервные копии, восстановление, уход за журналами и диском, ротация ключей, диагностика.

## Резервная копия

Через админку:

- **Maintenance** -> **Backup** -> выберите папку назначения.
- На выходе получается `familia-backup-<host>-<timestamp>.tar.gz` с `principals.json`, `policy.yaml`, `memx-config/acl.json`, полным содержимым тома memX и `audit.jsonl`.
- В резервной копии есть секреты. Храните ее так же аккуратно, как `.env`.

Вручную:

```bash
# На ВМ:
cd /opt/familia
docker compose down               # остановить писателей
tar czf familia-backup-$(date +%F).tar.gz \
    principals.json policy.yaml \
    memx-config/acl.json .env audit.jsonl \
    workspace/    # путь монтирования docker volume; зависит от драйвера
docker compose up -d
```

Резервные копии намеренно делаются в остановленном состоянии, когда контейнеры выключены. Живые снимки memX/Redis могут пересечься с активной записью; проект не пытается поддерживать согласованные живые копии.

## Восстановление для любой ВМ

Восстановление сделано **независимым от исходной ВМ**: UID/GID, имена томов и пути на исходнике не важны.

Через админку:

- **Install** -> **Restore from backup** -> выберите архив и новую целевую ВМ.
- Приложение загружает архив, распаковывает его во временный каталог, находит реальные пути томов через `docker inspect` и меняет владельца содержимого на UID целевого контейнера. Оно не предполагает, что исходная ВМ была такой же.

Вручную:

```bash
# На свежей ВМ, где стек familia скачан, но остановлен:
cd /opt/familia
docker compose down
tar xzf familia-backup-*.tar.gz -C ./
# Поправить владельца внутри docker volumes:
docker run --rm -v memx_data:/data alpine chown -R 1000:1000 /data
docker compose up -d
```

## Журналы

Журналы контейнеров ограничены через драйвер Docker `json-file` (20 МБ x 5 ротаций); см. `docker-compose.yml`. Просмотр:

```bash
docker compose logs -f familia-gateway
docker compose logs -f memx-backend
docker compose logs -f memx-redis
```

Журнал аудита (`audit.jsonl`) ротируется самим приложением: 50 МБ x 5.

## Уход за диском

Familia автоматически чистит:

- `media/` (скачанные вложения из чата) — срок жизни 24 часа, запуск каждый час.
- `workspace/sessions/*.jsonl` (журналы разговоров по сессиям) — срок жизни 90 дней, запуск каждый день.
- `workspace/.git` (внутреннее git-хранилище nanobot) — `git gc` раз в месяц.
- `audit.jsonl` — ротация на 50 МБ, хранится 5 поколений.

Использование диска видно в разделе **Maintenance** админки: свободно / всего на ВМ и объем по категориям.

## Ротация ключей

### Ключ memX отдельного участника

Когда ключ memX участника нужно сменить (подозрение на утечку, смена устройства):

```bash
# 1. Сгенерировать новый ключ:
openssl rand -hex 32 > /tmp/newkey

# 2. Атомарно изменить оба файла:
cd /opt/familia
NEW=$(cat /tmp/newkey)
# - обновить principals.json: principals.<id>.memx_key = $NEW
# - обновить memx-config/acl.json: заменить OLD key на NEW key
# - сохранить все разрешения областей

# 3. Перезапустить memX (перечитывает acl.json) и gateway (перечитывает principals.json):
docker compose -f docker-compose.memx.yml restart memx-backend
docker compose restart familia-gateway

# 4. Проверить, что участник все еще читает свои данные:
docker compose logs --tail=50 familia-gateway | grep -i actor
```

### Ключ ночного обработчика сжатия памяти

Ротация требует тех же действий и еще одного шага: ключ ночного обработчика сжатия памяти отдельно назван в `acl.json` и не привязан к участнику. Его утечка наиболее опасна: обладатель может перезаписать личную память любого участника. Относитесь к нему как к секрету уровня root.

## Запуск и остановка

```bash
# Остановить все:
cd /opt/familia
docker compose down
docker compose -f docker-compose.memx.yml down

# Запустить в правильном порядке (сначала memX):
docker compose -f docker-compose.memx.yml up -d
docker compose up -d
```

## Диагностика

Страница **Diagnostics** в админке запускает эквивалент:

```bash
# Проверка привязки личности:
docker exec familia-gateway python -c \
  "from familia.identity_resolver import resolve; \
   print(resolve('telegram', 12345))"

# Доступность memX:
docker exec familia-gateway curl -s \
  -H "X-API-Key: $(grep '^FAMILIA_OWNER_ACTOR' .env | cut -d= -f2)" \
  http://memx-backend:8100/get?key=shared:family.graph

# Хвост аудита:
tail -n 50 /opt/familia/audit.jsonl
```

Если аудит молчит во время реального чата, сломана привязка участника. Проверьте `channel_id`/`sender_id` в `principals.json` по метаданным сообщения в журналах контейнера.

## Типовые проблемы

| Симптом | Вероятная причина |
|---------|-------------------|
| `unknown principal: telegram/12345` | Telegram-чат не привязан к участнику в `principals.json` |
| Ответы общие, без учета вашего контекста | Неверный ключ LLM или исчерпана квота; `OPENAI_API_KEY` не попал в окружение gateway |
| `acl deny: scope=private:X:value:Y` | Чтение другого участника без связи доступа — так задумано, см. [`policy.ru.md`](policy.ru.md) |
| memX возвращает 401 | `acl.json` и `principals.json` разошлись: разные ключи memX |
| Telegram-бот молчит | webhook URL не задан или контейнер не может достучаться до `api.telegram.org` |
| Нет медиа из VK | CDN VK блокирует нероссийские IP; укажите `VK_PROXY` в `.env` на доверенный SOCKS5 |

## Зеркала для ВМ с ограниченным исходящим доступом

При запуске bootstrap автоматически проверяет стандартные источники (`pypi.org`, `deb.debian.org`, `registry.npmjs.org`, `get.docker.com`, `registry-1.docker.io`). Если какой-то источник недоступен с целевой ВМ, выбирается первое доступное зеркало из встроенного списка. Действия оператора не нужны. Автоматически выбранное зеркало пишется в журнал как `+ APT_MIRROR (auto): <url>`, чтобы было видно, что выбрано.

Встроенные списки намеренно короткие: одно хорошо известное зеркало на регион.

| Ресурс | Цепочка автоматического отката |
|--------|--------------------------------|
| PyPI | `pypi.tuna.tsinghua.edu.cn`, `mirrors.aliyun.com/pypi` |
| Debian apt | `mirror.yandex.ru/debian`, `mirrors.tuna.tsinghua.edu.cn/debian` |
| npm | `registry.npmmirror.com`, `mirrors.huaweicloud.com/repository/npm` |
| Docker Hub | `mirror.gcr.io`, `dockerhub.timeweb.cloud` (записывается в `/etc/docker/daemon.json`) |
| Установка Docker engine | откат на `apt install docker.io`, если `get.docker.com` заблокирован |

Чтобы **переопределить** автоматический выбор, например при наличии корпоративного внутреннего зеркала, задайте любую из этих переменных окружения до запуска bootstrap. Они побеждают без условий:

| Переменная окружения | Что переопределяет | Пример значения |
|----------------------|--------------------|-----------------|
| `APT_MIRROR` | `deb.debian.org` / `security.debian.org` внутри образа | `https://mirror.yandex.ru/debian` |
| `PIP_INDEX_URL` | индекс PyPI, который используют `pip` и `uv` внутри образа | `https://pypi.tuna.tsinghua.edu.cn/simple` или свой devpi |
| `NPM_REGISTRY` | npm registry для сборки WhatsApp bridge | `https://registry.npmmirror.com` |
| `DOCKER_INSTALL_METHOD` | способ установки Docker на хост без Docker | `auto` (по умолчанию: попробовать `get.docker.com`, затем `apt install docker.io`), `apt`, `get.docker.com` |

Для **базовых образов** (`ghcr.io/astral-sh/uv:...` и `python:3.12-slim`) Dockerfile закрепляет `FROM` по digest, поэтому единственная настройка — зеркало registry на уровне Docker daemon. Bootstrap автоматически пишет минимальный `/etc/docker/daemon.json` с `registry-mirrors`, когда проверка Docker Hub не проходит, но только если файл еще не объявляет свое зеркало. Чтобы заранее указать корпоративное зеркало, положите `daemon.json` перед установкой:

```jsonc
// /etc/docker/daemon.json
{
  "registry-mirrors": ["https://your.mirror.example.com"]
}
```

Для установок без админки bootstrap читает те же переменные окружения:

```bash
export APT_MIRROR=https://your-corp-mirror/debian
export PIP_INDEX_URL=https://your-corp-mirror/pypi/simple
sudo -E bash bootstrap.sh
```

## Воспроизводимые сборки Python (`requirements.lock`)

Прямые зависимости в `familia/pyproject.toml` ограничены следующим мажорным выпуском (`httpx>=0.27,<1.0` и т.п.), чтобы внезапный ломающий релиз не попал в пятничный `pip install`. Транзитивные зависимости тоже заморожены, но отдельным lock-файлом `familia/requirements.lock`, который обновляется по требованию:

```bash
bin/regen-lock.sh
```

Скрипт запускает `uv pip compile` внутри того же закрепленного по digest базового образа, который использует production Dockerfile, поэтому lock соответствует тому, что реально установится при сборке. Запускайте его после поднятия любой прямой зависимости или периодически, чтобы подтянуть security-исправления транзитивных зависимостей. Если lock-файла нет, Dockerfile откатывается к разрешению диапазонов из pyproject-файлов: прямые зависимости все еще ограничены мажорной версией, но дрейф транзитивных зависимостей уже возможен.
