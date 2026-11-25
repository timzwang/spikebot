"""Rule compiler (spikebot/rules.py): saved notes become settings, built-in or new-code requests,
re-read when the editor's capabilities change; one-off deploy requests (the /trend after a deploy)."""
from __future__ import annotations

import asyncio
import io
import json

from spikebot import discord_bot as DB
from spikebot import rules as RC
from spikebot import style as S

from .test_feedback import FakeClaude, row
from .test_review import bot_for, new_item


def res(n, verdict, settings=(), needs="", reason="ok"):
    return {"n": n, "verdict": verdict, "settings": list(settings), "reason": reason, "needs": needs}


def notes_profile(store):
    prof = store.profile()
    prof.add_rule("x", "", "tokyo_girl", [], ["Keep every shot completely still",
                                               "Show the release date in the last second",
                                               "Cut on the beat"], ["Licensed music inside the file"])
    prof.add_rule("y", "", "lacy", [], ["Post at 7pm"], [])
    prof.save()
    return prof


def test_pending_and_version():
    es = [{"n": 1, "kind": "note", "on": True}, {"n": 2, "kind": "setting", "on": True},
          {"n": 3, "kind": "unsupported", "on": True, "checked": "v1"}, {"n": 4, "kind": "note", "on": False}]
    assert [e["n"] for e in RC.pending(es, "v1")] == [1]
    assert [e["n"] for e in RC.pending(es, "v2")] == [1, 3]
    assert len(RC.capabilities_version()) == 12


def test_apply_turns_notes_into_settings_and_marks_the_rest(tmp_path):
    store, _ = new_item(tmp_path)
    prof = notes_profile(store)
    ns = {e["text"]: e["n"] for e in prof.entries()}
    still, date, beat, music, post = (ns["Keep every shot completely still"], ns["Show the release date in the last second"],
                                      ns["Cut on the beat"], ns["Licensed music inside the file"], ns["Post at 7pm"])
    changes = RC.apply(prof, "v1", [
        res(still, "settings", [row("motion", "none", mode="all")]),
        res(date, "needs_code", needs="end card with the release date, setting end_date=on"),
        res(beat, "built_in"),
        res(music, "settings", [row("grade", "rainbow")]),   # not a real option -> needs code
        res(post, "not_editor"),
    ], now=5)
    by = {c["n"]: c for c in changes}
    e = prof.entry(still)
    assert not e["on"] and e["upgraded"].startswith("now #")
    new = prof.entry(by[still]["added"][0])
    # limited to the kinds the note applies to (asked for "all"), never Lacy
    assert new["setting"] == "motion" and new["value"] == "none" and S.entry_modes(new) == ["tokyo_girl"]
    assert prof.pinned("tokyo_girl")["motion"] == "none" and "motion" not in prof.pinned("lacy")
    assert prof.entry(date)["on"] and prof.entry(date)["status"] == "needs_code"
    assert not prof.entry(beat)["on"] and prof.entry(beat)["upgraded"] == "built in now"
    assert prof.entry(music)["status"] == "needs_code" and by[music]["verdict"] == "needs_code"
    assert prof.entry(post)["on"] and prof.entry(post)["status"] == "not_editor"
    assert RC.pending(prof.entries(), "v1") == []
    assert {e["n"] for e in RC.needs_code(prof.entries())} == {date, music}
    out = json.loads(RC.export(prof.entries(), tmp_path / "data").read_text())
    assert {r["n"] for r in out} == {date, music}


def test_new_capabilities_recheck_open_notes(tmp_path, monkeypatch):
    store, _ = new_item(tmp_path)
    prof = notes_profile(store)
    t0 = 1_000_000.0
    RC.apply(prof, RC.capabilities_version(), [res(e["n"], "needs_code", needs="x") for e in prof.entries()], now=t0)
    assert RC.pending(prof.entries(), RC.capabilities_version(), now=t0) == []
    monkeypatch.setattr(S, "BUILT_IN", S.BUILT_IN + ("Tokyo Girl: a new automatic behaviour",))
    # credits: a run of deploys doesn't re-send every note; a day later the new editor is checked
    assert RC.pending(prof.entries(), RC.capabilities_version(), now=t0 + 3600) == []
    assert len(RC.pending(prof.entries(), RC.capabilities_version(), now=t0 + RC.RECHECK_S + 1)) == 5


def test_not_editor_notes_and_skipped_answers_are_never_asked_again(tmp_path):
    store, _ = new_item(tmp_path)
    prof = notes_profile(store)
    ns = [e["n"] for e in prof.entries()]
    t0 = 1_000_000.0
    # the model answers only the first note: the rest are still marked as read
    RC.apply(prof, "v1", [res(ns[0], "not_editor")] + [{"n": n, "verdict": "none"} for n in ns[1:]], now=t0)
    assert RC.pending(prof.entries(), "v1", now=t0 + 60) == []
    later = RC.pending(prof.entries(), "v2", now=t0 + RC.RECHECK_S + 1)
    assert ns[0] not in {e["n"] for e in later} and len(later) == len(ns) - 1


def test_decide_marks_entries_the_answer_skipped(tmp_path, monkeypatch):
    store, _ = new_item(tmp_path)
    prof = notes_profile(store)
    ns = [e["n"] for e in prof.entries()]
    monkeypatch.setattr(RC, "ask", lambda todo, eff, base, client=None: [res(ns[0], "built_in")])
    version, results = RC.decide(prof, None)
    assert {r["n"] for r in results} == set(ns) and sum(r["verdict"] == "none" for r in results) == len(ns) - 1
    RC.apply(prof, version, results)
    assert RC.pending(prof.entries(), version) == []


def test_bot_compiles_saved_rules_and_files_issues(tmp_path, monkeypatch):
    store, _ = new_item(tmp_path)
    prof = notes_profile(store)
    b, _ = bot_for(tmp_path, store)
    (tmp_path / ".env").write_text("GITHUB_TOKEN=ghp_test\n")
    sent = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(req, timeout=0):
        sent.append(json.loads(req.data))
        assert req.headers["Authorization"] == "Bearer ghp_test"
        return Resp(json.dumps({"number": 77}).encode())
    monkeypatch.setattr(RC.urllib.request, "urlopen", fake_open)
    ns = [e["n"] for e in prof.entries() if e["kind"] in ("note", "unsupported")]
    c = FakeClaude({"results": [res(ns[0], "settings", [row("motion", "none")]),
                                res(ns[1], "needs_code", needs="release-date end card")]
                    + [res(n, "not_editor") for n in ns[2:]]})
    changes = b.compile_rules(client=c, now=10)
    assert "Saved instructions" in c.requests[0]["messages"][0]["content"]
    prof = store.profile()
    assert not prof.entry(ns[0])["on"] and prof.entry(ns[1])["issue"] == 77
    assert len(sent) == 1 and "rule-request" in sent[0]["labels"] and "release-date" in sent[0]["body"]
    text = DB.ReviewBot.compile_notice(changes, True)
    assert "is now applied" in text and "issue #77" in text
    line = DB.rule_line(prof.entry(ns[1]), {})
    assert "needs new editor code (issue #77)" in line
    # nothing pending: no API call, no second issue
    assert b.compile_rules(client=FakeClaude(), now=99999) == [] and len(sent) == 1


def test_deploy_requests_run_once_and_expire(tmp_path):
    store, _ = new_item(tmp_path)
    b, _ = bot_for(tmp_path, store)
    calls = []

    async def fake_trend(inter, song, per_song):
        calls.append((inter, song, per_song))
    b.run_trend = fake_trend
    reqs = tmp_path / "reqs.json"
    reqs.write_text(json.dumps([
        {"id": "a", "action": "trend", "until": "2030-01-01T00:00:00+00:00"},
        {"id": "b", "action": "trend", "until": "2020-01-01T00:00:00+00:00"},
        {"id": "c", "action": "rm -rf"}]))

    async def go():
        first = await b.run_deploy_requests(now=1_800_000_000, path=reqs)
        await asyncio.sleep(0)
        second = await b.run_deploy_requests(now=1_800_000_000, path=reqs)
        return first, second
    first, second = asyncio.run(go())
    assert first == ["a"] and second == [] and calls == [(None, None, 1)]


def test_shipped_deploy_requests_are_valid():
    from pathlib import Path
    reqs = json.loads((Path(DB.__file__).with_name("deploy_requests.json")).read_text())
    assert all(r["action"] in ("trend", "feedback", "repost", "daily", "announce", "compare", "config") and r["id"]
               for r in reqs)   # config = main.apply_config_requests, before the bot starts


def test_deploy_request_queues_a_feedback_rerender(tmp_path):
    store, item = new_item(tmp_path)
    b, _ = bot_for(tmp_path, store)
    reqs = tmp_path / "reqs.json"
    reqs.write_text(json.dumps([
        {"id": "f1", "action": "feedback", "item": item["id"], "round": 1, "label": "B", "notes": "warmer"},
        {"id": "f2", "action": "feedback", "item": "nope", "round": 1, "label": "B", "notes": "x"}]))
    ran = asyncio.run(b.run_deploy_requests(now=1_800_000_000, path=reqs))
    assert ran == ["f1", "f2"]
    req = store.load(item["id"])["request"]
    assert req["kind"] == "feedback" and req["status"] == "queued" and req["label"] == "B" and req["notes"] == "warmer"
    assert not (store.dir("nope") / "item.json").exists()


def test_deploy_request_reposts_the_latest_round(tmp_path):
    store, item = new_item(tmp_path)
    b, reg = bot_for(tmp_path, store)
    reqs = tmp_path / "reqs.json"
    reqs.write_text(json.dumps([{"id": "r1", "action": "repost", "item": item["id"]}]))
    assert asyncio.run(b.run_deploy_requests(now=1_800_000_000, path=reqs)) == ["r1"]
    m = reg[100].sent[-1]
    assert item["id"] in m.content and len(m.kw["files"]) == 3


def test_key_news_goes_to_announcements_not_status(tmp_path):
    from .test_review import FakeChannel
    store, _ = new_item(tmp_path)
    b, reg = bot_for(tmp_path, store)
    ann = FakeChannel(300, reg)
    ann.name = "📢-announcements"

    class Guild:
        text_channels = [ann]

    class Client:
        guilds = [Guild()]
    b.client = Client()
    status = []

    class Notifier:
        async def send(self, key, text, **kw):
            status.append(text)
    b.notifier = Notifier()
    # new rules saved since the last look
    prof = store.profile()
    seen = max((e["n"] for e in prof.entries()), default=0)
    prof.pin("grade", "moody", "lacy")
    prof.save()
    seen2 = asyncio.run(b.announce_new_rules(seen))
    assert seen2 == seen + 1 and "New rule" in ann.sent[-1].content and "moody" in ann.sent[-1].content
    assert asyncio.run(b.announce_new_rules(seen2)) == seen2 and len(ann.sent) == 1   # once
    # a deploy can tell Tim what changed
    reqs = tmp_path / "reqs.json"
    reqs.write_text(json.dumps([{"id": "x", "action": "announce", "text": "🚀 New: Kick clips"}]))
    asyncio.run(b.run_deploy_requests(now=1_800_000_000, path=reqs))
    assert ann.sent[-1].content == "🚀 New: Kick clips" and not status
    # no #announcements channel: falls back to #status
    b.client = type("C", (), {"guilds": []})()
    b._announce_id = None
    asyncio.run(b.announce("k", "fallback"))
    assert status == ["fallback"]
