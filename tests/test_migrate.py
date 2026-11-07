import migrate


def test_moves_old_folders_and_claude_sessions(tmp_path):
    old_m, new_m = tmp_path / "MeetScribe", tmp_path / "MintoMeet"
    old_c, new_c = tmp_path / "cfg/meetscribe", tmp_path / "cfg/mintomeet"
    (old_m / "2026-10-26_144231").mkdir(parents=True)
    (old_m / "2026-10-26_144231/transcript.txt").write_text("[00:00:01] You: hi")
    old_c.mkdir(parents=True)
    (old_c / "voices.json").write_text("{}")
    projects = tmp_path / "projects"
    session = projects / (migrate.claude_dir(old_m / "2026-10-26_144231"))
    session.mkdir(parents=True)
    other = projects / "-home-someone-else"
    other.mkdir()

    moved = migrate.run([(old_m, new_m), (old_c, new_c)], projects, check_running=False)

    assert moved == [(old_m, new_m), (old_c, new_c)]
    assert (new_m / "2026-10-26_144231/transcript.txt").read_text() == "[00:00:01] You: hi"
    assert (new_c / "voices.json").exists() and not old_c.exists() and not old_m.exists()
    assert (projects / migrate.claude_dir(new_m / "2026-10-26_144231")).is_dir()
    assert not session.exists() and other.is_dir()


def test_never_overwrites_existing_new_folder(tmp_path):
    old, new = tmp_path / "MeetScribe", tmp_path / "MintoMeet"
    (old / "a").mkdir(parents=True)
    (new / "b").mkdir(parents=True)
    assert migrate.run([(old, new)], tmp_path / "none", check_running=False) == []
    assert (old / "a").is_dir() and (new / "b").is_dir()


def test_nothing_to_do(tmp_path):
    assert migrate.run([(tmp_path / "x", tmp_path / "y")], tmp_path, check_running=False) == []
