"""M8.4/M10.2 — /distill: propose a diff of the brand row text, write only on ✅ Apply; plus
the /brand command and scripts/brand_sync (offline)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app import brands, distill, llm
from app.config import Settings
from app.models import Post, PostStatus
from app.notifier.telegram import process_callback, process_message
from app.schemas import BrandProposal
from scripts import brand_sync

_OWNER = 42
_OLD = "# Brand\n\n## Voice\nPlayful.\n"
_NEW = "# Brand\n\n## Voice\nPlayful, never preachy.\n"


class _Notifier:
    def for_brand(self, brand: object) -> _Notifier:
        return self

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.proposals: list[distill.Proposal] = []
        self.marked: list[str] = []
        self.toasts: list[str] = []

    async def send_message(self, text: str, **kw: object) -> None:
        self.messages.append(text)

    async def send_distill(self, proposal: distill.Proposal) -> None:
        self.proposals.append(proposal)

    async def mark_distilled(self, cb_message: dict, label: str) -> None:
        self.marked.append(label)

    async def answer_callback(self, callback_query_id: str, text: str) -> None:
        self.toasts.append(text)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    brand = tmp_path / "brand.md"
    brand.write_text("file seed, ignored once the row has text\n", encoding="utf-8")
    s = Settings(telegram_chat_id=str(_OWNER), brand_file=str(brand))
    distill._PENDING.clear()
    return s


@pytest.fixture
def model_returns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the LLM; `calls` records that it was (not) reached."""
    calls: list[str] = []

    async def fake(brand: str, evidence: str, settings: Settings) -> BrandProposal:
        calls.append(evidence)
        return BrandProposal(new_brand=_NEW, rationale="because")

    monkeypatch.setattr(llm, "distill_brand", fake)
    return calls


async def _seed(session: AsyncSession) -> None:
    (await brands.default(session)).brand_text = _OLD
    session.add(
        Post(
            brand_id=1,
            image_ref="1.jpg",
            caption_he="אושר",
            caption_en="ok",
            visual_concept="v",
            status=PostStatus.APPROVED,
        )
    )
    await session.commit()


def _cb(data: str, chat: int = _OWNER) -> dict:
    return {
        "id": "c",
        "data": data,
        "message": {"message_id": 5, "chat": {"id": chat}, "text": "p"},
    }


async def test_nothing_to_learn_skips_the_model(
    session: AsyncSession, settings: Settings, model_returns: list[str]
) -> None:
    result = await distill.propose(session, settings, await brands.default(session))
    assert isinstance(result, str) and "Nothing to learn" in result
    assert model_returns == []


async def test_apply_writes_row_and_keeps_previous_only_on_tap(
    session: AsyncSession, settings: Settings, model_returns: list[str]
) -> None:
    await _seed(session)
    n = _Notifier()
    await process_message(session, n, {"chat": {"id": _OWNER}, "text": "/distill"}, settings)
    (proposal,) = n.proposals
    assert "+Playful, never preachy." in proposal.diff
    brand = await brands.default(session)
    assert brand.brand_text == _OLD  # nothing yet
    assert "approved_examples" in model_returns[0]  # the evidence is the learning context

    await process_callback(session, n, _cb(f"distill:apply:{proposal.token}"), settings)
    await session.refresh(brand)
    assert (brand.brand_text, brand.brand_text_prev) == (_NEW, _OLD)
    assert not Path(settings.brand_file + ".bak").exists()
    assert n.marked[0].startswith("✅ Applied")


async def test_discard_is_a_noop(
    session: AsyncSession, settings: Settings, model_returns: list[str]
) -> None:
    await _seed(session)
    n = _Notifier()
    await process_message(session, n, {"chat": {"id": _OWNER}, "text": "/distill"}, settings)
    token = n.proposals[0].token
    await process_callback(session, n, _cb(f"distill:discard:{token}"), settings)
    await process_callback(session, n, _cb(f"distill:apply:{token}"), settings)  # now expired
    brand = await brands.default(session)
    assert (brand.brand_text, brand.brand_text_prev) == (_OLD, None)
    assert n.marked[0].startswith("❌ Discarded") and "expired" in n.marked[1]


async def test_non_owner_cannot_apply_or_trigger(
    session: AsyncSession, settings: Settings, model_returns: list[str]
) -> None:
    await _seed(session)
    n = _Notifier()
    await process_message(session, n, {"chat": {"id": _OWNER}, "text": "/distill"}, settings)
    token = n.proposals[0].token
    await process_callback(session, n, _cb(f"distill:apply:{token}", chat=999), settings)
    await process_message(session, n, {"chat": {"id": 999}, "text": "/distill"}, settings)
    assert (await brands.default(session)).brand_text == _OLD
    assert len(n.proposals) == 1 and n.marked == []


async def test_apply_refuses_when_brand_changed_since_proposal(
    session: AsyncSession, settings: Settings, model_returns: list[str]
) -> None:
    await _seed(session)
    result = await distill.propose(session, settings, await brands.default(session))
    assert isinstance(result, distill.Proposal)
    brand = await brands.default(session)
    brand.brand_text = "edited by owner\n"
    assert "changed since" in await distill.apply(session, brand, result.token)
    assert brand.brand_text == "edited by owner\n" and brand.brand_text_prev is None


async def test_unchanged_brand_proposes_nothing(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(session)

    async def same(brand: str, evidence: str, s: Settings) -> BrandProposal:
        return BrandProposal(new_brand=brand, rationale="all good")

    monkeypatch.setattr(llm, "distill_brand", same)
    result = await distill.propose(session, settings, await brands.default(session))
    assert isinstance(result, str) and "No changes" in result and distill._PENDING == {}


async def test_brand_command_shows_the_row_text(session: AsyncSession, settings: Settings) -> None:
    await _seed(session)
    n = _Notifier()
    await process_message(session, n, {"chat": {"id": _OWNER}, "text": "/brand"}, settings)
    assert "Playful." in n.messages[0] and "<pre>" in n.messages[0]


async def test_brand_sync_push_and_pull_round_trip(
    session: AsyncSession, settings: Settings
) -> None:
    await _seed(session)
    brand = await brands.default(session)
    out = Path(settings.brand_file)
    await brand_sync.pull(session, settings)
    assert out.read_bytes() == _OLD.encode()  # byte-for-byte

    out.write_bytes("# Brand\r\n\r\nedited ✓\r\n".encode())
    await brand_sync.push(session, settings)
    await session.refresh(brand)
    assert (brand.brand_text, brand.brand_text_prev) == ("# Brand\r\n\r\nedited ✓\r\n", _OLD)
    assert "already matches" in await brand_sync.push(session, settings)
