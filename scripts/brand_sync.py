"""Sync the default brand's text between ``BRAND_FILE`` and the database (M10.2).

The DB row is the source of truth; ``brand.md`` only seeds it. Use this when you edit the
file by hand (``push``) or want the live text back as a file (``pull``).

    python -m scripts.brand_sync push   # BRAND_FILE -> row (old text kept in brand_text_prev)
    python -m scripts.brand_sync pull   # row -> BRAND_FILE (byte-for-byte)
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app import brands
from app.config import Settings, get_settings
from app.db import make_engine, make_sessionmaker


async def push(session: AsyncSession, settings: Settings) -> str:
    brand = await brands.default(session)
    new = Path(settings.brand_file).read_bytes().decode("utf-8")
    if new == brand.brand_text:
        return "Row already matches the file."
    brand.brand_text_prev = brand.brand_text
    brand.brand_text = new
    await session.commit()
    return f"Pushed {settings.brand_file} to brand {brand.id} (previous kept)."


async def pull(session: AsyncSession, settings: Settings) -> str:
    brand = await brands.default(session)
    Path(settings.brand_file).write_bytes(brand.brand_text.encode("utf-8"))
    return f"Pulled brand {brand.id} to {settings.brand_file}."


async def _run(action: str) -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    async with make_sessionmaker(engine)() as session:
        print(await (push if action == "push" else pull)(session, settings))
    await engine.dispose()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=["push", "pull"])
    asyncio.run(_run(ap.parse_args().action))


if __name__ == "__main__":
    main()
