"""Hallucination filtering and prompt priming - the Turkish accuracy levers."""

from __future__ import annotations

from dnd_bot.transcription import (
    RawSegment,
    build_initial_prompt,
    filter_hallucinations,
    is_hallucination,
    normalize_for_match,
)


def seg(text: str, start: float = 0.0, **kwargs) -> RawSegment:
    return RawSegment(start=start, end=start + 1.0, text=text, **kwargs)


def test_normalization_folds_turkish_characters():
    assert normalize_for_match("Altyazı M.K.") == "altyazi m k"
    assert normalize_for_match("  İZLEDİĞİNİZ   İÇİN  ") == "izlediginiz icin"


def test_known_subtitle_artifacts_are_recognized():
    assert is_hallucination("Altyazı M.K.")
    assert is_hallucination("altyazı m.k.")
    assert is_hallucination("Abone olmayı unutmayın!")
    assert is_hallucination("Thanks for watching!")
    assert is_hallucination("   ")


def test_real_speech_is_not_recognized_as_an_artifact():
    assert not is_hallucination("Zar attım, on sekiz geldi.")
    assert not is_hallucination("Altyazıları kapatabilir miyiz?")  # longer, real sentence
    assert not is_hallucination("Teşekkürler Thorin, kapıyı açıyorum.")


def test_artifacts_are_dropped_and_counted():
    segments = [
        seg("Altyazı M.K."),
        seg("Kapıyı açıyorum.", 2.0),
        seg("Abone olmayı unutmayın", 4.0),
    ]
    kept, dropped = filter_hallucinations(segments)
    assert [s.text for s in kept] == ["Kapıyı açıyorum."]
    assert dropped == 2


def test_low_confidence_silence_is_dropped():
    noisy = seg("hmm", avg_logprob=-1.5, no_speech_prob=0.9)
    real = seg("Zar attım.", 2.0, avg_logprob=-0.3, no_speech_prob=0.05)
    kept, dropped = filter_hallucinations([noisy, real])
    assert [s.text for s in kept] == ["Zar attım."]
    assert dropped == 1


def test_low_confidence_alone_is_not_enough_to_drop():
    # Quiet but real speech: bad logprob, but Whisper is sure it is speech.
    quiet = seg("Evet.", avg_logprob=-1.8, no_speech_prob=0.1)
    kept, dropped = filter_hallucinations([quiet])
    assert dropped == 0 and len(kept) == 1


def test_stuck_repetition_loops_are_truncated():
    segments = [seg("Evet evet evet.", float(i)) for i in range(6)]
    kept, dropped = filter_hallucinations(segments)
    assert len(kept) == 3  # the run is kept up to max_repeats, then cut
    assert dropped == 3


def test_repetition_counter_resets_on_new_text():
    segments = [
        seg("Evet.", 0.0),
        seg("Evet.", 1.0),
        seg("Kapı açıldı.", 2.0),
        seg("Evet.", 3.0),
        seg("Evet.", 4.0),
    ]
    kept, dropped = filter_hallucinations(segments)
    assert dropped == 0
    assert len(kept) == 5


def test_prompt_names_the_characters():
    prompt = build_initial_prompt(["Thorin", "Elenya", "Thorin"])
    assert "Thorin" in prompt and "Elenya" in prompt
    assert prompt.count("Thorin") == 1  # de-duplicated
    assert "Dungeons & Dragons" in prompt
    assert "Turkce" in prompt


def test_prompt_survives_no_speakers():
    prompt = build_initial_prompt([])
    assert "Dungeons & Dragons" in prompt
    assert "Karakterler" not in prompt


def test_prompt_appends_operator_supplied_vocabulary():
    prompt = build_initial_prompt(["Thorin"], "Yer isimleri: Neverwinter, Waterdeep.")
    assert "Neverwinter" in prompt


def test_prompt_is_capped_for_whispers_context_window():
    prompt = build_initial_prompt([f"Karakter{i}" for i in range(200)])
    assert len(prompt) <= 600
    assert not prompt.endswith(" ")


def test_blank_labels_are_ignored():
    prompt = build_initial_prompt(["", "   ", "Thorin"])
    assert "Karakterler ve oyuncular: Thorin." in prompt
