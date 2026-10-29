"""Offline tests for Google Drive sync and write-back (M11.3) over a fake Drive transport."""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import jwt
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import drive
from app.config import Settings
from app.models import BannedImage, Brand, Post, PostStatus, StockImage
from app.notifier.telegram import process_callback, process_message
from app.pipeline import publish
from app.publishers.base import PublishResult

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
_PUBLIC = _KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)
_EMAIL = "cutebot@proj.iam.gserviceaccount.com"
_TOKEN_URI = "https://oauth2.example/token"
_SA = json.dumps(
    {"client_email": _EMAIL, "private_key": _PEM, "private_key_id": "k1", "token_uri": _TOKEN_URI}
)
_ROOT = "folderROOT123"


def _jpeg(color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="JPEG")
    return buf.getvalue()


class FakeDrive:
    """Just enough of Drive v3: list by parent, get, alt=media, create folder, re-parent."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self.ids = (f"f{n}" for n in itertools.count(100))
        self.calls: list[str] = []
        self.add(_ROOT, "Shop photos", drive.FOLDER_MIME, parent="")

    def add(self, fid: str, name: str, mime: str, parent: str, content: bytes = b"") -> None:
        self.files[fid] = {
            "id": fid,
            "name": name,
            "mimeType": mime,
            "parents": [parent] if parent else [],
            "content": content,
            "sha256Checksum": hashlib.sha256(content).hexdigest(),
        }

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(f"{req.method} {req.url.path}")
        if str(req.url).startswith(_TOKEN_URI):
            form = dict(x.split("=", 1) for x in req.content.decode().split("&"))
            claims = jwt.decode(form["assertion"], certs=_PUBLIC, audience=_TOKEN_URI)
            assert claims["iss"] == _EMAIL and "drive" in claims["scope"]
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert req.headers["Authorization"] == "Bearer tok"
        path = req.url.path.removeprefix("/drive/v3/files").strip("/")
        p = req.url.params
        if req.method == "GET" and not path:
            q = p["q"]
            parent = re.search(r"'([\w-]+)' in parents", q)
            name = re.search(r"name = '([^']+)'", q)
            hits = [
                {k: v for k, v in f.items() if k != "content"}
                for f in self.files.values()
                if parent
                and parent.group(1) in f["parents"]
                and ("contains 'image/'" not in q or f["mimeType"].startswith("image/"))
                and (not name or f["name"] == name.group(1))
                and (drive.FOLDER_MIME not in q or f["mimeType"] == drive.FOLDER_MIME)
            ]
            return httpx.Response(200, json={"files": hits})
        f = self.files.get(path)
        if f is None:
            return httpx.Response(404, json={"error": {"message": f"File not found: {path}."}})
        if req.method == "GET" and p.get("alt") == "media":
            return httpx.Response(200, content=f["content"])
        if req.method == "GET":
            return httpx.Response(200, json={k: v for k, v in f.items() if k != "content"})
        if req.method == "PATCH":
            f["parents"] = [x for x in f["parents"] if x not in p["removeParents"].split(",")]
            f["parents"].append(p["addParents"])
            return httpx.Response(200, json={"id": path})
        raise AssertionError(f"unexpected {req.method} {req.url}")

    def create(self, req: httpx.Request) -> httpx.Response | None:
        if req.method == "POST" and req.url.path == "/drive/v3/files":
            meta = json.loads(req.content)
            fid = next(self.ids)
            self.add(fid, meta["name"], meta["mimeType"], meta["parents"][0])
            self.calls.append("POST create")
            return httpx.Response(200, json={"id": fid})
        return None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(lambda r: self.create(r) or self.handler(r))

    def child(self, name: str) -> str | None:
        return next(
            (i for i, f in self.files.items() if f["name"] == name and _ROOT in f["parents"]),
            None,
        )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeDrive:
    fd = FakeDrive()
    for i, color in enumerate([(255, 0, 0), (0, 255, 0), (0, 0, 255)], 1):
        fd.add(f"img{i}", f"photo{i}.jpg", "image/jpeg", _ROOT, _jpeg(color))
    fd.add("sub", "old", drive.FOLDER_MIME, _ROOT)
    fd.add("img9", "nested.jpg", "image/jpeg", "sub", _jpeg((9, 9, 9)))  # not descended
    fd.add("doc1", "notes.txt", "text/plain", _ROOT, b"hi")
    monkeypatch.setattr(drive, "client", lambda s=None: drive.Drive(_settings(), fd.transport()))
    return fd


def _settings(tmp: Path | None = None) -> Settings:
    return Settings(
        google_service_account_json=_SA,
        stock_images_dir=str(tmp or "stock"),
        telegram_chat_id="42",
        telegram_bot_token="t",
    )


async def _linked(session: AsyncSession) -> Brand:
    brand = await session.get(Brand, 1)
    assert brand is not None
    brand.drive_folder_id = _ROOT
    await session.commit()
    return brand


class _Notifier:
    scheduler = None

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.keyboards: list[list | None] = []
        self.toasts: list[str] = []

    def for_brand(self, brand: object) -> _Notifier:
        return self

    async def send_message(self, text: str, *, html: bool = False, keyboard: Any = None) -> None:
        self.messages.append(text)
        self.keyboards.append(keyboard)

    async def answer_callback(self, cid: str, text: str) -> None:
        self.toasts.append(text)

    async def mark_decided(self, *a: Any, **kw: Any) -> None:
        pass


# ─────────────────────────────── link parsing ────────────────────────────────


@pytest.mark.parametrize(
    "text,want",
    [
        ("https://drive.google.com/drive/folders/1AbC-dEf_ghIJkl?usp=sharing", "1AbC-dEf_ghIJkl"),
        ("https://drive.google.com/drive/u/0/folders/1AbCdEfghIJ", "1AbCdEfghIJ"),
        ("https://drive.google.com/open?id=1AbCdEfghIJ", "1AbCdEfghIJ"),
        ("1AbCdEfghIJ", "1AbCdEfghIJ"),
        ("hello", None),
        ("https://example.com/x", None),
    ],
)
def test_parse_folder_id(text: str, want: str | None) -> None:
    assert drive.parse_folder_id(text) == want


# ─────────────────────────────── sync ────────────────────────────────────────


async def test_sync_imports_three_then_zero(
    session: AsyncSession, fake: FakeDrive, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    brand = await _linked(session)
    client = drive.Drive(settings, fake.transport())
    assert await drive.sync(session, brand, settings, client) == 3
    rows = (await session.scalars(select(StockImage).order_by(StockImage.image_ref))).all()
    assert [(r.image_ref, r.source, r.source_id) for r in rows] == [
        ("photo1.jpg", "drive", "img1"),
        ("photo2.jpg", "drive", "img2"),
        ("photo3.jpg", "drive", "img3"),
    ]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["photo1.jpg", "photo2.jpg", "photo3.jpg"]
    assert brand.drive_synced_at is not None

    downloads = sum("GET" in c and c.endswith(("img1", "img2", "img3")) for c in fake.calls)
    assert await drive.sync(session, brand, settings, client) == 0
    again = sum("GET" in c and c.endswith(("img1", "img2", "img3")) for c in fake.calls)
    assert again == downloads  # known Drive ids are not downloaded again


async def test_sync_skips_known_hash_without_downloading(
    session: AsyncSession, fake: FakeDrive, tmp_path: Path
) -> None:
    """A photo already sent via Telegram is not pulled again from Drive."""
    settings = _settings(tmp_path)
    brand = await _linked(session)
    sha = fake.files["img1"]["sha256Checksum"]
    session.add(StockImage(brand_id=1, image_ref="tg.jpg", source="telegram", sha256=sha))
    await session.commit()
    assert await drive.sync(session, brand, settings, drive.Drive(settings, fake.transport())) == 2
    assert not any(c.endswith("/img1") for c in fake.calls)


async def test_sync_does_not_reimport_a_deleted_photo(
    session: AsyncSession, fake: FakeDrive, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    brand = await _linked(session)
    client = drive.Drive(settings, fake.transport())
    await drive.sync(session, brand, settings, client)
    from app import stock

    assert await stock.delete_image(session, brand, settings, "photo1.jpg") is None
    await session.commit()
    assert await drive.sync(session, brand, settings, client) == 0


async def test_sync_survives_one_bad_file(
    session: AsyncSession, fake: FakeDrive, tmp_path: Path
) -> None:
    fake.files["img2"]["content"] = b"not an image"
    fake.files["img2"]["name"] = "broken.heic"
    fake.files["img2"]["mimeType"] = "image/heic"
    settings = _settings(tmp_path)
    brand = await _linked(session)
    assert await drive.sync(session, brand, settings, drive.Drive(settings, fake.transport())) == 2
    assert not (tmp_path / "broken.heic").exists()


# ─────────────────────────────── write-back ──────────────────────────────────


class _OkPublisher:
    name = "stub"

    async def publish(self, session: AsyncSession, post: Post) -> PublishResult:
        return PublishResult(network="stub", ok=True)


def _post(ref: str, status: PostStatus) -> Post:
    return Post(
        brand_id=1, image_ref=ref, caption_he="ש", caption_en="e", visual_concept="v", status=status
    )


async def test_publish_moves_drive_file_to_posted(
    session: AsyncSession, fake: FakeDrive, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    brand = await _linked(session)
    await drive.sync(session, brand, settings, drive.Drive(settings, fake.transport()))
    post = _post("photo1.jpg", PostStatus.APPROVED)
    session.add(post)
    await session.commit()

    done = await publish.publish_by_id(session, 1, post.id, publishers=[_OkPublisher()])
    assert done is not None and done.status == PostStatus.PUBLISHED
    posted = fake.child(drive.POSTED)
    assert posted is not None
    assert fake.files["img1"]["parents"] == [posted]
    assert fake.files["img2"]["parents"] == [_ROOT]


async def test_ban_moves_drive_file_to_banned(
    session: AsyncSession, fake: FakeDrive, tmp_path: Path
) -> None:
    """🚫 Ban image chip on a card → the Drive file goes to banned/ (created on first use)."""
    settings = _settings(tmp_path)
    brand = await _linked(session)
    await drive.sync(session, brand, settings, drive.Drive(settings, fake.transport()))
    post = _post("photo2.jpg", PostStatus.SUGGESTED)
    session.add(post)
    await session.commit()
    cb = {
        "id": "cq",
        "data": f"reason:{post.id}:ban_image",
        "message": {"message_id": 5, "chat": {"id": 42}, "text": "card"},
    }
    await process_callback(session, _Notifier(), cb, settings)  # type: ignore[arg-type]
    assert await session.get(BannedImage, (1, "photo2.jpg")) is not None
    banned = fake.child(drive.BANNED)
    assert banned is not None and fake.files["img2"]["parents"] == [banned]


async def test_posted_then_banned_ends_in_banned_only(
    session: AsyncSession, fake: FakeDrive, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    brand = await _linked(session)
    await drive.sync(session, brand, settings, drive.Drive(settings, fake.transport()))
    await drive.write_back(session, 1, "photo3.jpg", drive.POSTED)
    await drive.write_back(session, 1, "photo3.jpg", drive.BANNED)
    assert fake.files["img3"]["parents"] == [fake.child(drive.BANNED)]
    created = [c for c in fake.calls if c == "POST create"]
    assert len(created) == 2  # posted/ and banned/, each once


async def test_write_back_skips_non_drive_image_silently(
    session: AsyncSession, fake: FakeDrive
) -> None:
    await _linked(session)
    session.add(StockImage(brand_id=1, image_ref="tg.jpg", source="telegram", sha256="x"))
    await session.commit()
    await drive.write_back(session, 1, "tg.jpg", drive.BANNED)
    await drive.write_back(session, 1, "nothing.jpg", drive.POSTED)
    assert fake.calls == []


async def test_write_back_never_raises(
    session: AsyncSession, fake: FakeDrive, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    brand = await _linked(session)
    await drive.sync(session, brand, settings, drive.Drive(settings, fake.transport()))
    del fake.files["img1"]  # the owner deleted it in Drive
    await drive.write_back(session, 1, "photo1.jpg", drive.POSTED)  # logged, not raised


# ─────────────────────────────── /drive and /sync ────────────────────────────


async def _say(session: AsyncSession, text: str, settings: Settings) -> _Notifier:
    n = _Notifier()
    await process_message(session, n, {"chat": {"id": 42}, "text": text}, settings)  # type: ignore[arg-type]
    return n


async def test_drive_off_when_no_service_account(session: AsyncSession) -> None:
    off = Settings(google_service_account_json="")
    assert "not set up" in (await _say(session, "/drive", off)).messages[0]
    assert "not set up" in (await _say(session, "/sync", off)).messages[0]


async def test_drive_prints_the_service_account_address(
    session: AsyncSession, fake: FakeDrive
) -> None:
    n = await _say(session, "/drive", _settings())
    assert _EMAIL in n.messages[0]


async def test_drive_unseen_folder_fails_clearly(session: AsyncSession, fake: FakeDrive) -> None:
    n = await _say(
        session, "/drive https://drive.google.com/drive/folders/1NotSharedXyz", _settings()
    )
    assert "can't see that folder" in n.messages[0] and _EMAIL in n.messages[0]
    brand = await session.get(Brand, 1)
    assert brand is not None and brand.drive_folder_id is None


async def test_drive_rejects_a_file_link(session: AsyncSession, fake: FakeDrive) -> None:
    n = await _say(session, "/drive https://drive.google.com/drive/folders/img1xxxxx", _settings())
    assert "can't see" in n.messages[0]  # unknown id
    fake.add("imgfile12345", "a.jpg", "image/jpeg", _ROOT, b"x")
    n = await _say(session, "/drive imgfile12345", _settings())
    assert "not a folder" in n.messages[0]


async def test_drive_link_then_sync_and_stock_button(
    session: AsyncSession, fake: FakeDrive, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    n = await _say(session, f"/drive https://drive.google.com/drive/folders/{_ROOT}", settings)
    assert "Shop photos" in n.messages[0] and "3 photo(s)" in n.messages[0]
    brand = await session.get(Brand, 1)
    assert brand is not None and brand.drive_folder_id == _ROOT

    n = await _say(session, "/sync", settings)
    assert n.messages == ["Drive sync: 3 new photos imported."]

    n = await _say(session, "/stock", settings)
    datas = [b["callback_data"] for row in n.keyboards[0] or [] for b in row]
    assert "con:sync" in datas


async def test_stock_menu_has_no_sync_button_without_a_folder(session: AsyncSession) -> None:
    n = await _say(session, "/stock", Settings(google_service_account_json=""))
    datas = [b["callback_data"] for row in n.keyboards[0] or [] for b in row]
    assert "con:sync" not in datas


async def test_sync_without_folder_says_how(session: AsyncSession, fake: FakeDrive) -> None:
    n = await _say(session, "/sync", _settings())
    assert "/drive" in n.messages[0]

