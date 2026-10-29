COMPOSE := docker compose -f docker-compose.yml \
	-f deploy/compose.langgraph.yml \
	-f deploy/compose.transit.yml \
	-f deploy/compose.wikipedia.yml

.PHONY: up build down logs ps seed

# Полный стек: backend (geo_api), Postgres, Overpass, OTP, эмбеддинги и LangGraph/UI.
up:
	$(COMPOSE) up --build -d

build:
	$(COMPOSE) build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --no-log-prefix

ps:
	$(COMPOSE) ps

# Снимок данных базы в docker/initdb/02-tools-data.sql.gz (см. README «Сидирование»).
seed:
	scripts/tools-db.sh dump