"""The committed `lean/MLS/Vectors.lean` is what `tests/lean/vectors.py` generates (D43): the
generator asserts, while generating, that the Python evaluator and encoder agree on every
vector; the Lean build asserts the transcription agrees with both."""
from __future__ import annotations

from tests.lean import vectors


def test_vectors_file_is_current():
    expected = vectors.generate()
    assert vectors.OUT.read_text() == expected, "regenerate with: uv run python -m tests.lean.vectors"


def test_vectors_cover_every_layer():
    text = vectors.OUT.read_text()
    assert text.count("#guard Pred.eval") == vectors.N_PRED + vectors.N_GROUP
    assert text.count("#guard Pred.tf") == vectors.N_PRED + vectors.N_GROUP
    assert text.count("#guard Rules.") == vectors.N_OUTCOME
    for layer in ("Rules.visible", "Rules.insert", "Rules.update", "Rules.delete"):
        assert f"#guard {layer}" in text
    assert "TV.unk" in text and "Outcome.filtered" in text and "Outcome.error" in text
