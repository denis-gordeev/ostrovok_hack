#!/usr/bin/env bash
# Postgres для tools агента в Docker со снимком данных из локальной базы.
#
#   scripts/tools-db.sh dump [--from DSN] [--with-raw]
#       снимок локальной базы → docker/initdb/02-tools-data.sql.gz
#       (DSN по умолчанию postgresql://ostrovok:ostrovok@localhost:5433/ostrovok)
#   scripts/tools-db.sh up [--reset]
#       поднять сервис ostrovok-db из docker-compose.yml и залить в него снимок
#
# В снимке схемы ostrovok (снимок островка для search_hotels_in_tatarstan) и kudago (события для
# events_in_kazan); --with-raw добавляет raw — сырые ответы островка, они нужны только сборщику.
# На новом томе Postgres сам выполняет снимок из docker/initdb при первом старте. На уже существующем
# up заливает снимок, только если в ostrovok.hotels нет строк; --reset заменяет схемы снимка поверх
# имеющихся одной транзакцией (при ошибке прежние данные остаются).
# Порт на хосте — OSTROVOK_DB_PORT (по умолчанию 5433). Нужен только Docker: pg_dump и psql берутся из образа.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SNAPSHOT="$ROOT/docker/initdb/02-tools-data.sql.gz"
IMAGE=postgres:17
SERVICE=ostrovok-db
DB_USER=ostrovok
DB_NAME=ostrovok
PORT="${OSTROVOK_DB_PORT:-5433}"

die() { echo "tools-db: $*" >&2; exit 1; }
compose() { docker compose --project-directory "$ROOT" -f "$ROOT/docker-compose.yml" "$@"; }
psql_db() { compose exec -T "$SERVICE" psql -X -q -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" "$@"; }

cmd_dump() {
    local dsn="postgresql://ostrovok:ostrovok@localhost:5433/ostrovok"
    local schemas="ostrovok kudago"
    while (($#)); do
        case "$1" in
            --from) dsn="${2:?--from: нужен DSN}"; shift 2 ;;
            --with-raw) schemas="$schemas raw"; shift ;;
            *) die "dump: неизвестный аргумент $1" ;;
        esac
    done
    local args=() schema
    for schema in $schemas; do args+=(-n "$schema"); done
    # pg_dump запускается в контейнере, поэтому localhost хоста для него — host.docker.internal.
    local container_dsn
    container_dsn="$(sed -E 's#@(localhost|127\.0\.0\.1)([:/]|$)#@host.docker.internal\2#' <<<"$dsn")"
    trap 'rm -f "$SNAPSHOT.tmp"' EXIT
    echo "Снимаю схемы $schemas …"
    # --clean --if-exists: снимок сам удаляет свои схемы перед созданием, поэтому годится и для --reset.
    docker run --rm --add-host=host.docker.internal:host-gateway "$IMAGE" \
        pg_dump --dbname="$container_dsn" "${args[@]}" --no-owner --no-privileges --clean --if-exists \
        | gzip -6 >"$SNAPSHOT.tmp"
    mv "$SNAPSHOT.tmp" "$SNAPSHOT"
    echo "Готово: ${SNAPSHOT#"$ROOT"/} ($(du -h "$SNAPSHOT" | cut -f1))"
}

cmd_up() {
    local reset=0
    while (($#)); do
        case "$1" in
            --reset) reset=1; shift ;;
            *) die "up: неизвестный аргумент $1" ;;
        esac
    done
    [[ -f "$SNAPSHOT" ]] || die "нет снимка ${SNAPSHOT#"$ROOT"/} — сначала scripts/tools-db.sh dump"
    if [[ -z "$(compose ps -q --status running "$SERVICE")" ]]; then
        local busy
        busy="$(docker ps --filter "publish=$PORT" --format '{{.Names}}' | paste -sd ' ' -)"
        [[ -z "$busy" ]] || die "порт $PORT занят контейнером $busy: остановите его (docker stop $busy)" \
            "или поднимите базу на другом порту: OSTROVOK_DB_PORT=5434 $0 up"
    fi
    # Healthcheck идёт по TCP, поэтому --wait ждёт и конца заливки docker/initdb на новом томе.
    compose up -d --wait "$SERVICE"

    local hotels=0
    if [[ "$(psql_db -Atc "SELECT to_regclass('ostrovok.hotels') IS NOT NULL")" == t ]]; then
        hotels="$(psql_db -Atc "SELECT count(*) FROM ostrovok.hotels")"
    fi
    if ((reset)) || ((hotels == 0)); then
        echo "Заливаю снимок…"
        gunzip -c "$SNAPSHOT" | psql_db --single-transaction >/dev/null
    else
        echo "В базе уже $hotels отелей — снимок не заливаю (перезалить: $0 up --reset)"
    fi

    psql_db -c "
        SELECT table_schema AS schema, table_name AS table,
               (xpath('/row/n/text()', query_to_xml(
                   format('SELECT count(*) AS n FROM %I.%I', table_schema, table_name), false, true, '')))[1]::text::bigint
                   AS rows
        FROM information_schema.tables
        WHERE table_schema IN ('ostrovok', 'kudago', 'raw')
        ORDER BY 1, 2"
    echo "DSN для .env: postgresql://$DB_USER:$DB_USER@localhost:$PORT/$DB_NAME (OSTROVOK_DB_DSN, KUDAGO_DB_DSN)"
}

case "${1:-}" in
    dump) shift; cmd_dump "$@" ;;
    up) shift; cmd_up "$@" ;;
    *) sed -n '2,15s/^# \{0,1\}//p' "$0"; exit 2 ;;
esac
