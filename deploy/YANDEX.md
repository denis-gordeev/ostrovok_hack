# Казань: Overpass + OSMnx API на Yandex Cloud

> Архив: VM и её SSD 80 GiB удалены 26 октября 2026 по запросу пользователя.
> Отсутствие обоих ресурсов подтверждено через YC CLI.
> Текущий стенд — VPS 188.241.197.121; сведения ниже исторические.

## Развёрнутый стенд

- VM: `ostrovok-kazan`, ID `fv4f3lurgr9e67va9kl6`.
- Зона: `ru-central1-d`, платформа `standard-v3`.
- Ресурсы: 2 vCPU (100%), 16 GiB RAM, 80 GiB `network-ssd`.
- IP: `213.130.83.185` (динамический; после остановки/запуска может измениться).
- Код и Compose: `/opt/ostrovok`.
- Security group: `enpov8j1krjv2e1npiju`. Входящие 22, 8080, 8081 разрешены
  с адресов клиента `46.242.15.146/32` и его HTTP-прокси `23.95.85.12/32`.
  При смене внешнего IP нужно добавить новый адрес в эту security group.

```bash
ssh denis@213.130.83.185
cd /opt/ostrovok
docker compose ps
docker compose logs --tail=100 app overpass
```

Контейнеры запускаются после перезагрузки VM (`restart: unless-stopped`).
Данные и кэш находятся в каталогах хоста и сохраняются при пересоздании контейнеров.

## Источник и границы

Статический снимок OSM. Скрипт `prepare-data.sh` пытается скачать Татарстан
с download.openstreetmap.fr, при ошибке использует Приволжский округ Geofabrik.
Казань и окрестности вырезаются через `osmium extract --strategy smart`.
Границы: west=48.75, south=55.55, east=49.55, north=56.10; включён аэропорт.
Из-за сохранения целостности объектов некоторые геометрии могут выходить за bbox.
Метаданные и контрольная сумма вырезки: `data/extract/fileinfo.txt`, `SHA256SUMS`.
Последние изменения объектов в текущей вырезке: `2026-10-24T19:09:38Z`.
Размер PBF: 20 096 009 байт; база Overpass после импорта — около 1.2 GiB.
Автоматическая репликация и Overpass areas отключены. Запросы делайте через
координаты, bbox или OSM IDs; поиск областей через `area` не настроен.
Данные © OpenStreetMap contributors, https://www.openstreetmap.org/copyright.

## Overpass

Endpoint: `http://213.130.83.185:8081/api/interpreter`.

```bash
curl --noproxy '*' --fail --data-urlencode \
  'data=[out:json][timeout:25];nwr(around:500,55.7961,49.1064)[leisure=park];out center tags;' \
  http://213.130.83.185:8081/api/interpreter
```

Подключение локального агента в `.env`:

```env
OVERPASS_URL=http://213.130.83.185:8081/api
OVERPASS_RATE_LIMIT=false
REQUEST_TIMEOUT=120
```

`OVERPASS_RATE_LIMIT=false` отключает отдельный запрос OSMnx к `/status`;
для публичного сервера оставляйте значение `true`.

## OSMnx HTTP API

Swagger: `http://213.130.83.185:8080/docs`.

- `GET /health` — состояние процесса, регион и upstream. Это liveness-проверка;
  готовность Overpass при запуске проверяется отдельно в Compose.
- `POST /features` — поиск объектов и расстояния по прямой.
- `POST /route` — кратчайший пеший маршрут в локальном графе.

```bash
curl --noproxy '*' --fail http://213.130.83.185:8080/health
curl --noproxy '*' --fail http://213.130.83.185:8080/features \
  -H 'Content-Type: application/json' \
  -d '{"lat":55.7961,"lon":49.1064,"tags":{"leisure":"park"},"radius_m":500}'
curl --noproxy '*' --fail http://213.130.83.185:8080/route \
  -H 'Content-Type: application/json' \
  -d '{"start":{"lat":55.7961,"lon":49.1064},"end":{"lat":55.7905,"lon":49.1144}}'
```

Координаты ограничены bbox Казани; радиус поиска — до 5 км, расстояние между
концами маршрута по прямой — до 10 км. Ближайшие узлы графа должны быть не дальше
300 м от заданных точек. В длину маршрута не включён путь до этих узлов;
смещения возвращаются отдельно. Время — оценка при скорости 4.5 км/ч.
Маршрут строится внутри bbox с запасом вокруг концов; глобально более короткий
маршрут может выходить за этот bbox. Доступность для колясок не гарантируется.

Кэшируются сырые HTTP-ответы, обработанные features/routes и графы GraphML.
Хранилище API: `.cache/server/` на хосте; база Overpass: `data/overpass/`.
API последовательно выполняет геооперации, чтобы не перегружать небольшую VM.
Сервис не требует LLM-ключа. Исходники агента также находятся на VM;
для запуска агента отдельно настройте его LLM-переменные окружения.

## Повторное развёртывание

Нужны Docker с Compose, `osmium-tool`, curl, достаточно памяти для вырезания
регионального PBF. Подготовка данных и первичный импорт выполняются один раз.

```bash
# Код должен находиться в /opt/ostrovok
cd /opt/ostrovok
./deploy/prepare-data.sh
docker compose up -d --build
```

Дождитесь завершения импорта и состояния healthy у Overpass и app.
Для обновления снимка сначала сохраните предыдущую базу и вырезку, подготовьте
новые данные в отдельном каталоге, импортируйте их и только затем переключите
сервис. После переключения очистите кэш API, чтобы он не возвращал старые данные.

## Источники по инфраструктуре

- https://github.com/wiktorn/Overpass-API
- https://docs.osmcode.org/osmium/latest/osmium-extract.html
- https://download.geofabrik.de/russia/volga-fed-district.html

## Проверки стенда

Проверены реальный Overpass-запрос, `/health`, `/features`, `/route`,
повторное чтение features/routes из кэша и отклонение координат вне региона.
Для точки `55.7961, 49.1064` найдено 7 парков в радиусе 500 м;
ближайший — Ново-Кремлевский сквер, 214.5 м до геометрии по прямой.
Маршрут из этой точки в `55.7905, 49.1144`: 888 м, оценка 11.8 минуты.

После перезапуска обоих контейнеров повторно проверены Overpass, features,
маршрут и сохранность кэша. Локальный `.env` агента переключён на этот Overpass.

## LangGraph CLI с HTTPS и паролем

- API и Swagger: **https://213.130.83.185:8443/docs**.
- Studio: https://smith.langchain.com/studio/?baseUrl=https%3A%2F%2F213.130.83.185%3A8443
- Логин: `denis`. Пароль и готовый заголовок Authorization сохранены локально
  в `.cache/deploy/langgraph-access.txt` (файл не коммитится, права 0600).
- Для входа в Swagger браузер запросит логин и пароль (HTTP Basic через TLS).
- В Studio откройте **Configure connection**, задайте Base URL
  `https://213.130.83.185:8443` и в **Custom Headers** добавьте `Authorization`
  со значением `Basic …` из файла доступа. Если Studio попросит разрешить
  подключение к этому адресу, добавьте его в Advanced Settings самостоятельно.
  Не передавайте пароль в URL.

Backend запущен командой `langgraph dev` в отдельном контейнере; его порт 2024
не опубликован на хосте. Nginx проверяет пароль перед проксированием всех запросов,
кроме CORS OPTIONS, и не передаёт заголовок с паролем в LangGraph.
Проверены отказ 401 без пароля и с неверным паролем, доступ с правильным паролем,
регистрация `geo_agent`, создание треда и CORS для Studio.

Это dev-сервер для прототипа. История хранится в `.cache/langgraph/`,
геокэш — в `.cache/server/`; оба каталога сохраняются на хосте.
Автоперезагрузка исходников отключена, одновременных запусков агента — один.
Трассировка LangSmith отключена. Studio — отдельный интерфейс LangSmith,
его исходники на VM не развёрнуты.

Управление дополнительными контейнерами:

```bash
cd /opt/ostrovok
docker compose -f docker-compose.yml -f deploy/compose.langgraph.yml ps
docker compose -f docker-compose.yml -f deploy/compose.langgraph.yml logs --tail=100 langgraph langgraph-gateway
docker compose -f docker-compose.yml -f deploy/compose.langgraph.yml up -d --build langgraph langgraph-gateway
```

The full environment is now stored at `/opt/ostrovok/.env` (0600).
It is copied from the local `.env`, preserving LLM settings.
Host endpoints: `OVERPASS_URL=http://127.0.0.1:8081/api`,
`OSMNX_API_URL=http://127.0.0.1:8080`.
Compose loads `.env` and overrides these inside Docker with
`http://overpass/api` and `http://app:8080` respectively.
`OSMNX_API_URL` is configured for HTTP API clients; the current agent
still uses the OSMnx Python library directly with the configured Overpass.
Хэш пароля шлюза — `.secrets/langgraph.htpasswd`.
Эти файлы исключены из Git и Docker build context.

### Сертификат и сеть

Для IP выпущен доверенный Let's Encrypt сертификат профиля `shortlived`.
Он хранится в `/etc/letsencrypt/live/langgraph-ip/` и проверяется на продление
дважды в сутки через `langgraph-cert-renew.timer`. После проверки Nginx
перезагружает сертификат. Проверка `certbot renew --dry-run` прошла успешно.

```bash
systemctl list-timers langgraph-cert-renew.timer
sudo journalctl -u langgraph-cert-renew.service
```

В security group добавлены:

- TCP 8443 с текущих клиентских адресов, как для остальных API;
- TCP 80 для HTTP-01 проверки Let's Encrypt. На этом порту слушает только
  временный standalone-сервер Certbot при выпуске/продлении сертификата.

При изменении публичного IP VM понадобится перевыпустить сертификат,
обновить адреса в конфигурации и Studio. Сертификат выпускается для IP,
домен для этого не нужен:
https://letsencrypt.org/2026/03/11/shorter-certs-certbot/

### Текущая доступность LLM

На 26 октября 2026 запросы с VM к OpenRouter возвращают
`403 Access denied by security policy`, включая публичный `/api/v1/models`.
С локального компьютера этот endpoint отвечает 200. LangGraph API и пароль
работают, но живой запуск агента останавливается на обращении к LLM.
Нужен доступный с VM LLM endpoint либо разрешение запросов у провайдера.
После изменения `.env` пересоздайте контейнер `langgraph`:

```bash
docker compose -f docker-compose.yml -f deploy/compose.langgraph.yml up -d --force-recreate langgraph
```
