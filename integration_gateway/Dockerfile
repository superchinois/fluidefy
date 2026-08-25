FROM python:3.11-slim

# git is required to install the odoo_client dependency from the git URL
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first for better layer caching.
# Build context is the repository root (see docker-compose.yml).
COPY integration_gateway/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code.
COPY integration_gateway/ /app/

# The xsdata dataclasses in app/fluidefy_models/ are gitignored and therefore
# absent from fresh checkouts. Generate them at build time from the XSD schemas.
# generate_models.py computes the XSD path as ROOT.parent.parent / "XSD", so we
# stage the schemas at /XSD. They are only needed for generation, not at runtime.
COPY XSD/ /XSD/
RUN python scripts/generate_models.py && rm -rf /XSD

EXPOSE 8000

# Default command runs the FastAPI app; the Celery worker is started via
# a separate service in docker-compose.yml.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
