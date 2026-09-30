"""Application boundary for atomic publication commands."""

from sqlalchemy import Engine

from app.modules.catalog.adapters.postgres_repository import PostgresCatalogRepository


class CatalogPublisher(PostgresCatalogRepository):
    """Catalog-owned publication service used by trusted command entrypoints."""

    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)
