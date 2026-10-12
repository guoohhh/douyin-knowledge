"""Multi-asset reconciliation: several assets of the same kind may coexist.

A real image album maps to many `CapturedMedia(kind="image")` on one source. The
`SourceAsset` identity model is `(source_id, asset_type, remote_url_fingerprint)`,
so that is a legal and expected shape. Reconciliation therefore has to compare the
*whole* captured set against the stored set; retiring same-kind rows one media item
at a time makes each image supersede the one before it.

These tests pin the set semantics and guard the DEC-014 single-video behaviour
against regression.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from douyin_knowledge.capture.models import CapturedCreator, CapturedMedia, CapturedSource
from douyin_knowledge.capture.sync import CaptureSyncService
from douyin_knowledge.db.models.capture import Source, SourceAsset
from douyin_knowledge.media.store import MediaStore, url_fingerprint

IMG_A = "https://cdn.example.com/album/a.jpeg?sig=1"
IMG_B = "https://cdn.example.com/album/b.jpeg?sig=1"
IMG_C = "https://cdn.example.com/album/c.jpeg?sig=1"
IMG_D = "https://cdn.example.com/album/d.jpeg?sig=1"


def _album(*image_urls: str, title: str = "album", external_id: str = "img-1") -> CapturedSource:
    """An image-album-shaped capture: every picture is the same `asset_type`."""
    return CapturedSource(
        platform="douyin",
        external_id=external_id,
        source_type="image_album",
        title=title,
        creator=CapturedCreator(external_creator_id="c1", display_name="c1"),
        media=[
            CapturedMedia(kind="image", url=url, mime_type="image/jpeg") for url in image_urls
        ],
    )


def _video(url: str | None, *, title: str = "vid", external_id: str = "vid-1") -> CapturedSource:
    media = [CapturedMedia(kind="video", url=url, mime_type="video/mp4")] if url else []
    return CapturedSource(
        platform="douyin",
        external_id=external_id,
        source_type="video",
        title=title,
        creator=CapturedCreator(external_creator_id="c1", display_name="c1"),
        media=media,
    )


def _service(session: Session, tmp_path: Path) -> CaptureSyncService:
    return CaptureSyncService(session, media_store=MediaStore(str(tmp_path / "media")))


def _assets(session: Session, external_id: str = "img-1") -> list[SourceAsset]:
    source = session.scalars(select(Source).where(Source.external_id == external_id)).one()
    return list(
        session.scalars(
            select(SourceAsset)
            .where(SourceAsset.source_id == source.id)
            .order_by(SourceAsset.created_at_ms, SourceAsset.id)
        ).all()
    )


def _current(session: Session, external_id: str = "img-1") -> list[SourceAsset]:
    return [
        a
        for a in _assets(session, external_id)
        if a.download_state != SourceAsset.DOWNLOAD_UNAVAILABLE
    ]


def _by_url(assets: list[SourceAsset], url: str) -> SourceAsset | None:
    fp = url_fingerprint(url)
    for a in assets:
        if a.remote_url_fingerprint == fp:
            return a
    return None


# ------------------------------------------------- invariant 1: coexistence


def test_three_simultaneous_images_all_stay_current(session: Session, tmp_path: Path) -> None:
    """A, B, C captured together must all remain current -- none supersedes another."""
    service = _service(session, tmp_path)
    service.sync_sources([_album(IMG_A, IMG_B, IMG_C)])
    session.commit()

    current = _current(session)
    assert len(current) == 3, (
        "all three album images must stay acquirable; "
        f"states were {[a.download_state for a in _assets(session)]}"
    )
    assert all(a.download_state == SourceAsset.DOWNLOAD_PENDING for a in current)
    assert {a.remote_url for a in current} == {IMG_A, IMG_B, IMG_C}


# ------------------------------------------------- invariant 8: idempotency


def test_identical_resync_produces_same_three_current_rows(
    session: Session, tmp_path: Path
) -> None:
    service = _service(session, tmp_path)
    service.sync_sources([_album(IMG_A, IMG_B, IMG_C)])
    session.commit()
    before = {(a.id, a.download_state) for a in _assets(session)}

    service.sync_sources([_album(IMG_A, IMG_B, IMG_C)])
    session.commit()
    session.expire_all()
    after = {(a.id, a.download_state) for a in _assets(session)}

    assert after == before, "an identical capture must not add rows or churn state"
    assert len(_current(session)) == 3


def test_ready_assets_stay_ready_across_unchanged_sync(session: Session, tmp_path: Path) -> None:
    service = _service(session, tmp_path)
    service.sync_sources([_album(IMG_A, IMG_B, IMG_C)])
    session.commit()
    for asset in _assets(session):
        asset.download_state = SourceAsset.DOWNLOAD_READY
        asset.downloaded_at_ms = 1000
    session.commit()

    service.sync_sources([_album(IMG_A, IMG_B, IMG_C)])
    session.commit()
    session.expire_all()

    states = [a.download_state for a in _assets(session)]
    assert states == [SourceAsset.DOWNLOAD_READY] * 3, (
        f"a ready asset must not be re-queued or retired by an unchanged sync; got {states}"
    )


# ------------------------------------------------- invariant 3: one disappears


def test_one_image_disappearing_retires_exactly_that_row(
    session: Session, tmp_path: Path
) -> None:
    """A, B, C -> A, C must retire B only."""
    service = _service(session, tmp_path)
    service.sync_sources([_album(IMG_A, IMG_B, IMG_C)])
    session.commit()
    for asset in _assets(session):
        asset.download_state = SourceAsset.DOWNLOAD_READY
    session.commit()

    service.sync_sources([_album(IMG_A, IMG_C, title="album v2")])
    session.commit()
    session.expire_all()

    all_assets = _assets(session)
    assert len(all_assets) == 3, "the retired row is kept as history, not deleted"
    a, b, c = (_by_url(all_assets, u) for u in (IMG_A, IMG_B, IMG_C))
    assert a is not None and b is not None and c is not None
    assert b.download_state == SourceAsset.DOWNLOAD_UNAVAILABLE, "B disappeared, so B retires"
    assert a.download_state == SourceAsset.DOWNLOAD_READY, "A is still current"
    assert c.download_state == SourceAsset.DOWNLOAD_READY, "C is still current"


# ------------------------------------------------- invariant 4: replacement


def test_replacement_preserves_survivor_retires_gone_creates_new(
    session: Session, tmp_path: Path
) -> None:
    """A, B -> A, D must keep A, retire B, and create D as pending."""
    service = _service(session, tmp_path)
    service.sync_sources([_album(IMG_A, IMG_B)])
    session.commit()
    for asset in _assets(session):
        asset.download_state = SourceAsset.DOWNLOAD_READY
    session.commit()

    service.sync_sources([_album(IMG_A, IMG_D, title="album v2")])
    session.commit()
    session.expire_all()

    all_assets = _assets(session)
    assert len(all_assets) == 3
    a, b, d = (_by_url(all_assets, u) for u in (IMG_A, IMG_B, IMG_D))
    assert a is not None and b is not None and d is not None
    assert a.download_state == SourceAsset.DOWNLOAD_READY, "A survived, keep its bytes"
    assert b.download_state == SourceAsset.DOWNLOAD_UNAVAILABLE
    assert d.download_state == SourceAsset.DOWNLOAD_PENDING


# ------------------------------------------------- invariant 2: re-signing


def test_resigned_urls_keep_same_rows_and_state(session: Session, tmp_path: Path) -> None:
    """Same fingerprint, new signature: refresh remote_url and nothing else."""
    service = _service(session, tmp_path)
    service.sync_sources([_album(IMG_A, IMG_B, IMG_C)])
    session.commit()
    for asset in _assets(session):
        asset.download_state = SourceAsset.DOWNLOAD_READY
    session.commit()
    before = {a.id: (a.download_state, a.storage_key) for a in _assets(session)}

    resigned = [u.replace("sig=1", "sig=999") for u in (IMG_A, IMG_B, IMG_C)]
    service.sync_sources([_album(*resigned, title="album v2")])
    session.commit()
    session.expire_all()

    after = _assets(session)
    assert len(after) == 3, "re-signing is not new content, so no new rows"
    assert {a.id: (a.download_state, a.storage_key) for a in after} == before, (
        "re-signing must not move download_state or storage_key"
    )
    assert {a.remote_url for a in after} == set(resigned), "remote_url is refreshed"


# ------------------------------------------------- invariant 6: in-payload dupes


def test_duplicate_fingerprint_in_one_capture_creates_one_row(
    session: Session, tmp_path: Path
) -> None:
    service = _service(session, tmp_path)
    # Same file listed twice (cover repeated inside the album is the real-world shape),
    # plus a re-signed variant of it, which fingerprints identically.
    service.sync_sources([_album(IMG_A, IMG_A, IMG_A.replace("sig=1", "sig=2"), IMG_B)])
    session.commit()

    assets = _assets(session)
    fingerprints = [a.remote_url_fingerprint for a in assets]
    assert len(fingerprints) == len(set(fingerprints)) == 2, (
        f"one row per unique fingerprint; got {len(assets)} rows"
    )
    assert all(a.download_state == SourceAsset.DOWNLOAD_PENDING for a in assets)


# ------------------------------------------------- invariant 5/7: full loss


def test_all_images_disappearing_retires_every_row(session: Session, tmp_path: Path) -> None:
    service = _service(session, tmp_path)
    service.sync_sources([_album(IMG_A, IMG_B, IMG_C)])
    session.commit()
    for asset in _assets(session):
        asset.download_state = SourceAsset.DOWNLOAD_READY
    session.commit()

    service.sync_sources([_album(title="album emptied")])
    session.commit()
    session.expire_all()

    assets = _assets(session)
    assert len(assets) == 3
    assert all(a.download_state == SourceAsset.DOWNLOAD_UNAVAILABLE for a in assets)


def test_only_the_retired_assets_local_file_is_deleted(session: Session, tmp_path: Path) -> None:
    """Invariant 7: a surviving asset's bytes must not be collateral damage."""
    store = MediaStore(str(tmp_path / "media"))
    service = CaptureSyncService(session, media_store=store)
    service.sync_sources([_album(IMG_A, IMG_B)])
    session.commit()

    files: dict[str, Path] = {}
    for asset in _assets(session):
        path = store.resolve(asset.storage_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"bytes")
        asset.download_state = SourceAsset.DOWNLOAD_READY
        files[asset.remote_url_fingerprint or ""] = path
    session.commit()

    service.sync_sources([_album(IMG_A, title="album v2")])
    session.commit()
    session.expire_all()

    assert files[url_fingerprint(IMG_A)].is_file(), "surviving asset keeps its bytes"
    assert not files[url_fingerprint(IMG_B)].is_file(), "retired asset's stale file is removed"


# ------------------------------------------------- DEC-014 regression guards


def test_single_video_replacement_still_retires_old_upload(
    session: Session, tmp_path: Path
) -> None:
    service = _service(session, tmp_path)
    service.sync_sources([_video("https://cdn.example.com/v/a.mp4")])
    session.commit()
    for asset in _assets(session, "vid-1"):
        asset.download_state = SourceAsset.DOWNLOAD_READY
    session.commit()

    service.sync_sources([_video("https://cdn.example.com/v/b.mp4", title="vid v2")])
    session.commit()
    session.expire_all()

    assets = _assets(session, "vid-1")
    old = _by_url(assets, "https://cdn.example.com/v/a.mp4")
    new = _by_url(assets, "https://cdn.example.com/v/b.mp4")
    assert old is not None and new is not None
    assert old.download_state == SourceAsset.DOWNLOAD_UNAVAILABLE
    assert new.download_state == SourceAsset.DOWNLOAD_PENDING


def test_video_disappearing_entirely_still_retires_it(session: Session, tmp_path: Path) -> None:
    service = _service(session, tmp_path)
    service.sync_sources([_video("https://cdn.example.com/v/a.mp4")])
    session.commit()
    for asset in _assets(session, "vid-1"):
        asset.download_state = SourceAsset.DOWNLOAD_READY
    session.commit()

    service.sync_sources([_video(None, title="vid v2")])
    session.commit()
    session.expire_all()

    assets = _assets(session, "vid-1")
    assert len(assets) == 1
    assert assets[0].download_state == SourceAsset.DOWNLOAD_UNAVAILABLE


def test_video_swap_does_not_disturb_coexisting_images(session: Session, tmp_path: Path) -> None:
    """Kinds reconcile independently: swapping the video leaves the images alone."""
    service = _service(session, tmp_path)
    mixed = CapturedSource(
        platform="douyin",
        external_id="mix-1",
        source_type="video",
        title="mixed",
        creator=CapturedCreator(external_creator_id="c1", display_name="c1"),
        media=[
            CapturedMedia(kind="video", url="https://cdn.example.com/v/a.mp4"),
            CapturedMedia(kind="image", url=IMG_A),
            CapturedMedia(kind="image", url=IMG_B),
        ],
    )
    service.sync_sources([mixed])
    session.commit()
    assert len(_current(session, "mix-1")) == 3
    for asset in _assets(session, "mix-1"):
        asset.download_state = SourceAsset.DOWNLOAD_READY
    session.commit()

    swapped = mixed.model_copy(
        update={
            "title": "mixed v2",
            "media": [
                CapturedMedia(kind="video", url="https://cdn.example.com/v/b.mp4"),
                CapturedMedia(kind="image", url=IMG_A),
                CapturedMedia(kind="image", url=IMG_B),
            ],
        }
    )
    service.sync_sources([swapped])
    session.commit()
    session.expire_all()

    assets = _assets(session, "mix-1")
    for url in (IMG_A, IMG_B):
        image = _by_url(assets, url)
        assert image is not None
        assert image.download_state == SourceAsset.DOWNLOAD_READY, "images are untouched"
    old_video = _by_url(assets, "https://cdn.example.com/v/a.mp4")
    new_video = _by_url(assets, "https://cdn.example.com/v/b.mp4")
    assert old_video is not None and new_video is not None
    assert old_video.download_state == SourceAsset.DOWNLOAD_UNAVAILABLE
    assert new_video.download_state == SourceAsset.DOWNLOAD_PENDING


# ------------------------------------------------- reactivation: sync-retired → current


def test_sync_retired_image_reappearing_becomes_pending_again(
    session: Session, tmp_path: Path
) -> None:
    """A,B,C → A,C → A,B,C: B disappears then returns, must become acquirable."""
    service = _service(session, tmp_path)
    service.sync_sources([_album(IMG_A, IMG_B, IMG_C)])
    session.commit()
    for asset in _assets(session):
        asset.download_state = SourceAsset.DOWNLOAD_READY
    session.commit()

    service.sync_sources([_album(IMG_A, IMG_C, title="album v2")])
    session.commit()
    b_retired = _by_url(_assets(session), IMG_B)
    assert b_retired is not None
    assert b_retired.download_state == SourceAsset.DOWNLOAD_UNAVAILABLE
    assert "disappeared" in b_retired.download_error

    service.sync_sources([_album(IMG_A, IMG_B, IMG_C, title="album v3")])
    session.commit()
    session.expire_all()

    b_reactivated = _by_url(_assets(session), IMG_B)
    assert b_reactivated is not None
    assert b_reactivated.id == b_retired.id, "same row is reused, not a new insert"
    assert b_reactivated.download_state == SourceAsset.DOWNLOAD_PENDING, (
        "sync-retired asset reappearing must become pending so acquisition can fetch it"
    )
    assert b_reactivated.download_error is None
    assert b_reactivated.download_attempts == 0
    assert b_reactivated.downloaded_at_ms is None
    assert b_reactivated.sha256 is None, "local bytes were deleted at retirement"


def test_sync_retired_ready_asset_resets_to_pending_not_ready(
    session: Session, tmp_path: Path
) -> None:
    """A sync-retired asset that was READY before retirement must return as PENDING."""
    service = _service(session, tmp_path)
    service.sync_sources([_album(IMG_A, IMG_B)])
    session.commit()
    b_asset = _by_url(_assets(session), IMG_B)
    assert b_asset is not None
    b_asset.download_state = SourceAsset.DOWNLOAD_READY
    b_asset.downloaded_at_ms = 5000
    b_asset.sha256 = "abc123"
    session.commit()

    service.sync_sources([_album(IMG_A, title="album v2")])
    session.commit()
    session.expire_all()
    b_retired = _by_url(_assets(session), IMG_B)
    assert b_retired is not None
    assert b_retired.download_state == SourceAsset.DOWNLOAD_UNAVAILABLE

    service.sync_sources([_album(IMG_A, IMG_B, title="album v3")])
    session.commit()
    session.expire_all()

    b_reactivated = _by_url(_assets(session), IMG_B)
    assert b_reactivated is not None
    assert b_reactivated.download_state == SourceAsset.DOWNLOAD_PENDING, (
        "must not remain ready; local file was deleted at retirement"
    )
    assert b_reactivated.sha256 is None
    assert b_reactivated.downloaded_at_ms is None


def test_acquisition_terminal_unavailable_stays_terminal_on_reappearance(
    session: Session, tmp_path: Path
) -> None:
    """Asset marked unavailable by acquisition failure must not be reactivated."""
    service = _service(session, tmp_path)
    service.sync_sources([_album(IMG_A)])
    session.commit()
    a_asset = _by_url(_assets(session), IMG_A)
    assert a_asset is not None
    a_asset.download_state = SourceAsset.DOWNLOAD_UNAVAILABLE
    a_asset.download_error = "file exceeds size limit (50MB)"
    session.commit()

    service.sync_sources([_album(IMG_A, title="album v2")])
    session.commit()
    session.expire_all()

    a_still_terminal = _by_url(_assets(session), IMG_A)
    assert a_still_terminal is not None
    assert a_still_terminal.download_state == SourceAsset.DOWNLOAD_UNAVAILABLE, (
        "acquisition-terminal unavailable must not be reactivated"
    )
    assert "exceeds size limit" in a_still_terminal.download_error


def test_legacy_superseded_message_is_repairable(session: Session, tmp_path: Path) -> None:
    """Assets with legacy 'superseded by a newer asset' message must reactivate."""
    service = _service(session, tmp_path)
    service.sync_sources([_album(IMG_A)])
    session.commit()
    a_asset = _by_url(_assets(session), IMG_A)
    assert a_asset is not None
    a_asset.download_state = SourceAsset.DOWNLOAD_UNAVAILABLE
    a_asset.download_error = "superseded by a newer asset for this source"
    session.commit()

    service.sync_sources([_album(IMG_A, title="album v2")])
    session.commit()
    session.expire_all()

    a_reactivated = _by_url(_assets(session), IMG_A)
    assert a_reactivated is not None
    assert a_reactivated.download_state == SourceAsset.DOWNLOAD_PENDING
    assert a_reactivated.download_error is None


def test_legacy_media_type_disappeared_message_is_repairable(
    session: Session, tmp_path: Path
) -> None:
    """Assets with legacy 'media type disappeared' message must reactivate."""
    service = _service(session, tmp_path)
    service.sync_sources([_album(IMG_A)])
    session.commit()
    a_asset = _by_url(_assets(session), IMG_A)
    assert a_asset is not None
    a_asset.download_state = SourceAsset.DOWNLOAD_UNAVAILABLE
    a_asset.download_error = "media type disappeared from authoritative capture"
    session.commit()

    service.sync_sources([_album(IMG_A, title="album v2")])
    session.commit()
    session.expire_all()

    a_reactivated = _by_url(_assets(session), IMG_A)
    assert a_reactivated is not None
    assert a_reactivated.download_state == SourceAsset.DOWNLOAD_PENDING
    assert a_reactivated.download_error is None


# ------------------------------------------------- unchanged payload reconciliation


def test_unchanged_payload_reconciles_assets_without_duplicate_snapshot(
    session: Session, tmp_path: Path
) -> None:
    """Asset reconciliation runs even when source hash is unchanged."""
    service = _service(session, tmp_path)
    service.sync_sources([_album(IMG_A, IMG_B)])
    session.commit()
    b_asset = _by_url(_assets(session), IMG_B)
    assert b_asset is not None
    b_asset.download_state = SourceAsset.DOWNLOAD_UNAVAILABLE
    b_asset.download_error = "disappeared from the authoritative capture"
    session.commit()

    from douyin_knowledge.db.models.capture import SourceSnapshot

    snapshots_before = session.scalars(select(SourceSnapshot)).all()
    assert len(snapshots_before) == 1

    # Re-sync the exact same payload
    service.sync_sources([_album(IMG_A, IMG_B)])
    session.commit()
    session.expire_all()

    snapshots_after = session.scalars(select(SourceSnapshot)).all()
    assert len(snapshots_after) == 1, "unchanged source must not create duplicate snapshot"

    b_reactivated = _by_url(_assets(session), IMG_B)
    assert b_reactivated is not None
    assert b_reactivated.download_state == SourceAsset.DOWNLOAD_PENDING, (
        "asset reconciliation must run even when source hash unchanged"
    )


def test_repeated_unchanged_sync_stays_idempotent(session: Session, tmp_path: Path) -> None:
    """Multiple syncs of identical payload must not churn state or multiply rows."""
    service = _service(session, tmp_path)
    service.sync_sources([_album(IMG_A, IMG_B)])
    session.commit()
    for asset in _assets(session):
        asset.download_state = SourceAsset.DOWNLOAD_READY
    session.commit()

    service.sync_sources([_album(IMG_A, IMG_B)])
    session.commit()
    after_second = {(a.id, a.download_state) for a in _assets(session)}

    service.sync_sources([_album(IMG_A, IMG_B)])
    session.commit()
    session.expire_all()
    after_third = {(a.id, a.download_state) for a in _assets(session)}

    assert after_third == after_second, "repeated unchanged sync must be idempotent"
    assert len(_current(session)) == 2
