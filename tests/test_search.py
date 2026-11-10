import datetime
import json

import meetings
import search

TODAY = datetime.date(2026, 11, 10)


def make(root, name, lines, title=None, tags=None, chat=None, bookmarks=None):
    folder = root / name
    folder.mkdir()
    (folder / "transcript.txt").write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    if title or tags:
        meetings.save_meta(folder, **({"title": title} if title else {}), **({"tags": tags} if tags else {}))
    if chat:
        (folder / "chat.json").write_text(json.dumps({"messages": chat}), encoding="utf-8")
    if bookmarks:
        meetings.save_bookmarks(folder, bookmarks)
    return folder


def library(tmp_path):
    make(tmp_path, "2026-11-02_100000", [
        "[00:00:05] Harris: Let's go over the budget for the café launch.",
        "[00:01:10] You: The budget looks fine to me.",
        "[00:02:00] Jawad: Planning is on track.",
    ], title="Weekly sync", tags=["Client", "weekly"],
        chat=[{"role": "user", "text": "What was decided?"},
              {"role": "assistant", "text": "The **budget** was approved.", "agent": "Claude"}],
        bookmarks=[{"t": 70, "note": "budget sign-off"}])
    make(tmp_path, "2026-11-09_150000", [
        "[00:00:03] Alan: Can you explain the deadline?",
        "[00:00:20] You: Friday, and the budget is separate.",
    ], tags=["internal"])
    make(tmp_path, "2026-10-20_090000", ["[00:00:01] Them: Hello everyone."])
    idx = search.Index(tmp_path)
    idx.refresh()
    return idx


def run(idx, text="", **kw):
    return idx.search(search.Query(text, today=TODAY, **kw))


def test_fold_and_spans_handle_case_and_accents():
    assert search.fold("Café ÉLAN") == "cafe elan"
    text = "Straße and CAFÉ"
    assert [text[a:b] for a, b in search.spans(text, ["strasse", "cafe"])] == ["Straße", "CAFÉ"]


def test_words_match_at_the_start_of_words_only():
    assert search.spans("planning to explain", ["plan"]) == [(0, 4)]


def test_every_word_must_be_in_the_meeting(tmp_path):
    idx = library(tmp_path)
    names = [r.doc.folder.name for r in run(idx, "budget")]
    assert set(names) == {"2026-11-02_100000", "2026-11-09_150000"}
    assert [r.doc.folder.name for r in run(idx, "budget deadline")] == ["2026-11-09_150000"]
    assert run(idx, "budget nonsenseword") == []


def test_hits_come_from_transcript_bookmarks_and_chat(tmp_path):
    idx = library(tmp_path)
    r = next(r for r in run(idx, "budget") if r.doc.folder.name == "2026-11-02_100000")
    assert [(h.source, h.who) for h in r.hits] == [
        ("transcript", "Harris"), ("transcript", "You"), ("bookmark", "Bookmark"), ("chat", "Claude")]
    assert r.hits[-1].text == "The budget was approved."
    first = r.hits[0]
    assert [first.text[a:b] for a, b in first.marks] == ["budget"]
    assert first.when == "00:00:05"


def test_phrases_and_accent_insensitive_search(tmp_path):
    idx = library(tmp_path)
    assert len(run(idx, '"cafe launch"')) == 1
    assert run(idx, '"launch cafe"') == []


def test_speaker_filter_limits_lines_and_meetings(tmp_path):
    idx = library(tmp_path)
    res = run(idx, "budget from:harris")
    assert len(res) == 1 and [h.who for h in res[0].hits if h.source == "transcript"] == ["Harris"]
    only = run(idx, "", people={"Alan"})
    assert [r.doc.folder.name for r in only] == ["2026-11-09_150000"] and only[0].people_lines == 1
    assert run(idx, "from:Nobody") == []


def test_tag_and_date_filters(tmp_path):
    idx = library(tmp_path)
    assert [r.doc.folder.name for r in run(idx, "tag:client")] == ["2026-11-02_100000"]
    assert [r.doc.folder.name for r in run(idx, "", tags={"CLIENT", "Weekly"})] == ["2026-11-02_100000"]
    assert run(idx, "", tags={"client", "internal"}) == []
    assert [r.doc.folder.name for r in run(idx, "after:2026-11-05")] == ["2026-11-09_150000"]
    assert [r.doc.folder.name for r in run(idx, "before:2026-10")] == ["2026-10-20_090000"]
    assert [r.doc.folder.name for r in run(idx, "on:2026-11-02")] == ["2026-11-02_100000"]
    first, last = search.date_preset("week", TODAY)
    assert [r.doc.folder.name for r in run(idx, "", after=first, before=last)] == ["2026-11-09_150000"]


def test_titles_and_tags_are_searched(tmp_path):
    idx = library(tmp_path)
    r = run(idx, "weekly")
    assert len(r) == 1 and r[0].title_marks and r[0].tag_hits == ["weekly"]
    assert run(idx, "weekly in:chat") == []


def test_where_filter(tmp_path):
    idx = library(tmp_path)
    r = run(idx, "budget in:chat")
    assert len(r) == 1 and {h.source for h in r[0].hits} == {"chat"}


def test_unknown_operators_and_times_are_plain_words():
    q = search.Query("from:Harris 10:30 http://x.io budget", today=TODAY)
    assert q.people == {"Harris"} and q.terms == ["10:30", "http://x.io", "budget"]
    q = search.Query("after:soon", today=TODAY)
    assert q.after is None and q.terms == ["after:soon"]


def test_only_filters_lists_meetings_newest_first(tmp_path):
    idx = library(tmp_path)
    names = [r.doc.folder.name for r in run(idx, "", after=datetime.date(2026, 10, 1))]
    assert names == ["2026-11-09_150000", "2026-11-02_100000", "2026-10-20_090000"]


def test_index_rereads_only_changed_meetings(tmp_path):
    idx = library(tmp_path)
    doc = next(d for _s, d in idx.docs.values() if d.folder.name == "2026-10-20_090000")
    idx.refresh()
    assert next(d for _s, d in idx.docs.values() if d.folder.name == "2026-10-20_090000") is doc
    path = tmp_path / "2026-10-20_090000" / "transcript.txt"
    path.write_text("[00:00:01] Them: A brand new zebra line.\n", encoding="utf-8")
    idx.refresh()
    assert run(idx, "zebra")


def test_people_lists_named_speakers(tmp_path):
    idx = library(tmp_path)
    people = idx.people()
    assert "Them" not in people and people["You"] == 2 and people["Harris"] == 1


def test_snippet_cuts_around_the_match():
    text = "word " * 100 + "budget " + "word " * 100
    piece, marks = search.snippet(text, search.spans(text, ["budget"]), width=60)
    assert piece.startswith("…") and piece.endswith("…") and len(piece) <= 62
    assert [piece[a:b] for a, b in marks] == ["budget"]


def test_tags_are_normalized_and_counted(tmp_path):
    f = tmp_path / "2026-11-02_100000"
    f.mkdir()
    assert meetings.set_tags(f, [" Client ", "client", "a,b", ""]) == ["Client", "a b"]
    assert meetings.add_tag(f, "Weekly") == ["Client", "a b", "Weekly"]
    assert meetings.remove_tag(f, "CLIENT") == ["a b", "Weekly"]
    assert meetings.count_tags([["Client"], ["client", "x"], ["Client"]]) == {"Client": 3, "x": 1}


def test_rename_merges_and_delete_returns_folders(tmp_path):
    a = make(tmp_path, "2026-11-01_100000", ["[00:00:01] You: hi"], tags=["Clients", "Acme"])
    b = make(tmp_path, "2026-11-02_100000", ["[00:00:01] You: hi"], tags=["clients"])
    assert meetings.rename_tag(tmp_path, "clients", "Acme") == 2
    assert meetings.tags(a) == ["Acme"] and meetings.tags(b) == ["Acme"]
    assert set(meetings.delete_tag(tmp_path, "acme")) == {a, b}
    assert meetings.delete_tag(tmp_path, "acme") == []
    assert meetings.tags(a) == [] and meetings.tags(b) == []


def test_export_lists_tags(tmp_path):
    f = make(tmp_path, "2026-11-02_100000", ["[00:00:01] You: hi"], tags=["Client"])
    assert "Tags: Client" in meetings.build_markdown(f, [], [])


def test_find_bar_terms_ignore_filters():
    assert search.plain_terms('from:harris "Next Steps" ok') == ["from:harris", "next steps", "ok"]


def test_markdown_is_removed_from_agent_answers():
    md = "## Decisions\n\n- **Budget** approved by *Harris*\n```\ncode\n```\nUse `export`"
    assert search.plain(md) == "Decisions\n\n• Budget approved by Harris\ncode\nUse export"   # code stays searchable
