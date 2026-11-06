"""Tests cover the pure-Python modules (no GTK, no speech engines), so they run anywhere with pytest."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


@pytest.fixture
def voice_store(tmp_path, monkeypatch):
    """voices.py writing to a temporary config folder instead of ~/.config/meetscribe."""
    import voices
    monkeypatch.setattr(voices, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(voices, "FILE", tmp_path / "voices.json")
    monkeypatch.setattr(voices, "CLIPS_FILE", tmp_path / "voice_clips.json")
    return voices
