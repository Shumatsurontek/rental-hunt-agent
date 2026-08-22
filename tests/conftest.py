from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from rental_hunt.contracts import NormalizedListing, listing_fingerprint
from rental_hunt.models import Base


@pytest.fixture
def listing_factory() -> Callable[..., NormalizedListing]:
    def build(**overrides: Any) -> NormalizedListing:
        values: dict[str, Any] = {
            "source": "seloger",
            "source_listing_id": "123456789",
            "canonical_url": "https://www.seloger.com/annonces/123456789",
            "title": "Appartement 2 pièces",
            "description": "Appartement calme et lumineux.",
            "rent_eur_monthly": 1_500,
            "surface_m2": Decimal("45.00"),
            "rooms": 2,
            "bedrooms": 1,
            "furnished": True,
            "postal_code": "75011",
            "city": "Paris",
            "photo_urls": ("https://img.seloger.com/one.jpg",),
            "published_at": datetime(2026, 8, 20, 12, tzinfo=UTC),
            "observed_at": datetime(2026, 8, 21, 12, tzinfo=UTC),
            "data_warnings": (),
        }
        values.update(overrides)
        values["fingerprint"] = listing_fingerprint(values)
        return NormalizedListing.model_validate(values)

    return build


@pytest_asyncio.fixture
async def database(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    postgres_url = os.environ.get("TEST_DATABASE_URL")
    if postgres_url is None:
        database_path = tmp_path / "test.sqlite3"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{database_path}",
            connect_args={"timeout": 30},
        )
    else:
        if not postgres_url.startswith("postgresql+psycopg://"):
            raise AssertionError("TEST_DATABASE_URL must use postgresql+psycopg://")
        engine = create_async_engine(
            postgres_url,
            connect_args={"connect_timeout": 10},
            pool_size=5,
            max_overflow=5,
            pool_timeout=10,
        )
    async with engine.begin() as connection:
        if postgres_url is not None:
            await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    yield engine, factory
    if postgres_url is not None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()
