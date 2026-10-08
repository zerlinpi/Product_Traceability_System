"""Product images survive a backup and a restore.

Why this file exists
--------------------
The database stores ``/api/product-images/<name>`` URLs; the bytes live in
``<database dir>/product-images/``. A backup that carried only the ``.db``
therefore restored into a catalogue where every picture was a 404 — the rows came
back, the images did not, and nothing said so.

These tests hold the line on the properties that make the fix real:

* the archive is written only when there is something to archive, and the
  manifest says what is in it;
* verification refuses a backup whose archive is missing or altered, because a
  restore that silently drops images is worse than one that stops;
* the restore puts back exactly the bytes that were taken, including nested
  paths;
* a backup from before images were archived still verifies and still restores —
  it simply does not touch what is on disk, because deleting images that nothing
  else can reproduce is not something a restore should decide on its own.

Feature: batch-traceability, Property 71: 备份可验证与可恢复
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capability_helpers import bootstrap_admin, make_auth_app  # noqa: E402
from traceability.backup import (  # noqa: E402
    BackupError,
    create_backup,
    image_archive_path,
    image_dir_for,
    list_backups,
    manifest_path_for,
    prune_backups,
    restore_backup,
    verify_backup,
)

FIXED_NOW = "2026-10-08T10:00:00+08:00"
LATER = "2026-10-08T11:00:00+08:00"

#: A small PNG header plus filler, so the archive has something real to compress.
IMAGE_A = b"\x89PNG\r\n\x1a\n" + b"A" * 1200
IMAGE_B = b"\x89PNG\r\n\x1a\n" + b"B" * 3400
NESTED = b"\x89PNG\r\n\x1a\n" + b"C" * 800


@pytest.fixture()
def live(tmp_path):
    """A migrated database, plus the image directory that belongs to it."""
    _app, database_path, _fake = make_auth_app(tmp_path)
    return database_path, image_dir_for(database_path)


def write_images(directory: Path, images: dict[str, bytes]) -> None:
    for name, data in images.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def digest_tree(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }


def backup(live, name: str = "backups") -> tuple[Path, Path]:
    database_path, _images = live
    directory = database_path.parent / name
    report = create_backup(database_path, directory, created_at=FIXED_NOW)
    return report.path, directory


# --------------------------------------------------------------------------
# Taking the backup
# --------------------------------------------------------------------------


def test_images_are_archived_beside_the_database(live):
    _database, images = live
    images.mkdir(parents=True)
    write_images(images, {"a.png": IMAGE_A, "b.png": IMAGE_B})

    path, _directory = backup(live)

    archive = image_archive_path(path)
    assert archive.is_file(), "图片没有被归档"
    with zipfile.ZipFile(archive) as bundle:
        assert sorted(bundle.namelist()) == ["a.png", "b.png"]


def test_the_manifest_records_what_the_archive_holds(live):
    _database, images = live
    images.mkdir(parents=True)
    write_images(images, {"a.png": IMAGE_A, "b.png": IMAGE_B})

    path, _directory = backup(live)

    manifest = json.loads(manifest_path_for(path).read_text(encoding="utf-8"))
    entry = manifest["images"]
    assert entry["file_count"] == 2
    assert entry["total_bytes"] == len(IMAGE_A) + len(IMAGE_B)
    assert entry["sha256"] == hashlib.sha256(image_archive_path(path).read_bytes()).hexdigest()


def test_a_database_without_images_grows_no_archive(live):
    """An empty archive on every run would be noise in the backup directory."""
    path, _directory = backup(live)

    assert not image_archive_path(path).exists()
    manifest = json.loads(manifest_path_for(path).read_text(encoding="utf-8"))
    assert manifest["images"] is None


def test_nested_image_paths_are_preserved(live):
    _database, images = live
    images.mkdir(parents=True)
    write_images(images, {"a.png": IMAGE_A, "sub/dir/c.png": NESTED})

    path, _directory = backup(live)

    with zipfile.ZipFile(image_archive_path(path)) as bundle:
        assert sorted(bundle.namelist()) == ["a.png", "sub/dir/c.png"]


def test_the_same_images_produce_the_same_archive(live):
    """Determinism: a ZIP stores mtimes, which would make two runs differ."""
    _database, images = live
    images.mkdir(parents=True)
    write_images(images, {"a.png": IMAGE_A})

    first, _directory = backup(live, "backups-a")
    second, _directory2 = backup(live, "backups-b")

    assert sha256_of(image_archive_path(first)) == sha256_of(image_archive_path(second))


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


def test_verification_reports_the_image_count(live):
    _database, images = live
    images.mkdir(parents=True)
    write_images(images, {"a.png": IMAGE_A, "b.png": IMAGE_B})

    path, _directory = backup(live)

    report = verify_backup(path)
    assert report.ok, report.problems
    assert report.image_count == 2


def test_a_missing_archive_makes_the_backup_unusable(live):
    """Restoring it would bring back rows pointing at pictures that are gone."""
    _database, images = live
    images.mkdir(parents=True)
    write_images(images, {"a.png": IMAGE_A})

    path, _directory = backup(live)
    image_archive_path(path).unlink()

    report = verify_backup(path)

    assert not report.ok
    assert any("图片归档缺失" in problem for problem in report.problems), report.problems


def test_an_altered_archive_is_caught(live):
    _database, images = live
    images.mkdir(parents=True)
    write_images(images, {"a.png": IMAGE_A})

    path, _directory = backup(live)
    archive = image_archive_path(path)
    with zipfile.ZipFile(archive, "a") as bundle:
        bundle.writestr("injected.png", b"tampered")
    # Recompress so the file is a valid zip that no longer matches the manifest.
    _rewrite(archive)

    report = verify_backup(path)

    assert not report.ok
    assert any("SHA-256 不匹配" in problem for problem in report.problems), report.problems


def _rewrite(archive: Path) -> None:
    """Re-pack an archive in place, dropping duplicate entries."""
    with zipfile.ZipFile(archive) as bundle:
        members = [(info.filename, bundle.read(info.filename)) for info in bundle.infolist()]
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, data in members:
            bundle.writestr(name, data)


def test_a_backup_from_before_images_were_archived_still_verifies(live):
    """Backward compatibility: old manifests have no images entry at all."""
    path, _directory = backup(live)
    manifest_path = manifest_path_for(path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("images", None)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    report = verify_backup(path)

    assert report.ok, report.problems
    assert report.image_count is None, "旧格式备份不应声称含有图片"


# --------------------------------------------------------------------------
# Restoring
# --------------------------------------------------------------------------


def test_restore_puts_the_images_back_byte_for_byte(live):
    database_path, images = live
    images.mkdir(parents=True)
    taken = {"a.png": IMAGE_A, "b.png": IMAGE_B, "sub/c.png": NESTED}
    write_images(images, taken)
    before = digest_tree(images)

    path, _directory = backup(live)
    # Simulate the disaster the backup exists for.
    for item in sorted(images.rglob("*"), reverse=True):
        item.unlink() if item.is_file() else item.rmdir()

    report = restore_backup(path, database_path, created_at=LATER)

    assert report.images_restored == 3
    assert digest_tree(images) == before, "恢复后的图片与备份时不一致"


def test_restore_moves_the_previous_images_aside(live):
    """A restore against the wrong backup has to be undoable."""
    database_path, images = live
    images.mkdir(parents=True)
    write_images(images, {"a.png": IMAGE_A})
    path, _directory = backup(live)

    write_images(images, {"newer.png": IMAGE_B})

    report = restore_backup(path, database_path, created_at=LATER)

    assert report.images_safety_copy is not None
    assert (report.images_safety_copy / "newer.png").is_file(), "被覆盖的图片没有保留"
    assert digest_tree(images) == {"a.png": hashlib.sha256(IMAGE_A).hexdigest()}


def test_restoring_a_backup_without_images_leaves_the_disk_alone(live):
    """Deleting pictures that nothing else can reproduce is not a restore's call."""
    database_path, images = live
    path, _directory = backup(live)
    assert not image_archive_path(path).exists()

    images.mkdir(parents=True)
    write_images(images, {"existing.png": IMAGE_B})

    report = restore_backup(path, database_path, created_at=LATER)

    assert report.images_restored == 0
    assert report.images_left_in_place is True
    assert (images / "existing.png").read_bytes() == IMAGE_B


def test_a_corrupt_archive_stops_the_restore_before_it_touches_anything(live):
    database_path, images = live
    images.mkdir(parents=True)
    write_images(images, {"a.png": IMAGE_A})
    path, _directory = backup(live)

    archive = image_archive_path(path)
    archive.write_bytes(archive.read_bytes()[: len(archive.read_bytes()) // 2])

    with pytest.raises(BackupError) as error:
        restore_backup(path, database_path, created_at=LATER)

    assert "校验" in str(error.value)


def test_an_archive_cannot_write_outside_the_image_directory(live):
    """A crafted member name must not escape the target directory."""
    database_path, images = live
    images.mkdir(parents=True)
    write_images(images, {"a.png": IMAGE_A})
    path, _directory = backup(live)

    archive = image_archive_path(path)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("../../escaped.png", b"nope")

    # Update the manifest so verification passes and the extractor is reached.
    manifest_path = manifest_path_for(path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["images"]["sha256"] = sha256_of(archive)
    manifest["images"]["file_count"] = 1
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(BackupError) as error:
        restore_backup(path, database_path, created_at=LATER)

    assert "非法路径" in str(error.value)
    assert not (database_path.parent.parent / "escaped.png").exists()


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------


def test_pruning_removes_the_archive_with_its_backup(live):
    """An orphaned archive is invisible to list_backups, so nothing would ever clean it."""
    _database, images = live
    images.mkdir(parents=True)
    write_images(images, {"a.png": IMAGE_A})

    database_path, _images = live
    directory = database_path.parent / "backups"
    create_backup(database_path, directory, created_at="2026-10-08T09:00:00")
    second = create_backup(database_path, directory, created_at="2026-10-08T10:00:00")

    removed = prune_backups(directory, keep=1)

    assert len(removed) == 1
    assert not image_archive_path(removed[0]).exists(), "归档没有随备份一起删除"
    assert image_archive_path(second.path).is_file(), "保留的那份归档被误删"
    assert list_backups(directory) == [second.path]


# --------------------------------------------------------------------------
# End to end, through the database the application actually writes
# --------------------------------------------------------------------------


def test_an_image_uploaded_through_the_api_survives_a_round_trip(tmp_path):
    """The tests above prove the archive works; this proves the wiring is right.

    It uploads through the real endpoint, so the directory the application writes
    to and the directory the backup reads from have to be the same one. If they
    ever drift apart, every other test here still passes and the factory loses
    its pictures.
    """
    import io
    import struct
    import zlib

    app, database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)

    # A real PNG: the endpoint validates by signature and reads the IHDR, so a
    # hand-made header would be rejected before it reaches the disk.
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr
    content = (
        b"\x89PNG\r\n\x1a\n"
        + chunk
        + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
        + zlib.compress(b"pixel")
    )

    uploaded = admin.post(
        "/api/product-images",
        data={"file": (io.BytesIO(content), "main.png")},
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": csrf},
    )
    assert uploaded.status_code == 201, uploaded.get_json()
    filename = uploaded.get_json()["data"]["filename"]

    directory = database_path.parent / "backups"
    report = create_backup(database_path, directory, created_at=FIXED_NOW)
    assert report.images and report.images["file_count"] == 1

    images = image_dir_for(database_path)
    taken = digest_tree(images)
    for path in images.rglob("*"):
        path.unlink()

    restore_backup(report.path, database_path, created_at=LATER)

    assert digest_tree(images) == taken
    assert (images / filename).read_bytes() == content
    assert admin.get(f"/api/product-images/{filename}").get_data() == content


def test_the_database_and_the_images_come_from_the_same_moment(live):
    """Both halves of a backup describe one point in time, not two."""
    database_path, images = live
    images.mkdir(parents=True)
    write_images(images, {"a.png": IMAGE_A})

    path, _directory = backup(live)
    manifest = json.loads(manifest_path_for(path).read_text(encoding="utf-8"))

    # The manifest is written once, after both halves exist.
    assert manifest["images"] is not None
    assert manifest["created_at"] == FIXED_NOW
    assert image_archive_path(path).stat().st_mtime >= path.stat().st_mtime - 1


def test_the_database_backup_is_still_a_plain_sqlite_file(live):
    """Older tooling and older manifests must keep working on the .db itself."""
    _database, images = live
    images.mkdir(parents=True)
    write_images(images, {"a.png": IMAGE_A})

    path, _directory = backup(live)

    assert path.open("rb").read(15) == b"SQLite format 3"
    connection = sqlite3.connect(str(path))
    try:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= 1
    finally:
        connection.close()
