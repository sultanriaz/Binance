.PHONY: setup test lint up down logs status bootstrap demo paper-smoke history-load
setup:
	cp -n .env.example .env || true
	@echo "Edit .env and replace JWT_SECRET with a secure random value."
test:
	python -m pytest -q
lint:
	ruff check src tests
up:
	docker compose up --build -d
down:
	docker compose down
logs:
	docker compose logs -f --tail=100
status:
	docker compose ps
bootstrap:
	docker compose exec -it gateway python -m trader.bootstrap_admin

demo:
	PYTHONPATH=src python -m trader.paper.demo --output data/examples/synthetic_btcusdt_1m.jsonl
paper-smoke:
	docker compose exec gateway python /app/scripts/paper_smoke.py
history-load:
	docker compose exec gateway python -m trader.paper.history load --file /app/data/examples/synthetic_btcusdt_1m.jsonl

# Module 5: evidence and reliability. Staging uses the documented Compose overlay.
.PHONY: fault-drill ops-chain ops-db-verify ops-backup staging-config
fault-drill:
	PYTHONPATH=src python scripts/ops/fault_drill.py
ops-chain:
	docker compose exec gateway python -m trader.operations.verify_chain
ops-db-verify:
	docker compose exec -T postgres psql -U trader -d trader -v ON_ERROR_STOP=1 < scripts/ops/verify_db_runtime.sql
ops-backup:
	./scripts/ops/backup_restore_drill.sh
staging-config:
	docker compose -f docker-compose.yml -f infrastructure/staging/docker-compose.staging.yml --env-file .env.staging config --quiet
