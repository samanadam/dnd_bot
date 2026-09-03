"""The sink is what stands between a crash and a lost session."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("discord", reason="py-cord is only installed with runtime deps")

from dnd_bot.audio import BYTES_PER_SECOND  # noqa: E402
from dnd_bot.sinks import DiskSink  # noqa: E402

PACKET = b"\x01\x00" * 960  # 20 ms of 48 kHz stereo 16-bit


def make_sink(tmp_path: Path, clock_box: list[float], **kwargs) -> DiskSink:
    return DiskSink(tmp_path / "raw", clock=lambda: clock_box[0], **kwargs)


def test_packets_land_on_disk_as_they_arrive(tmp_path: Path):
    clock = [0.0]
    sink = make_sink(tmp_path, clock, flush_interval=0.0)

    sink.write(PACKET, 1001)
    # Written before any stop/cleanup: a crash here still keeps the audio.
    assert sink.path_for("1001").stat().st_size == len(PACKET)

    sink.write(PACKET, 1001)
    assert sink.bytes_written("1001") == 2 * len(PACKET)
    sink.cleanup()


def test_each_speaker_gets_their_own_file(tmp_path: Path):
    clock = [0.0]
    sink = make_sink(tmp_path, clock)
    sink.write(PACKET, 1001)
    sink.write(PACKET, 1002)
    sink.cleanup()

    assert sorted(p.name for p in (tmp_path / "raw").iterdir()) == ["1001.pcm", "1002.pcm"]


def test_first_packet_time_becomes_the_speaker_offset(tmp_path: Path):
    clock = [0.0]
    sink = make_sink(tmp_path, clock)

    sink.write(PACKET, 1001)
    clock[0] = 12.5
    sink.write(PACKET, 1002)

    assert sink.offsets == {"1001": 0.0, "1002": 12.5}
    sink.cleanup()


def test_new_speaker_callback_fires_once_per_speaker(tmp_path: Path):
    clock = [0.0]
    seen: list[tuple[str, float]] = []
    sink = make_sink(tmp_path, clock, on_new_speaker=lambda uid, off: seen.append((uid, off)))

    sink.write(PACKET, 1001)
    sink.write(PACKET, 1001)
    clock[0] = 5.0
    sink.write(PACKET, 1002)

    assert seen == [("1001", 0.0), ("1002", 5.0)]
    sink.cleanup()


def test_a_failing_callback_does_not_kill_the_receive_thread(tmp_path: Path):
    clock = [0.0]

    def boom(_uid: str, _off: float) -> None:
        raise RuntimeError("callback exploded")

    sink = make_sink(tmp_path, clock, on_new_speaker=boom)
    sink.write(PACKET, 1001)  # must not raise
    assert sink.bytes_written("1001") == len(PACKET)
    sink.cleanup()


def test_resumed_sink_keeps_session_relative_offsets(tmp_path: Path):
    """After a voice reconnect the replacement sink must not restart at zero."""
    clock = [0.0]
    first = make_sink(tmp_path, clock)
    first.write(PACKET, 1001)
    clock[0] = 30.0
    first.write(PACKET, 1002)
    first.cleanup()

    clock[0] = 60.0
    resumed = DiskSink(
        tmp_path / "raw",
        clock=lambda: clock[0],
        base_offset=60.0,
        known_offsets=first.offsets,
    )
    clock[0] = 61.0
    resumed.write(PACKET, 1001)  # known speaker keeps their original offset
    resumed.write(PACKET, 1003)  # new speaker is placed at session time
    assert resumed.offsets["1001"] == 0.0
    assert resumed.offsets["1003"] == pytest.approx(61.0)
    # Audio is appended to the existing capture, not truncated.
    resumed.flush_all()
    assert resumed.path_for("1001").stat().st_size == 2 * len(PACKET)
    resumed.cleanup()


def test_speaker_list_survives_cleanup(tmp_path: Path):
    clock = [0.0]
    sink = make_sink(tmp_path, clock)
    sink.write(PACKET, 1001)
    sink.cleanup()
    assert sink.speakers == ["1001"]


def test_writes_after_cleanup_are_ignored(tmp_path: Path):
    clock = [0.0]
    sink = make_sink(tmp_path, clock)
    sink.write(PACKET, 1001)
    sink.cleanup()
    sink.write(PACKET, 1001)
    assert sink.path_for("1001").stat().st_size == len(PACKET)


def test_duration_is_derived_from_bytes(tmp_path: Path):
    clock = [0.0]
    sink = make_sink(tmp_path, clock)
    sink.write(b"\x00" * BYTES_PER_SECOND, 1001)
    assert sink.duration_seconds("1001") == 1.0
    sink.cleanup()


def test_cleanup_is_idempotent(tmp_path: Path):
    clock = [0.0]
    sink = make_sink(tmp_path, clock)
    sink.write(PACKET, 1001)
    sink.cleanup()
    sink.cleanup()  # must not raise


class FakeSpeaker:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class FakeVoiceData:
    """What py-cord 2.8 hands to Sink.write: decoded PCM plus the speaker."""

    def __init__(self, pcm: bytes, source) -> None:  # noqa: ANN001 - mirrors py-cord
        self.pcm = pcm
        self.source = source


def test_a_voice_data_packet_is_written_under_its_speaker(tmp_path: Path):
    clock = [0.0]
    sink = make_sink(tmp_path, clock)

    sink.write(FakeVoiceData(PACKET, FakeSpeaker(1001)), None)
    sink.cleanup()

    assert sink.path_for("1001").read_bytes() == PACKET
    assert sink.speakers == ["1001"]


def test_an_unattributed_packet_is_dropped(tmp_path: Path):
    """No speaker means no label, so the audio could never be transcribed."""
    clock = [0.0]
    sink = make_sink(tmp_path, clock)

    sink.write(FakeVoiceData(PACKET, None), None)
    sink.cleanup()

    assert sink.speakers == []
    assert list(tmp_path.glob("raw/*.pcm")) == []


def test_the_sink_satisfies_the_receive_routers_contract(tmp_path: Path):
    """py-cord 2.8's SinkEventRouter reads both off every sink before recording."""
    sink = make_sink(tmp_path, [0.0])

    assert sink.__sink_listeners__ == ()
    assert list(sink.walk_children()) == []
