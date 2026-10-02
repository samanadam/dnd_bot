"""The sink is what stands between a crash and a lost session."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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


# Everything py-cord 2.8's receive path reads off a sink. Its own Sink base
# class provides none of the first three, so each missing one crashed a live
# recording that the rest of the suite could not catch. Derived by grepping
# discord/voice/receive/ and discord/opus.py for "sink.".
RECEIVE_PATH_CONTRACT = ("__sink_listeners__", "walk_children", "is_opus", "client", "write")


def test_the_sink_satisfies_the_receive_paths_contract(tmp_path: Path):
    sink = make_sink(tmp_path, [0.0])

    missing = [name for name in RECEIVE_PATH_CONTRACT if not hasattr(sink, name)]
    assert missing == [], f"py-cord will crash mid-recording on: {missing}"

    assert sink.__sink_listeners__ == ()
    assert list(sink.walk_children()) == []
    assert sink.is_opus() is False, "True would hand us undecoded Opus, not PCM"


def test_init_gives_the_sink_the_voice_client(tmp_path: Path):
    """The packet decoder maps an ssrc back to a speaker through sink.client."""
    sink = make_sink(tmp_path, [0.0])
    assert sink.client is None

    voice_client = object()
    sink.init(voice_client)

    assert sink.client is voice_client


def test_the_sink_ignores_the_bots_own_audio(tmp_path: Path):
    """Playback must never land in the recording.

    Discord does not loop a bot's own transmission back to it, so this guard
    should never fire in practice. It exists because the failure it prevents -
    a track file named after the bot, full of the music it just played - would
    be discovered late and be hard to explain.
    """
    sink = DiskSink(tmp_path, ignore_user_ids={"999"})
    sink.write(b"\x01" * 3840, SimpleNamespace(id=999))
    assert sink.bytes_written("999") == 0
    assert not list(tmp_path.glob("*.pcm"))


def test_the_sink_ignores_any_bot_member(tmp_path: Path):
    sink = DiskSink(tmp_path)
    sink.write(b"\x01" * 3840, SimpleNamespace(id=555, bot=True))
    assert sink.bytes_written("555") == 0


def test_the_sink_still_records_humans_while_ignoring_a_bot(tmp_path: Path):
    sink = DiskSink(tmp_path, ignore_user_ids={"999"})
    sink.write(b"\x01" * 3840, SimpleNamespace(id=999))
    sink.write(b"\x02" * 3840, SimpleNamespace(id=10))
    sink.cleanup()
    assert sink.bytes_written("10") == 3840
    assert (tmp_path / "10.pcm").exists()


# -- silence between bursts ----------------------------------------------------

from dnd_bot.gaps import read_gaps  # noqa: E402

PACKET20 = b"\x01\x00" * 1920  # a real 20 ms packet: 48 kHz stereo 16-bit


def gaps_of(sink: DiskSink, user_id: str):
    return read_gaps(sink.path_for(user_id).with_suffix(".gaps"))


def test_pause_between_bursts_is_recorded_as_a_gap(tmp_path: Path):
    clock = [0.0]
    sink = make_sink(tmp_path, clock)
    sink.write(PACKET20, 1001)  # speaker appears at +0.0
    clock[0] = 0.02
    sink.write(PACKET20, 1001)  # contiguous: no gap
    clock[0] = 10.0
    sink.write(PACKET20, 1001)  # 10 s later
    sink.cleanup()

    assert sink.path_for("1001").stat().st_size == 3 * len(PACKET20)  # pcm stays speech-only
    [(position, silence)] = gaps_of(sink, "1001")
    assert position == 2 * len(PACKET20)
    assert silence == round((10.0 - 2 * 0.02) * 48_000) * 4


def test_continuous_speech_with_jitter_records_no_gap(tmp_path: Path):
    clock = [0.0]
    sink = make_sink(tmp_path, clock)
    for i in range(200):  # 4 s of 20 ms packets, each arriving up to 30 ms late
        clock[0] = i * 0.02 + (0.03 if i % 2 else 0.0)
        sink.write(PACKET20, 1001)
    sink.cleanup()
    assert gaps_of(sink, "1001") == []


def test_a_network_stall_is_not_mistaken_for_a_pause(tmp_path: Path):
    """The backlog of a stall arrives at once; it was spoken in real time."""
    clock = [0.0]
    sink = make_sink(tmp_path, clock)
    for _ in range(50):  # 1 s of ordinary speech
        sink.write(PACKET20, 1001)
        clock[0] += 0.02
    clock[0] += 2.0  # the network stalls for 2 s...
    for _ in range(100):  # ...then 2 s of audio arrives in one burst
        sink.write(PACKET20, 1001)
    for _ in range(50):  # and speech carries on in real time
        sink.write(PACKET20, 1001)
        clock[0] += 0.02
    sink.cleanup()

    assert gaps_of(sink, "1001") == []


def test_a_real_pause_is_committed_once_speech_resumes_in_real_time(tmp_path: Path):
    clock = [0.0]
    sink = make_sink(tmp_path, clock)
    for _ in range(50):
        sink.write(PACKET20, 1001)
        clock[0] += 0.02
    clock[0] += 5.0  # nobody speaks for 5 s
    for _ in range(50):
        sink.write(PACKET20, 1001)
        clock[0] += 0.02
    sink.cleanup()

    [(position, silence)] = gaps_of(sink, "1001")
    assert position == 50 * len(PACKET20)
    assert silence / BYTES_PER_SECOND == pytest.approx(5.0, abs=0.001)


def test_a_pause_still_being_weighed_when_recording_ends_is_kept(tmp_path: Path):
    clock = [0.0]
    sink = make_sink(tmp_path, clock)
    sink.write(PACKET20, 1001)
    clock[0] = 8.0
    sink.write(PACKET20, 1001)  # a single packet after a pause, then the session ends
    sink.cleanup()

    [(_, silence)] = gaps_of(sink, "1001")
    assert silence / BYTES_PER_SECOND == pytest.approx(8.0 - 0.02, abs=0.001)


def test_speaker_who_leaves_and_rejoins_keeps_real_timing(tmp_path: Path):
    clock = [0.0]
    sink = make_sink(tmp_path, clock)
    sink.write(PACKET20, 1001)
    clock[0] = 60.0  # away a minute, same user id, same file
    sink.write(PACKET20, 1001)
    sink.cleanup()
    [(_, silence)] = gaps_of(sink, "1001")
    assert silence / BYTES_PER_SECOND == pytest.approx(60.0 - 0.02, abs=0.001)


def test_resumed_sink_pads_the_outage(tmp_path: Path):
    clock = [0.0]
    first = make_sink(tmp_path, clock)
    first.write(PACKET20, 1001)
    first.cleanup()

    clock[0] = 100.0  # the sink was gone from 0.02 s to 100 s of session time
    resumed = DiskSink(
        tmp_path / "raw",
        clock=lambda: clock[0],
        base_offset=100.0,
        known_offsets=first.offsets,
    )
    resumed.write(PACKET20, 1001)
    resumed.cleanup()

    [(position, silence)] = gaps_of(resumed, "1001")
    assert position == len(PACKET20)
    assert silence / BYTES_PER_SECOND == pytest.approx(100.0 - 0.02, abs=0.001)


def test_two_resumes_do_not_double_count_earlier_gaps(tmp_path: Path):
    clock = [0.0]
    sink = make_sink(tmp_path, clock)
    sink.write(PACKET20, 1001)
    clock[0] = 10.0
    sink.write(PACKET20, 1001)  # gap of ~9.98 s recorded here
    sink.cleanup()

    clock[0] = 20.0
    resumed = DiskSink(
        tmp_path / "raw", clock=lambda: clock[0], base_offset=20.0, known_offsets=sink.offsets
    )
    resumed.write(PACKET20, 1001)
    resumed.cleanup()

    total = sum(silence for _, silence in gaps_of(resumed, "1001"))
    # Three packets of speech plus all the silence must reach the session time of the last packet.
    assert (3 * len(PACKET20) + total) / BYTES_PER_SECOND == pytest.approx(20.0 + 0.02, abs=0.01)


def test_packet_with_rtp_metadata_is_handled_like_any_other(tmp_path: Path):
    clock = [0.0]
    sink = make_sink(tmp_path, clock)
    voice = SimpleNamespace(
        pcm=PACKET20,
        source=SimpleNamespace(id=1001, bot=False),
        packet=SimpleNamespace(ssrc=7, timestamp=960, sequence=1),
    )
    sink.write(voice, None)
    assert sink.bytes_written("1001") == len(PACKET20)
    sink.cleanup()


def test_finished_track_length_matches_session_time(tmp_path: Path):
    """Sink to finalize: 30 s of session with a 10 s pause yields a 30 s file."""
    import wave

    from dnd_bot import paths
    from dnd_bot.finalize import finalize_session_audio

    raw = paths.raw_dir(tmp_path, "s1")
    clock = [0.0]
    sink = DiskSink(raw, clock=lambda: clock[0])

    def speak(seconds: float) -> None:
        for _ in range(round(seconds / 0.02)):
            sink.write(PACKET20, 1001)
            clock[0] += 0.02

    speak(10.0)  # 0-10 s
    clock[0] = 20.0  # 10 s of silence: nothing arrives
    speak(10.0)  # 20-30 s
    sink.cleanup()

    written, warnings = finalize_session_audio(tmp_path, "s1", "wav")

    assert warnings == []
    with wave.open(str(written[0]), "rb") as reader:
        seconds = reader.getnframes() / reader.getframerate()
    assert seconds == pytest.approx(30.0, abs=0.05)


def test_the_receive_path_own_voice_data_is_accepted(tmp_path: Path):
    """The pinned py-cord hands `write` its own VoiceData, not bytes."""
    voice_module = pytest.importorskip("discord.voice")
    voice_data = getattr(voice_module, "VoiceData", None)
    if voice_data is None:
        pytest.skip("this py-cord predates VoiceData")

    clock = [0.0]
    sink = make_sink(tmp_path, clock)
    speaker = SimpleNamespace(id=1001, bot=False)
    packet = SimpleNamespace(ssrc=7, timestamp=960, sequence=1, decrypted_data=b"")
    sink.write(voice_data(packet, speaker, pcm=PACKET20), speaker)
    clock[0] = 4.0
    for _ in range(4):
        sink.write(voice_data(packet, speaker, pcm=PACKET20), speaker)
        clock[0] += 0.02
    sink.cleanup()

    assert sink.bytes_written("1001") == 5 * len(PACKET20)
    assert len(gaps_of(sink, "1001")) == 1
