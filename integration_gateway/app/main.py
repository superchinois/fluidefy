from fastapi import FastAPI
from .api.routes import router
from .database import Base, engine

# Ensure infrastructure staging configurations run migrations
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Fluidefy to Odoo ERP Integration Ingestion Microservice Gateway",
    version="1.0.0",
)

app.include_router(router)
