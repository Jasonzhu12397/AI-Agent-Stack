COMPOSE=docker compose --env-file .env -f deploy/docker-compose.yml

.PHONY: dev down logs test lint

dev:
	@test -f .env || cp .env.example .env
	$(COMPOSE) up --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f api worker

test:
	python -m pytest

lint:
	ruff check .
