"""The soundboard mixer: frame arithmetic, looping, and reporting a track's end."""

from __future__ import annotations

from array import array

from dnd_bot.mixer import FRAME_BYTES, MAIN_HANDOVER_FRAMES, SILENCE, Layer, MixerSource, add_frames


def frame(value: int) -> bytes:
    return array("h", [value] * (FRAME_BYTES // 2)).tobytes()


class Frames:
    """A source that yields a fixed number of constant frames."""

    def __init__(self, value: int, count: int):
        self.value = value
        self.left = count
        self.cleaned = False
        self.volume = 1.0

    def read(self):
        if self.left <= 0:
            return b""
        self.left -= 1
        return frame(self.value)

    def cleanup(self):
        self.cleaned = True


def layer(source, kind="sfx", reopen=None, layer_id="l1"):
    return Layer(layer_id, kind, "music/sfx/x.ogg", "x", source, 1.0, reopen)


def samples(data: bytes) -> set[int]:
    return set(array("h", data))


def test_add_frames_sums_and_saturates():
    assert samples(add_frames(frame(100), frame(23))) == {123}
    assert samples(add_frames(frame(30000), frame(30000))) == {32767}
    assert samples(add_frames(frame(-30000), frame(-30000))) == {-32768}


def test_short_frames_are_padded_with_silence():
    mixed = add_frames(frame(5)[:100], b"")
    assert len(mixed) == FRAME_BYTES
    assert array("h", mixed)[0] == 5
    assert array("h", mixed)[-1] == 0


def test_main_and_layers_are_mixed():
    mixer = MixerSource(main=Frames(100, 5))
    mixer.add_layer(layer(Frames(7, 5)))
    assert samples(mixer.read()) == {107}


def test_main_end_is_reported_once_and_silence_holds_for_the_next_track():
    ended = []
    mixer = MixerSource(main=Frames(100, 1), on_main_end=ended.append)
    assert samples(mixer.read()) == {100}
    # The track ran out: reported, and the mixer holds silence instead of ending.
    assert mixer.read() == SILENCE
    assert ended == [None]
    assert mixer.read() == SILENCE
    assert ended == [None]

    assert mixer.set_main(Frames(9, 3), ended.append)
    assert samples(mixer.read()) == {9}


def test_handover_expires_and_the_mixer_closes():
    main = Frames(1, 0)
    mixer = MixerSource(main=main, on_main_end=lambda _e: None)
    outputs = [mixer.read() for _ in range(MAIN_HANDOVER_FRAMES + 1)]
    assert outputs[-1] == b""
    assert mixer.closed
    assert main.cleaned
    assert not mixer.set_main(Frames(1, 1), lambda _e: None)


def test_end_handover_closes_straight_away():
    mixer = MixerSource(main=Frames(1, 0), on_main_end=lambda _e: None)
    assert mixer.read() == SILENCE
    mixer.end_handover()
    assert mixer.read() == b""


def test_one_shot_layers_end_and_are_cleaned_up():
    shot = Frames(3, 1)
    mixer = MixerSource()
    mixer.add_layer(layer(shot))
    assert samples(mixer.read()) == {3}
    assert mixer.read() == b""
    assert shot.cleaned
    assert mixer.snapshot() == []


def test_ambience_loops_by_reopening():
    opened = []

    def reopen():
        source = Frames(4, 2)
        opened.append(source)
        return source

    first = Frames(4, 1)
    mixer = MixerSource()
    mixer.add_layer(layer(first, kind="ambience", reopen=reopen))
    for _ in range(6):
        assert samples(mixer.read()) == {4}
    assert first.cleaned
    assert len(opened) >= 2


def test_a_loop_that_cannot_reopen_ends_without_stopping_the_music():
    def broken():
        raise OSError("gone")

    mixer = MixerSource(main=Frames(10, 5))
    mixer.add_layer(layer(Frames(1, 0), kind="ambience", reopen=broken))
    assert samples(mixer.read()) == {10}
    assert mixer.snapshot() == []
    assert samples(mixer.read()) == {10}


def test_paused_main_is_silent_but_layers_play():
    main = Frames(50, 5)
    mixer = MixerSource(main=main)
    mixer.add_layer(layer(Frames(2, 5)))
    mixer.main_paused = True
    assert samples(mixer.read()) == {2}
    assert main.left == 5


def test_clear_main_does_not_report_an_end():
    ended = []
    main = Frames(1, 5)
    mixer = MixerSource(main=main, on_main_end=ended.append)
    mixer.add_layer(layer(Frames(2, 5)))
    mixer.clear_main()
    assert samples(mixer.read()) == {2}
    assert ended == []
    assert main.cleaned


def test_a_failing_layer_does_not_break_the_mix():
    class Broken(Frames):
        def read(self):
            raise RuntimeError("decoder died")

    mixer = MixerSource(main=Frames(6, 5))
    mixer.add_layer(layer(Broken(0, 0)))
    assert samples(mixer.read()) == {6}


def test_cleanup_releases_everything():
    main, shot = Frames(1, 5), Frames(1, 5)
    mixer = MixerSource(main=main)
    mixer.add_layer(layer(shot))
    mixer.cleanup()
    assert main.cleaned and shot.cleaned
    assert mixer.closed
    assert not mixer.add_layer(layer(Frames(1, 1)))
