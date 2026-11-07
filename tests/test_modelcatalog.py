import os

import pytest

import datasize
import modelcatalog as c

GB = c.GB
BIG = c.Specs("Reference laptop", cores=8, threads=16, ram_total=16 * GB, ram_available=8 * GB, disk_free=200 * GB)
SMALL = c.Specs("Small laptop", cores=2, threads=4, ram_total=8 * GB, ram_available=3 * GB, disk_free=200 * GB)
TINY_RAM = c.Specs("Old laptop", cores=4, threads=4, ram_total=4 * GB, ram_available=2 * GB, disk_free=200 * GB)


def test_catalog_is_consistent():
    ids = [m.id for m in c.MODELS]
    assert len(ids) == len(set(ids)) and c.DEFAULT in ids
    for m in c.MODELS:
        assert m.download_mb > 0 and m.ram_gb > 0 and m.speed > 0
        assert (m.engine == "whisper") == bool(m.repo) == bool(m.model)
        assert c.by_engine(m.engine, m.model) is m


def test_reference_speeds_match_the_measurements():
    assert c.speed(c.get("whisper-small"), BIG) == pytest.approx(5.7)
    assert c.speed(c.get("whisper-medium"), BIG) == pytest.approx(1.9)
    assert c.speed(c.get("parakeet"), BIG) == pytest.approx(7.0)
    # fewer real cores: slower; afterwards (more threads): faster than live
    assert c.speed(c.get("whisper-small"), SMALL) < 5.7
    assert c.speed(c.get("whisper-small"), BIG, live=False) > 5.7


def test_fit_levels():
    assert c.fit(c.get("whisper-small"), BIG).level == "great"
    assert c.fit(c.get("whisper-medium"), BIG).level == "ok"
    assert c.fit(c.get("whisper-large-v3"), BIG).level == "slow"
    assert c.fit(c.get("whisper-medium"), SMALL).level == "slow"
    assert c.fit(c.get("whisper-large-v3"), TINY_RAM).label == "Not enough memory"
    full = c.Specs("x", 8, 16, 16 * GB, 8 * GB, disk_free=1 * GB)
    assert c.fit(c.get("whisper-medium"), full).label == "Not enough disk space"
    assert c.fit(c.get("whisper-medium"), full, downloaded=True).level != "no"   # already on disk


def test_suggestions():
    s = c.suggestions(BIG)
    assert s["parakeet"][0] == "Best for English"
    assert s["whisper-small"][0] == "Best for other languages"    # most accurate that runs well live
    assert all(c.get(mid).id for mid in c.suggestions(SMALL))


def test_processing_factor_matches_old_estimates():
    assert c.processing_factor(c.get("whisper-medium"), BIG) == pytest.approx(0.5, abs=0.06)
    assert c.processing_factor(c.get("whisper-small"), BIG) == pytest.approx(0.25, abs=0.05)


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """A fake Hugging Face cache laid out like huggingface_hub 2.x: big files in a shared hub/blobs folder."""
    root = tmp_path / "hub"
    monkeypatch.setattr(c, "HF_HUB", root)
    monkeypatch.setattr(c, "MODELS_DIR", tmp_path / "models")
    shared = root / "blobs" / "ab"
    shared.mkdir(parents=True)

    def make(repo, files):
        d = root / ("models--" + repo.replace("/", "--"))
        snap = d / "snapshots" / "rev"
        (d / "blobs").mkdir(parents=True)
        snap.mkdir(parents=True)
        for name, (blob, size) in files.items():
            target = shared / blob
            if not target.exists():
                target.write_bytes(b"x" * size)
            os.symlink(target, d / "blobs" / blob)
            os.symlink(d / "blobs" / blob, snap / name)
        return d
    return root, make


def test_delete_frees_shared_files_only_when_unused(hub):
    root, make = hub
    make("Systran/faster-whisper-base", {"model.bin": ("base-bin", 1000), "config.json": ("cfg", 10)})
    make("Systran/faster-whisper-tiny", {"model.bin": ("tiny-bin", 500), "config.json": ("cfg", 10)})  # shares cfg
    base, tiny = c.get("whisper-base"), c.get("whisper-tiny")
    assert c.is_downloaded(base) and c.is_downloaded(tiny)
    assert c.size_on_disk(base) == 1010
    c.delete("whisper-base")
    assert not c.is_downloaded(base) and c.is_downloaded(tiny)
    assert not (root / "blobs/ab/base-bin").exists()
    assert (root / "blobs/ab/cfg").exists()             # still used by tiny
    assert [m.id for m in c.downloaded()] == ["whisper-tiny"]


def test_half_downloaded_model_is_not_downloaded(hub):
    root, _make = hub
    (root / "models--Systran--faster-whisper-small" / "blobs").mkdir(parents=True)
    assert not c.is_downloaded(c.get("whisper-small"))
    c.delete("whisper-small")
    assert not (root / "models--Systran--faster-whisper-small").exists()


def test_delete_refuses_unknown_models(hub):
    with pytest.raises(KeyError):
        c.delete("../../etc")


def test_datasize_labels_catalog_models_as_used(hub, monkeypatch):
    root, make = hub
    make("Systran/faster-whisper-base", {"model.bin": ("base-bin", 1000), "config.json": ("cfg", 10)})
    make("someone/faster-whisper-other", {"model.bin": ("other", 5)})
    monkeypatch.setattr(datasize, "HF_HUB", root)
    monkeypatch.setattr(datasize, "DATA", root.parent / "nothing")
    labels = {label: used for label, _p, _s, used in datasize.models()}
    assert labels == {"Whisper base": True, "faster-whisper-other (not used by MintoMeet)": False}


def test_detect_reads_this_computer():
    s = c.detect()
    assert s.threads >= 1 and s.cores >= 1 and s.ram_total > 0
