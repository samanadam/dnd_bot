"""Bulk-importing a local folder of sounds into the music bucket.

The import reuses the upload checks, so the refusals are what matter: a file
only reaches R2 when its name, size, first bytes and probed audio all check out,
and nothing already in the bucket is ever overwritten.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_r2 import FakeS3

from dnd_bot.r2 import R2Store
from dnd_bot.soundimport import NothingToImport, import_sounds
from dnd_bot.uploads import Probe

OGG = b"OggS" + b"\x00" * 60
MP3 = b"ID3" + b"\x00" * 61
LIMITS = {"prefix": "music", "max_bytes": 1_000_000, "max_ambience_seconds": 1800.0}


class Prober:
    def __init__(self, probe: Probe = Probe(True, 30.0)) -> None:
        self.probe = probe
        self.calls: list[Path] = []

    def __call__(self, path: Path) -> Probe:
        self.calls.append(path)
        return self.probe


@pytest.fixture
def s3() -> FakeS3:
    return FakeS3()


@pytest.fixture
def store(s3: FakeS3) -> R2Store:
    return R2Store(s3, "bucket")


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "sounds"
    (root / "ambience").mkdir(parents=True)
    (root / "sfx").mkdir()
    return root


def statuses(outcomes) -> dict[str, str]:
    return {outcome.path.name: outcome.status for outcome in outcomes}


def reasons(outcomes) -> dict[str, str]:
    return {outcome.path.name: outcome.reason for outcome in outcomes}


def test_uploads_each_folder_under_its_own_prefix(library, store, s3):
    (library / "ambience" / "Tavern Rain.ogg").write_bytes(OGG)
    (library / "sfx" / "Sword Hit.mp3").write_bytes(MP3)

    outcomes = import_sounds(library, store, prober=Prober(), **LIMITS)

    assert statuses(outcomes) == {"Tavern Rain.ogg": "uploaded", "Sword Hit.mp3": "uploaded"}
    assert set(s3.objects) == {"music/ambience/Tavern Rain.ogg", "music/sfx/Sword Hit.mp3"}
    assert s3.extra_args == {"ContentType": "audio/mpeg"}


def test_names_are_sanitised_like_an_upload(library, store, s3):
    (library / "sfx" / "  big   boom [1].ogg").write_bytes(OGG)

    import_sounds(library, store, prober=Prober(), **LIMITS)

    assert list(s3.objects) == ["music/sfx/big boom 1.ogg"]


def test_existing_names_are_never_overwritten(library, store, s3):
    s3.objects["music/sfx/Boom.ogg"] = b"original"
    (library / "sfx" / "Boom.ogg").write_bytes(OGG)

    outcomes = import_sounds(library, store, prober=Prober(), **LIMITS)

    assert statuses(outcomes) == {"Boom.ogg": "skipped"}
    assert "already" in reasons(outcomes)["Boom.ogg"]
    assert s3.objects["music/sfx/Boom.ogg"] == b"original"


def test_two_files_that_sanitise_to_one_name_upload_only_the_first(library, store, s3):
    (library / "sfx" / "Boom#.ogg").write_bytes(OGG)
    (library / "sfx" / "Boom@.ogg").write_bytes(OGG)

    outcomes = import_sounds(library, store, prober=Prober(), **LIMITS)

    assert sorted(o.status for o in outcomes) == ["skipped", "uploaded"]
    assert s3.uploads == ["music/sfx/Boom.ogg"]


def test_a_file_that_is_not_audio_is_skipped_with_a_reason(library, store, s3):
    (library / "sfx" / "notes.txt").write_text("credits", encoding="utf-8")
    (library / "sfx" / "fake.mp3").write_bytes(b"<?php echo 1;")

    outcomes = import_sounds(library, store, prober=Prober(), **LIMITS)

    assert statuses(outcomes) == {"notes.txt": "skipped", "fake.mp3": "skipped"}
    assert s3.uploads == []


def test_a_file_ffprobe_finds_no_audio_in_is_skipped(library, store, s3):
    (library / "sfx" / "silent.ogg").write_bytes(OGG)

    outcomes = import_sounds(library, store, prober=Prober(Probe(False, None)), **LIMITS)

    assert statuses(outcomes) == {"silent.ogg": "skipped"}
    assert s3.uploads == []


def test_an_effect_is_capped_at_two_minutes_but_ambience_is_not(library, store, s3):
    (library / "sfx" / "long.ogg").write_bytes(OGG)
    (library / "ambience" / "long.ogg").write_bytes(OGG)

    outcomes = import_sounds(library, store, prober=Prober(Probe(True, 900.0)), **LIMITS)

    by_folder = {o.folder: o.status for o in outcomes}
    assert by_folder == {"sfx": "skipped", "ambience": "uploaded"}


def test_ambience_over_the_track_limit_is_skipped(library, store, s3):
    (library / "ambience" / "day.ogg").write_bytes(OGG)

    outcomes = import_sounds(library, store, prober=Prober(Probe(True, 5000.0)), **LIMITS)

    assert statuses(outcomes) == {"day.ogg": "skipped"}


def test_a_file_over_the_size_cap_is_skipped_before_it_is_probed(library, store, s3):
    (library / "sfx" / "huge.ogg").write_bytes(OGG + b"\x00" * 2_000_000)
    prober = Prober()

    outcomes = import_sounds(library, store, prober=prober, **LIMITS)

    assert statuses(outcomes) == {"huge.ogg": "skipped"}
    assert prober.calls == []


def test_an_empty_file_is_skipped(library, store, s3):
    (library / "sfx" / "empty.ogg").write_bytes(b"")

    outcomes = import_sounds(library, store, prober=Prober(), **LIMITS)

    assert statuses(outcomes) == {"empty.ogg": "skipped"}


def test_a_dry_run_checks_everything_but_uploads_nothing(library, store, s3):
    (library / "sfx" / "Boom.ogg").write_bytes(OGG)
    (library / "sfx" / "bad.ogg").write_bytes(b"nope")

    outcomes = import_sounds(library, store, prober=Prober(), dry_run=True, **LIMITS)

    assert statuses(outcomes) == {"Boom.ogg": "would upload", "bad.ogg": "skipped"}
    assert s3.uploads == []


def test_a_failed_upload_is_reported_and_the_rest_carry_on(library, store, s3):
    (library / "sfx" / "a.ogg").write_bytes(OGG)
    (library / "sfx" / "b.ogg").write_bytes(OGG)
    real = s3.upload_file

    def flaky(Filename, Bucket, Key, ExtraArgs=None):  # noqa: N803
        if Key.endswith("a.ogg"):
            raise OSError("connection reset")
        real(Filename, Bucket, Key, ExtraArgs)

    s3.upload_file = flaky

    outcomes = import_sounds(library, store, prober=Prober(), **LIMITS)

    assert statuses(outcomes) == {"a.ogg": "failed", "b.ogg": "uploaded"}
    assert "connection reset" in reasons(outcomes)["a.ogg"]


def test_files_outside_the_two_folders_are_left_alone(library, store, s3):
    (library / "music").mkdir()
    (library / "music" / "song.ogg").write_bytes(OGG)
    (library / "ambience" / "nested").mkdir()
    (library / "ambience" / "nested" / "deep.ogg").write_bytes(OGG)
    (library / "ambience" / ".hidden.ogg").write_bytes(OGG)

    outcomes = import_sounds(library, store, prober=Prober(), **LIMITS)

    assert outcomes == []
    assert s3.uploads == []


def test_a_folder_with_neither_subfolder_is_an_error(tmp_path, store):
    with pytest.raises(NothingToImport):
        import_sounds(tmp_path, store, prober=Prober(), **LIMITS)

    with pytest.raises(NothingToImport):
        import_sounds(tmp_path / "missing", store, prober=Prober(), **LIMITS)


def test_one_missing_subfolder_is_fine(tmp_path, store, s3):
    (tmp_path / "sfx").mkdir()
    (tmp_path / "sfx" / "Boom.ogg").write_bytes(OGG)

    outcomes = import_sounds(tmp_path, store, prober=Prober(), **LIMITS)

    assert statuses(outcomes) == {"Boom.ogg": "uploaded"}
