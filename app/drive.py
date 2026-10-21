"""Google Drive folder sync and write-back (M11.3) — see PRODUCT_SPEC §3.

One service account for every brand (``GOOGLE_SERVICE_ACCOUNT_JSON``; blank = Drive off).
A brand shares a folder with the account's address and registers it with ``/drive``; sync
pulls the folder's images (not its sub-folders) into the stock library, and write-back
moves a published image's Drive file into ``posted/`` and a banned one into ``banned/``.
Moves change the file's parents only, so the owner can drag a file back.

Drive REST v3 through ``httpx``; ``google-auth`` only signs the service-account JWT.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from google.auth import crypt, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import stock
from app.config import Settings, get_settings
from app.models import Brand, StockImage

logger = logging.getLogger(__name__)

_API = "https://www.googleapis.com/drive/v3/files"
_TOKEN_URI = "https://oauth2.googleapis.com/token"
# Full drive scope: drive.file only reaches files the app created, and the owner's photos
# were not. What the account can see is still only what a brand shares with it.
_SCOPE = "https://www.googleapis.com/auth/drive"
FOLDER_MIME = "application/vnd.google-apps.folder"
POSTED, BANNED = "posted", "banned"
_ALL_DRIVES = {"supportsAllDrives": "true"}
_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}


class DriveError(Exception):
    """A Drive call failed. ``status`` is the HTTP status (0 = no response)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def parse_folder_id(text: str) -> str | None:
    """A folder link (``…/folders/<id>``, ``…?id=<id>``) or a bare id → the id; else None."""
    text = text.strip()
    m = re.search(r"/folders/([\w-]+)", text) or re.search(r"[?&]id=([\w-]+)", text)
    if m:
        return m.group(1)
    return text if re.fullmatch(r"[\w-]{10,}", text) else None


def service_account_email(settings: Settings) -> str | None:
    if not settings.google_service_account_json:
        return None
    return str(json.loads(settings.google_service_account_json)["client_email"])


class Drive:
    """A Drive v3 client for the service account. ``transport`` is for tests."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self._info: dict[str, Any] = json.loads(settings.google_service_account_json)
        self.email = str(self._info["client_email"])
        self._transport = transport
        self._token: tuple[str, float] | None = None  # (access token, expiry epoch)

    def _client(self, timeout: float = 30) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=timeout)

    async def _access_token(self) -> str:
        # ponytail: token cached per client; clients live for one sync or one write-back.
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        token_uri = self._info.get("token_uri") or _TOKEN_URI
        now = int(time.time())
        claims = {"iss": self.email, "scope": _SCOPE, "aud": token_uri, "iat": now}
        signer = crypt.RSASigner.from_service_account_info(self._info)
        assertion = jwt.encode(signer, {**claims, "exp": now + 3600})
        async with self._client() as client:
            resp = await client.post(
                token_uri,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion.decode(),
                },
            )
        if resp.status_code != 200:
            raise DriveError(resp.status_code, f"service-account token refused: {resp.text[:200]}")
        body = resp.json()
        self._token = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
        return self._token[0]

    async def _call(self, method: str, url: str, **kw: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {await self._access_token()}"}
        async with self._client(timeout=120) as client:
            resp = await client.request(method, url, headers=headers, **kw)
        if resp.status_code >= 400:
            try:
                msg = resp.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                msg = resp.text[:200]
            raise DriveError(resp.status_code, msg)
        return resp

    async def get(self, file_id: str, fields: str = "id,name,mimeType,parents") -> dict[str, Any]:
        resp = await self._call(
            "GET", f"{_API}/{file_id}", params={"fields": fields, **_ALL_DRIVES}
        )
        return dict(resp.json())

    async def _list(self, q: str, fields: str) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        page: str | None = None
        while True:
            params = {
                "q": q,
                "fields": f"nextPageToken,files({fields})",
                "pageSize": "1000",
                "includeItemsFromAllDrives": "true",
                **_ALL_DRIVES,
            }
            if page:
                params["pageToken"] = page
            body = (await self._call("GET", _API, params=params)).json()
            files += body.get("files", [])
            page = body.get("nextPageToken")
            if not page:
                return files

    async def list_images(self, folder_id: str) -> list[dict[str, Any]]:
        """Image files directly in the folder (sub-folders are not descended), name order."""
        q = f"'{folder_id}' in parents and mimeType contains 'image/' and trashed = false"
        files = await self._list(q, "id,name,mimeType,sha256Checksum")
        return sorted(files, key=lambda f: f.get("name", ""))

    async def download(self, file_id: str, dest: Path) -> None:
        resp = await self._call("GET", f"{_API}/{file_id}", params={"alt": "media", **_ALL_DRIVES})
        dest.write_bytes(resp.content)

    async def ensure_subfolder(self, folder_id: str, name: str) -> str:
        """The id of ``name`` inside ``folder_id``, created on first use."""
        q = (
            f"'{folder_id}' in parents and name = '{name}' "
            f"and mimeType = '{FOLDER_MIME}' and trashed = false"
        )
        found = await self._list(q, "id")
        if found:
            return str(found[0]["id"])
        meta = {"name": name, "mimeType": FOLDER_MIME, "parents": [folder_id]}
        resp = await self._call("POST", _API, params={"fields": "id", **_ALL_DRIVES}, json=meta)
        return str(resp.json()["id"])

    async def move(self, file_id: str, to_parent: str) -> None:
        """Re-parent the file into ``to_parent`` (wherever it is now). No-op if already there."""
        # ponytail: reads the current parents instead of taking from_parent — a file can be
        # banned after it was posted, and then it sits in posted/, not the root folder.
        parents = (await self.get(file_id, "parents")).get("parents", [])
        if parents == [to_parent]:
            return
        params = {"addParents": to_parent, "removeParents": ",".join(parents), **_ALL_DRIVES}
        await self._call("PATCH", f"{_API}/{file_id}", params=params, json={})


def client(settings: Settings | None = None) -> Drive | None:
    """The Drive client, or None when ``GOOGLE_SERVICE_ACCOUNT_JSON`` is blank."""
    settings = settings or get_settings()
    return Drive(settings) if settings.google_service_account_json else None


async def sync(session: AsyncSession, brand: Brand, settings: Settings, drive: Drive) -> int:
    """Import the brand folder's new images. Returns how many were added. Commits.

    A file is skipped when the brand already has a row for its Drive id or its sha256 —
    rows deleted in the browser included, so a deleted photo is not pulled back nightly.
    One bad file is logged and skipped; it does not stop the rest.
    """
    brand = await session.get(Brand, brand.id) or brand  # the scheduler's copy is detached
    if not brand.drive_folder_id:
        return 0
    files = await drive.list_images(brand.drive_folder_id)
    rows = (
        await session.execute(
            select(StockImage.source_id, StockImage.sha256).where(StockImage.brand_id == brand.id)
        )
    ).all()
    known_ids = {sid for sid, _ in rows if sid}
    known_sha = {sha for _, sha in rows}
    stock_dir = stock.brand_dir(settings, brand.stock_prefix)
    stock_dir.mkdir(parents=True, exist_ok=True)
    added = 0
    for f in files:
        if f["id"] in known_ids or f.get("sha256Checksum") in known_sha:
            continue
        name = f.get("name") or f["id"]
        ext = Path(name).suffix.lower()
        if ext not in stock.IMPORTABLE:
            ext = _MIME_EXT.get(f.get("mimeType", ""), "")
            if not ext:
                continue  # gif, svg, raw…: not something the pipeline can post
            name = Path(name).stem + ext
        dest = stock.unique_path(stock_dir, name)
        try:
            await drive.download(f["id"], dest)
            row = await stock.register(session, brand, dest, "drive", f["id"])
        except Exception:  # noqa: BLE001 — one bad file must not stop the sync
            logger.exception("Drive sync: could not import %s (%s).", name, f["id"])
            dest.unlink(missing_ok=True)
            continue
        await session.commit()
        if row is not None:
            added += 1
            known_sha.add(row.sha256)
    brand.drive_synced_at = datetime.now(UTC)
    await session.commit()
    logger.info("Drive sync brand %s: %d new of %d listed.", brand.id, added, len(files))
    return added


async def write_back(session: AsyncSession, brand_id: int, image_ref: str, subfolder: str) -> None:
    """Move the image's Drive file into ``posted/`` or ``banned/``. Best effort, never raises.

    Skipped silently when the image did not come from Drive, the brand has no folder, or
    Drive is off.
    """
    try:
        row = await session.get(StockImage, (brand_id, image_ref))
        if row is None or row.source != "drive" or not row.source_id:
            return
        brand = await session.get(Brand, brand_id)
        drive = client()
        if brand is None or not brand.drive_folder_id or drive is None:
            return
        target = await drive.ensure_subfolder(brand.drive_folder_id, subfolder)
        await drive.move(row.source_id, target)
        logger.info("Drive write-back: %s → %s/.", image_ref, subfolder)
    except Exception:  # noqa: BLE001 — publish/ban already happened; Drive is a courtesy
        logger.exception("Drive write-back to %s/ failed for %s.", subfolder, image_ref)
