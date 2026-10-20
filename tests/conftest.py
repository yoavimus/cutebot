"""Test fixtures — an isolated in-memory SQLite DB per test. No network, no Postgres.

Every DB starts with brand 1 (the default tenant), owned by chat ``OWNER_CHAT``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base, make_engine, make_sessionmaker
from app.models import Brand

OWNER_CHAT = "42"


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = make_sessionmaker(engine)
    async with sessionmaker() as s:
        s.add(Brand(id=1, name="default", telegram_chat_id=OWNER_CHAT, brand_text="test brand"))
        await s.commit()
        yield s
    await engine.dispose()
