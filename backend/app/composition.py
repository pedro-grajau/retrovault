"""Application composition helpers for command entrypoints."""

from sqlalchemy import Engine

from app.modules.catalog.adapters.postgres_repository import PostgresCatalogRepository
from app.modules.catalog.application.publisher import CatalogPublisher


def build_catalog_publisher(engine: Engine) -> CatalogPublisher:
    return CatalogPublisher(PostgresCatalogRepository(engine))
