# Казань: VPS, Overpass, OSMnx и LangGraph

## Текущий стенд

- SSH: `ssh root@188.241.197.121`.
- ОС: Ubuntu 24.04, система видит 2 vCPU, около 13.5 GiB RAM, диск около 80 GiB.
- Код и `.env`: `/opt/ostrovok`; `.env` имеет права 0600.
- Overpass: `http://188.241.197.121:8081/api/interpreter`.
- OSMnx Swagger: `http://188.241.197.121:8080/docs`.
- LangGraph Swagger: `https://188.241.197.121:8443/docs`.
- Studio: https://smith.langchain.com/studio/?baseUrl=https%3A%2F%2F188.241.197.121%3A8443

LangGraph защищён HTTP Basic через доверенный TLS. Логин `denis`, прежний
пароль сохранён локально в `.cache/deploy/langgraph-access.txt` (0600).
Для Studio: Base URL `https://188.241.197.121:8443`, Custom Headers →
`Authorization` со значением `Basic …` из файла доступа. Не вставляйте пароль в URL.

В VPS уже установлена панель хостинга с Nginx, Apache и другими службами.
Они сохранены. Гео API привязаны только к публичному IPv4 через
`API_BIND_ADDRESS=188.241.197.121`, чтобы не занимать Apache `127.0.0.1:8080`.
LangGraph слушает IPv4 8443; внутренний порт 2024 наружу не опубликован.

SSH принимает вход по ключу. Вход по паролю и keyboard-interactive отключён
через `/etc/ssh/sshd_config.d/00-ostrovok-key-only.conf`
(исходник: `deploy/00-ostrovok-key-only.conf`). Настройка загружается раньше
`40-hosting.conf`. Проверены новый вход по ключу и отказ при попытке без ключа.
Пароль HTTPS-шлюза LangGraph действует отдельно и сохранён.

## Управление

```bash
ssh root@188.241.197.121
cd /opt/ostrovok
docker compose -f docker-compose.yml -f deploy/compose.langgraph.yml -f deploy/compose.transit.yml ps
docker compose -f docker-compose.yml -f deploy/compose.langgraph.yml -f deploy/compose.transit.yml logs --tail=100 langgraph app overpass
docker compose -f docker-compose.yml -f deploy/compose.langgraph.yml -f deploy/compose.transit.yml up -d --build
```

Compose автоматически поднимается после перезапуска Docker. Данные хранятся
в `data/overpass-agglomeration-v1/`, исходная вырезка — `data/extract/`, геокэш — `.cache/server/`,
история LangGraph — `.cache/langgraph/`. Эти каталоги не коммитятся.
Это dev-сервер LangGraph для прототипа; одновременно выполняется один запуск.
Трассировка LangSmith отключена; Studio размещён у LangSmith.

В `.env` перенесены локальные LLM-настройки. Host endpoints:
`OVERPASS_URL=http://188.241.197.121:8081/api`,
`OSMNX_API_URL=http://188.241.197.121:8080`.
В контейнерах Compose заменяет их на `http://overpass/api` и `http://app:8080`.
Для поиска объектов агент использует библиотеку OSMnx напрямую.
Инструмент `walking_route` обращается к HTTP `POST /route` через `OSMNX_API_URL`
и возвращает длину, время, смещения до узлов и метаданные без GeoJSON.
Успешные HTTP-ответы кэшируются на диске; геометрия и GraphML — на стороне API.
После изменения `.env` пересоздайте контейнер LangGraph.

## Сеть и сертификат

Неавторизованные гео API 8080/8081 доступны только с клиентских адресов
`46.242.15.146/32` и `23.95.85.12/32`. Правила в Docker DOCKER-USER устанавливает
`deploy/vps-firewall.sh`, systemd unit `ostrovok-firewall.service` запускает
их после старта Docker. При смене клиентского IP обновите список и перезапустите unit.
LangGraph 8443 доступен с паролем. Остальные правила и службы панели не изменялись.

Let's Encrypt сертификат для IP хранится в `/etc/letsencrypt/live/langgraph-ip/`.
HTTP-01 обслуживает хостовый Nginx: `deploy/nginx-acme.conf` установлен в
`/etc/nginx/conf.d/ostrovok-acme.conf`, webroot `/var/www/letsencrypt`.
Порт 80 занят хостовым Nginx, Certbot использует webroot, а не standalone.
Продление проверяется дважды в сутки через `langgraph-cert-renew.timer`;
скрипт `renew-langgraph-cert.sh` после проверки перезагружает Nginx контейнера.

```bash
systemctl status ostrovok-firewall.service
systemctl list-timers langgraph-cert-renew.timer
journalctl -u langgraph-cert-renew.service
```

## Данные и API

Активна вырезка Казанской агломерации из ПФО Geofabrik: west=48.20,
south=55.30, east=49.85, north=56.35. Казань, аэропорт, Иннополис,
Зеленодольск и Свияжск. PBF `data/extract/agglomeration-v1.osm.pbf`,
контрольная сумма `data/extract/agglomeration-v1.SHA256SUMS`.
`osmium check-refs` не нашёл отсутствующих узлов дорог.
Старые городские данные сохранены для отката.
Подробности: [региональные маршруты](../docs/regional-routing.md).
Метаданные, репликация и Overpass areas отключены; запросы через bbox,
координаты и OSM IDs. Данные © OpenStreetMap contributors:
https://www.openstreetmap.org/copyright.

```bash
curl --noproxy '*' --fail --data-urlencode \
 'data=[out:json][timeout:25];nwr(around:500,55.7961,49.1064)[leisure=park];out center tags;' \
 http://188.241.197.121:8081/api/interpreter
curl --noproxy '*' --fail http://188.241.197.121:8080/features \
 -H 'Content-Type: application/json' \
 -d '{"lat":55.7961,"lon":49.1064,"tags":{"leisure":"park"},"radius_m":500}'
curl --noproxy '*' --fail http://188.241.197.121:8080/route \
 -H 'Content-Type: application/json' \
 -d '{"start":{"lat":55.7961,"lon":49.1064},"end":{"lat":55.7905,"lon":49.1144}}'
```

`/features`: радиус до 5 км. `/route`: пеший маршрут, концы не далее 10 км
друг от друга; оба внутри bbox. Смещения до ближайших узлов возвращаются
отдельно и не входят в длину. Время оценивается при 4.5 км/ч. Граф локальный,
доступность для колясок не проверяется. Сырые запросы, результаты и GraphML
кэшируются на диске. Обновление снимка и сброс кэша выполняются вручную.

## Старый стенд

VM Yandex Cloud `ostrovok-kazan` (`fv4f3lurgr9e67va9kl6`) и её SSD 80 GiB
(`fv4mn07pc0q05hhcg1t9`) удалены 26 октября 2026 по запросу пользователя
после проверки нового VPS. Отсутствие VM и диска подтверждено через YC CLI.
Старый стенд больше не доступен для отката. Историческая конфигурация
и идентификаторы — в [YANDEX.md](YANDEX.md).

## Проверка после переноса, 26 октября 2026

Все четыре контейнера запущены. Проверены TLS без обхода валидации,
401 без пароля и с неверным паролем, вход с правильным паролем,
CORS для Studio, регистрация графа и создание треда.
Реальный запуск через LangGraph API вызвал OSM-инструмент и LLM:
ближайший парк — Ново-Кремлевский сквер, 214.5 м, OSM way 99733784.
OSMnx `/features` возвращает данные и повторно читает кэш;
`/route` возвращает 888 м / 11.8 минуты для примера выше.
OpenRouter с нового VPS доступен. Пробное продление сертификата
`certbot renew --dry-run` завершилось успешно.

## Общественный транспорт

Добавлен локальный OpenTripPlanner и `POST /transit/route`; агент использует
инструмент `transit_route`. Данные и GTFS лежат в `data/transit-regional-v1/`.
Промежуточные времена расчётные, календарь модели действует до 25.11.2026.
Инструкция: [../docs/transit.md](../docs/transit.md).
