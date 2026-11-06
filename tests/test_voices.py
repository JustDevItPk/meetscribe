import json


def emb(*head, dim=8):
    v = list(head) + [0.0] * (dim - len(head))
    return v


def test_learn_is_idempotent_and_moves_moments_between_people(voice_store):
    v = voice_store
    a = v.clip("2026-11-02_131011", "Them", 10.0, 14.0, "hello there", emb(1, 0))
    v.learn("Alex", [a])
    v.learn("Alex", [a])                                  # same moment again: no double counting
    assert len(v.load_clips()["Alex"]["clips"]) == 1
    assert v.load()["Alex"]["seconds"] == 4.0

    shifted = v.clip("2026-11-02_131011", "Them", 10.4, 14.2, "hello there", emb(1, 0))
    v.learn("Sam", [shifted])                            # reassigned (timing shifted by "Transcribe again")
    assert "Alex" not in v.load()                         # Alex had nothing else, so the voice is gone
    assert len(v.load_clips()["Sam"]["clips"]) == 1


def test_unlearn_removes_from_every_voice(voice_store):
    v = voice_store
    c = v.clip("m", "Them", 0, 3, "hi", emb(0, 1))
    v.learn("Alex", [c, v.clip("m", "Them", 5, 9, "more", emb(0, 1))])
    v.unlearn([c])
    assert [x["start"] for x in v.load_clips()["Alex"]["clips"]] == [5]


def test_remove_and_restore(voice_store):
    v = voice_store
    clips = [v.clip("m", "Them", i * 5, i * 5 + 3, f"line {i}", emb(1, i * 0.1)) for i in range(3)]
    v.learn("Alex", clips)
    before = v.load()["Alex"]["emb"]
    removed = v.remove_clips("Alex", {clips[0]["id"]})
    assert len(v.load_clips()["Alex"]["clips"]) == 2
    v.restore_clips("Alex", removed)
    assert len(v.load_clips()["Alex"]["clips"]) == 3
    assert all(abs(x - y) < 1e-9 for x, y in zip(v.load()["Alex"]["emb"], before))


def test_review_flags_a_clip_that_sounds_like_someone_else(voice_store):
    v = voice_store
    v.learn("Alex", [v.clip("m", "Them", i * 5, i * 5 + 4, "alex", emb(1, 0.05 * i)) for i in range(4)])
    v.learn("Sam", [v.clip("m", "Them", 100 + i * 5, 104 + i * 5, "sam", emb(0, 1)) for i in range(3)])
    v.learn("Alex", [v.clip("m", "Them", 200, 204, "actually sam", emb(0.05, 1))])
    flagged = [c for c in v.review("Alex") if c["suspect"]]
    assert [c["text"] for c in flagged] == ["actually sam"]
    assert flagged[0]["other"] == "Sam"


def test_match_picks_the_closest_known_voice(voice_store):
    v = voice_store
    profiles = {"Alex": {"emb": emb(1, 0)}, "Sam": {"emb": emb(0, 1)}}
    assert v.match([emb(0.9, 0.1), emb(0.1, 0.9)], profiles) == {0: "Alex", 1: "Sam"}
    assert v.match([emb(1, 1)], profiles) == {}           # too close to both to decide


def test_migrate_rebuilds_clips_from_meetings(voice_store, tmp_path):
    v = voice_store
    v.save({"Alex": {"emb": emb(1, 0), "seconds": 4.0}})
    meeting = tmp_path / "meetings" / "2026-11-02_131011"
    meeting.mkdir(parents=True)
    (meeting / "speaker_map.json").write_text(json.dumps({"00:00:10|hello there": "Alex"}))
    (meeting / "segments.json").write_text(json.dumps(
        [{"start": 10.2, "end": 14.2, "stream": "Them", "text": "hello there", "emb": emb(1, 0)}]))
    assert v.migrate(tmp_path / "meetings") is True
    entry = v.load_clips()["Alex"]
    assert len(entry["clips"]) == 1 and entry["legacy"] is None
    assert v.migrate(tmp_path / "meetings") is False      # only once
