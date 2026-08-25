# Regenerate the xsdata dataclasses from the XSD schemas
.PHONY: models
models:
	python scripts/generate_models.py

# Install dependencies
.PHONY: install
install:
	pip install -r requirements.txt

# Run the FastAPI gateway
.PHONY: api
api:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Run the Celery worker
.PHONY: worker
worker:
	celery -A app.workers.tasks.celery_app worker --loglevel=info

# Run everything with docker compose
.PHONY: up
up:
	docker compose up --build
