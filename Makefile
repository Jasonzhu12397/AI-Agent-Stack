COMPOSE=docker compose --env-file .env -f deploy/docker-compose.yml

.PHONY: dev down logs test lint verify version release-notes

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

verify:
	ruff check .
	python -m pytest -q
	cd apps/web-studio && npm test && npm run build

version:
	@python -c "from packages.version import project_version; print(project_version())"
