"""Learned voice profiles: one averaged voiceprint per named speaker, shared by every meeting.

Pure Python on purpose: the window (system Python) and the recorder (its own venv) both use it.
"""
import json
import math
import os
import pathlib

CONFIG_DIR = pathlib.Path(os.environ.get("MEETSCRIBE_CONFIG_DIR", pathlib.Path.home() / ".config/meetscribe"))
FILE = CONFIG_DIR / "voices.json"
MATCH = 0.50        # cosine similarity needed to recognise a known voice
MARGIN = 0.05       # ...and how much better than the second-best profile it must be
MAX_WEIGHT = 600.0  # seconds; caps a profile's history so it keeps adapting to new meetings


def load():
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("emb")}
    except (OSError, ValueError):
        return {}


def save(profiles):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(profiles), encoding="utf-8")


def normalize(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b)) / ((math.sqrt(sum(x * x for x in a)) or 1) * (math.sqrt(sum(y * y for y in b)) or 1))


def learn(name, samples):
    """Fold (embedding, seconds) samples into `name`'s profile. Returns seconds learned."""
    samples = [(e, s) for e, s in samples if e]
    if not samples:
        return 0.0
    profiles = load()
    dim = len(samples[0][0])
    old = profiles.get(name)
    acc, weight = [0.0] * dim, 0.0
    if old and len(old["emb"]) == dim:
        w = min(old.get("seconds", 1.0), MAX_WEIGHT)
        acc = [x * w for x in old["emb"]]
        weight = w
    for emb, secs in samples:
        acc = [a + x * secs for a, x in zip(acc, emb)]
        weight += secs
    profiles[name] = {"emb": normalize(acc), "seconds": round(weight, 1)}
    save(profiles)
    return sum(s for _, s in samples)


def forget(name):
    profiles = load()
    if profiles.pop(name, None) is not None:
        save(profiles)


def match(centroids, profiles=None):
    """Map cluster index -> profile name for clusters that clearly sound like a known person.

    Each profile is used at most once (the most similar cluster wins).
    """
    profiles = load() if profiles is None else profiles
    pairs = []
    for i, c in enumerate(centroids):
        scores = sorted(((cosine(c, p["emb"]), name) for name, p in profiles.items()), reverse=True)
        if scores and scores[0][0] >= MATCH and (len(scores) == 1 or scores[0][0] - scores[1][0] >= MARGIN):
            pairs.append((scores[0][0], i, scores[0][1]))
    result, used = {}, set()
    for _score, i, name in sorted(pairs, reverse=True):
        if name not in used and i not in result:
            result[i] = name
            used.add(name)
    return result
