"""Run with Hisho's venv at the repository's top: ~/code/hisho/.venv/bin/python -m pytest -q

FakeVikunja answers like Vikunja v2.5 did on 2026-10-21 (shapes, the zero date, the Kanban view with a
done bucket), keeps tasks in memory, applies writes and records every request."""
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hisho import addon_store, addons

HERE = Path(__file__).resolve().parents[1]
NOW = datetime.now(timezone.utc)
ZERO = "0001-01-01T00:00:00Z"


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def task(i, title, done=False, due=ZERO, updated=None, done_at=ZERO, project=15, **kw):
    return {"id": i, "identifier": f"#{i}", "title": title, "done": done, "due_date": due, "done_at": done_at,
            "updated": updated or iso(NOW - timedelta(days=30)), "project_id": project, "priority": 0,
            "percent_done": 0, "labels": [], "assignees": [], "description": "", **kw}


TASKS = [
    task(1, "Upload limits", due=iso(NOW - timedelta(days=2))),                     # overdue
    task(2, "CI pipeline", due=iso(NOW + timedelta(days=3)), priority=3),            # next due
    task(3, "Speaker labels", updated=iso(NOW - timedelta(hours=5))),                # recently changed
    task(4, "Login page", done=True, done_at=iso(NOW - timedelta(days=1)), updated=iso(NOW - timedelta(days=1))),
    task(9, "Someone else's", project=4),
]
PROJECTS = [{"id": 15, "title": "Website"}, {"id": 4, "title": "Vikunja / Task Management System"}]
BUCKETS = [{"id": 45, "title": "Backlog", "tasks": [3]}, {"id": 46, "title": "In Progress", "tasks": [1, 2]},
           {"id": 47, "title": "Prod Released", "tasks": [4]}]


class FakeVikunja:
    def __init__(self, password="pw", down=False):
        self.tasks = {t["id"]: copy.deepcopy(t) for t in TASKS}
        self.calls, self.logins, self.password, self.down = [], 0, password, down
        self.comments, self.labels_put, self.moved = [], [], []
        self.expire_once = False

    def __call__(self, method, url, headers, body, timeout):
        from urllib.parse import urlsplit
        if self.down:
            raise self.mod.vk.VikunjaError("not reachable: [Errno 111] Connection refused", "unreachable")
        u = urlsplit(url)
        path, q = u.path.removeprefix("/api/v1"), u.query
        self.calls.append((method, path))
        if path == "/login":
            if body["password"] != self.password:
                return 412, {}, {"message": "Wrong username or password."}
            self.logins += 1
            return 200, {}, {"token": f"jwt{self.logins}"}
        if path == "/info":
            return 200, {}, {"version": "v2.5.0", "frontend_url": "https://tasks.example.com/"}
        if self.expire_once:
            self.expire_once = False
            return 401, {}, {"message": "missing, malformed, expired or otherwise invalid token provided"}
        if not headers.get("Authorization", "").startswith("Bearer jwt"):
            return 401, {}, {"message": "no token"}
        if path == "/user":
            return 200, {}, {"username": "hisho", "id": 2}
        if path == "/projects":
            return 200, {"x-pagination-total-pages": "1"}, PROJECTS
        if path == "/labels":
            return 200, {"x-pagination-total-pages": "1"}, [{"id": 5, "title": "BUG"}]
        if path == "/tasks" and method == "GET":
            pid = int(q.split("project_id+%3D+")[1].split("&")[0])
            return 200, {"x-pagination-total-pages": "1"}, [t for t in self.tasks.values() if t["project_id"] == pid]
        if path == "/projects/15/views":
            return 200, {}, [{"id": 57, "view_kind": "list"}, {"id": 60, "view_kind": "kanban", "done_bucket_id": 47}]
        if path == "/projects/15/views/60/tasks":
            return 200, {}, [{**b, "tasks": [{"id": i} for i in b["tasks"]]} for b in BUCKETS]
        if path == "/projects/15/views/60/buckets":
            return 200, {}, [{"id": b["id"], "title": b["title"]} for b in BUCKETS]
        if path.startswith("/projects/15/views/60/buckets/") and method == "POST":
            self.moved.append((int(path.split("/")[6]), body))
            return 200, {}, body
        if path == "/projects/15/tasks" and method == "PUT":
            i = max(self.tasks) + 1
            self.tasks[i] = task(i, body["title"], **{k: v for k, v in body.items() if k != "title"})
            return 201, {}, self.tasks[i]
        if path.startswith("/tasks/"):
            parts = path.split("/")
            i = int(parts[2])
            if len(parts) == 3 and method == "GET":
                return (200, {}, copy.deepcopy(self.tasks[i])) if i in self.tasks else (404, {}, {"message": "nope"})
            if len(parts) == 3 and method == "POST":
                assert set(body) >= {"title", "description", "labels"}, "an update must send the whole task"
                self.tasks[i] = body
                return 200, {}, body
            if parts[3] == "comments" and method == "PUT":
                self.comments.append((i, body["comment"]))
                return 201, {}, {"id": 1}
            if parts[3] == "labels" and method == "PUT":
                self.labels_put.append((i, body["label_id"]))
                return 201, {}, {}
        return 404, {}, {"message": f"no route {method} {path}"}


ENTRY = {"main": {"url": "https://tasks.example.com", "user": "VIKUNJA_USER", "password": "VIKUNJA_PASSWORD",
                  "projects": "Website"}}


@pytest.fixture
def mod(addon):
    m = addons.provider(addon)
    m.vk._tokens.clear()
    m._frontend.clear()
    return m


@pytest.fixture
def fake(mod, monkeypatch, tmp_path):
    f = FakeVikunja(password="value-of-VIKUNJA_PASSWORD")
    f.mod = mod
    monkeypatch.setattr(mod.vk, "http", f)
    monkeypatch.setenv("HISHO_STATE_DIR", str(tmp_path / "state"))
    return f


def press(mod, ctx, item, action, typed="", entries=ENTRY):
    ctx.input = typed
    return mod.act(entries, ctx, "main", item, action)


def test_manifest_is_valid_and_readme_matches(addon):
    assert addons.validate_manifest(addon.manifest) == []
    assert addon_store.readme_problems(HERE) == []


def test_no_entries_is_unknown_not_an_alert(addon, ctx_for):
    assert addons.summarize(addon, ctx_for(addon))["state"] in ("unknown", "ok")
    assert addons.provider(addon).summary({}, ctx_for(addon))["state"] == "unknown"


def test_summary_counts_and_overdue_is_not_a_problem(mod, fake, addon, ctx_for):
    s = mod.summary(ENTRY, ctx_for(addon))
    assert s["state"] == "ok" and s["problems"] == []        # an overdue task never reaches the phone
    assert s["headline"].startswith("Website: 1/4 done (25%) · 1 overdue · next due ")
    assert fake.logins == 1
    mod.summary(ENTRY, ctx_for(addon))
    assert fake.logins == 1                                    # the token is reused between checks


def test_expired_token_logs_in_again(mod, fake, addon, ctx_for):
    mod.summary(ENTRY, ctx_for(addon))
    fake.expire_once = True
    assert mod.summary(ENTRY, ctx_for(addon))["state"] == "ok"
    assert fake.logins == 2


@pytest.mark.parametrize("kind,title", [("down", "Can't reach main"), ("password", "Can't log in to main")])
def test_unreachable_or_bad_login_is_a_warning(mod, fake, addon, ctx_for, kind, title):
    if kind == "down":
        fake.down = True
    else:
        fake.password = "something else"
    s = mod.summary(ENTRY, ctx_for(addon))
    assert s["state"] == "warn" and [p["title"] for p in s["problems"]] == [title]
    assert s["problems"][0]["severity"] == "warn"
    assert "value-of" not in json.dumps(s)                    # no secret in what the card shows
    assert mod.test(ENTRY["main"], ctx_for(addon))["ok"] is False


def test_unknown_project_is_a_problem(mod, fake, addon, ctx_for):
    s = mod.summary({"main": {**ENTRY["main"], "projects": "Website, Nope"}}, ctx_for(addon))
    assert [p["title"] for p in s["problems"]] == ["No project Nope on main"]


def test_connection_test(mod, fake, addon, ctx_for):
    res = mod.test({**ENTRY["main"], "projects": "15"}, ctx_for(addon))
    assert res == {"ok": True, "message": "Connected: Vikunja v2.5.0 as hisho; watching Website (#15)"}


def test_details_page(mod, fake, addon, ctx_for):
    [sec] = mod.details(ENTRY, ctx_for(addon))
    assert sec["title"] == "Website" and sec["state"] == "warn"
    assert sec["facts"][0].startswith("3 open · 1 done · 25% done · 1 overdue · next due ")
    assert sec["facts"][1] == "Board: Backlog 1 · In Progress 2 · Prod Released 1"
    assert any(f.startswith("Recently completed: Login page") for f in sec["facts"])
    assert any(f.startswith("Recently changed: Speaker labels") for f in sec["facts"])
    ids = [it["id"] for it in sec["items"]]
    assert ids == ["project-15", "task-1", "task-2", "task-3", "task-4"]   # overdue first, then by due date
    late = sec["items"][1]
    assert late["state"] == "warn" and late["facts"][1].startswith("OVERDUE since")
    assert late["url"] == "https://tasks.example.com/tasks/1"
    assert [a["id"] for a in late["actions"]] == ["done", "move", "due", "priority", "comment"]
    assert late["actions"][1]["ask"].endswith("Backlog, In Progress, Prod Released")
    assert [a["id"] for a in sec["items"][-1]["actions"]] == ["undone"]
    ro = mod.details({"main": {**ENTRY["main"], "allow_writes": False}}, ctx_for(addon))[0]
    assert all(not it["actions"] for it in ro["items"]) and "read only" in ro["facts"][-1]


def test_add_task_with_due_priority_label_and_description(mod, fake, addon, ctx_for):
    res = press(mod, ctx_for(addon), "project-15", "add", "Fix the upload due:2026-11-03 !4 *bug | Big files fail")
    assert res["ok"], res
    t = fake.tasks[res["task_id"]]
    assert (t["title"], t["priority"], t["description"]) == ("Fix the upload", 4, "<p>Big files fail</p>")
    assert t["due_date"].startswith("2026-11-03T17:00:00")
    assert fake.labels_put == [(res["task_id"], 5)]


def test_unknown_label_creates_nothing(mod, fake, addon, ctx_for):
    res = press(mod, ctx_for(addon), "project-15", "add", "Thing *nosuch")
    assert not res["ok"] and "no label nosuch" in res["error"]
    assert ("PUT", "/projects/15/tasks") not in fake.calls


def test_task_buttons(mod, fake, addon, ctx_for):
    ctx = ctx_for(addon)
    assert press(mod, ctx, "task-2", "done")["message"] == "#2 CI pipeline: done"
    assert fake.tasks[2]["done"] is True and fake.tasks[2]["priority"] == 3      # the rest of the task kept
    assert press(mod, ctx, "task-2", "undone")["ok"] and fake.tasks[2]["done"] is False
    assert press(mod, ctx, "task-3", "due", "none")["message"] == "#3 Speaker labels: no due date"
    assert fake.tasks[3]["due_date"] == ZERO
    assert press(mod, ctx, "task-3", "priority", "urgent")["message"] == "#3 Speaker labels: priority 4 (urgent)"
    assert press(mod, ctx, "task-3", "comment", "Customer tested <this>")["ok"]
    assert fake.comments == [(3, "<p>Customer tested &lt;this&gt;</p>")]
    assert not press(mod, ctx, "task-3", "due", "someday")["ok"]


def test_buttons_stay_in_watched_projects_and_never_delete(mod, fake, addon, ctx_for):
    ctx = ctx_for(addon)
    assert "not in a project" in press(mod, ctx, "task-9", "done")["error"]
    assert "not watched" in press(mod, ctx, "project-4", "add", "x")["error"]
    assert "not a button" in press(mod, ctx, "task-1", "delete")["error"]
    ro = {"main": {**ENTRY["main"], "allow_writes": False}}
    assert "read only" in press(mod, ctx, "task-1", "done", entries=ro)["error"]
    assert fake.tasks[9]["done"] is False and fake.tasks[1]["done"] is False
    assert not any(m == "DELETE" for m, _ in fake.calls)
    assert "DELETE" not in (HERE / "vikunja.py").read_text()


def test_changes_are_journaled_and_only_posted_when_discord_is_on(mod, fake, addon, ctx_for, fleet, monkeypatch):
    ctx = ctx_for(addon)
    hooked = {"main": {**ENTRY["main"], "discord_webhook": "VIKUNJA_TEAM_DISCORD_WEBHOOK_URL"}}
    sent = []
    monkeypatch.setattr(mod.vk, "post_discord", lambda url, msg: sent.append((url, msg)))
    mod.summary(hooked, ctx)                                   # first check only remembers
    fake.tasks[2]["done"] = True
    fake.tasks[3]["due_date"] = iso(NOW + timedelta(days=9))
    assert mod.summary(hooked, ctx)["problems"] == []
    assert sent == []                                          # discord is off by default
    journal = "".join(p.read_text() for p in (fleet / "System" / "Journal").rglob("*") if p.is_file())
    assert "vikunja-change" in journal and "marked complete" in journal and "CI pipeline" in journal

    fake.tasks[1]["done"] = True
    assert mod.summary({"main": {**hooked["main"], "discord": True}}, ctx)["problems"] == []
    [(url, msg)] = sent
    assert url == "value-of-VIKUNJA_TEAM_DISCORD_WEBHOOK_URL"
    assert msg.startswith("📋 **Website: Vikunja task changes**\n• ✅ [#1](https://tasks.example.com/tasks/1)")


def test_failed_discord_post_is_retried_next_check(mod, fake, addon, ctx_for, monkeypatch):
    ctx = ctx_for(addon)
    on = {"main": {**ENTRY["main"], "discord": True, "discord_webhook": "HOOK_URL"}}
    mod.summary(on, ctx)
    fake.tasks[1]["title"] = "Upload limits v2"

    def broken(url, msg):
        raise mod.vk.VikunjaError("Discord webhook answered HTTP 404")
    monkeypatch.setattr(mod.vk, "post_discord", broken)
    s = mod.summary(on, ctx)
    assert [p["title"] for p in s["problems"]] == ["Website: Discord post failed"]
    assert "value-of" not in json.dumps(s)
    sent = []
    monkeypatch.setattr(mod.vk, "post_discord", lambda url, msg: sent.append(msg))
    assert mod.summary(on, ctx)["problems"] == []
    assert len(sent) == 1 and "Upload limits v2" in sent[0]


def test_discord_post_sends_no_mentions_and_hides_the_address(mod, monkeypatch):
    got = []
    monkeypatch.setattr(mod.vk, "http", lambda *a: got.append(a) or (404, {}, {"message": "Unknown Webhook"}))
    with pytest.raises(mod.vk.VikunjaError) as err:
        mod.vk.post_discord("https://discord.com/api/webhooks/1/SECRET", "hi @everyone")
    assert "SECRET" not in str(err.value)
    assert got[0][3]["allowed_mentions"] == {"parse": []}


def test_typed_task_and_dates(mod):
    vk = mod.vk
    assert vk.parse_new_task("Just a title") == {"title": "Just a title", "labels": [], "description": ""}
    with pytest.raises(ValueError):
        vk.parse_new_task("due:today !2")
    now = datetime(2026, 10, 21, 9, 0).astimezone()
    assert vk.due_from_text("tomorrow", now).startswith("2026-10-22T17:00:00")
    assert vk.due_from_text("+10", now).startswith("2026-10-31T17:00:00")
    assert vk.parse_time(ZERO) is None
    assert vk.priority_from_text("!5") == 5


def test_phone_message_when_a_task_is_assigned_to_me(mod, fake, addon, ctx_for, fleet, monkeypatch):
    ctx = ctx_for(addon)
    sent = []
    monkeypatch.setattr(mod, "send_phone", lambda title, msg, click="/#/addons/vikunja": sent.append((title, msg)) or True)
    me = {"main": {**ENTRY["main"], "notify_assigned": "Jane"}}
    fake.tasks[3]["assignees"] = [{"username": "jane"}]          # already mine before the first look
    mod.summary(me, ctx)
    assert sent == []                                            # the first look only remembers
    fake.tasks[2]["assignees"] = [{"username": "jane"}, {"username": "bob"}]
    fake.tasks[1]["assignees"] = [{"username": "bob"}]           # someone else's: no message
    fake.tasks[4]["assignees"] = [{"username": "jane"}]          # done already: no message
    mod.summary(me, ctx)
    [(title, msg)] = sent
    assert title == "Vikunja: a task assigned to you in Website"
    assert msg.startswith("#2 CI pipeline (due ") and msg.endswith(" · In Progress")
    mod.summary(me, ctx)
    assert len(sent) == 1                                        # never repeated
    journal = "".join(p.read_text() for p in (fleet / "System" / "Journal").rglob("*") if p.is_file())
    assert "vikunja-assigned" in journal
    fake.tasks[2]["assignees"] = []
    mod.summary(me, ctx)
    fake.tasks[2]["assignees"] = [{"username": "Jane"}]
    mod.summary(me, ctx)
    assert len(sent) == 2                                        # unassigned, then assigned again: told again


def test_assignment_message_is_not_repeated_by_a_discord_retry(mod, fake, addon, ctx_for, monkeypatch):
    ctx = ctx_for(addon)
    sent = []
    monkeypatch.setattr(mod, "send_phone", lambda title, msg, click="/#/addons/vikunja": sent.append(msg) or True)

    def broken(url, msg):
        raise mod.vk.VikunjaError("Discord webhook answered HTTP 500")
    monkeypatch.setattr(mod.vk, "post_discord", broken)
    on = {"main": {**ENTRY["main"], "notify_assigned": "jane", "discord": True, "discord_webhook": "HOOK_URL"}}
    mod.summary(on, ctx)
    fake.tasks[1]["assignees"] = [{"username": "jane"}]
    for _ in range(3):
        assert mod.summary(on, ctx)["problems"]                  # Discord keeps failing...
    assert len(sent) == 1                                        # ...the phone heard once


def test_a_failed_phone_message_never_breaks_the_card(mod, fake, addon, ctx_for, monkeypatch):
    ctx = ctx_for(addon)

    def down(*a, **k):
        raise OSError("Core not answering")
    monkeypatch.setattr(mod, "send_phone", down)
    me = {"main": {**ENTRY["main"], "notify_assigned": "jane"}}
    mod.summary(me, ctx)
    fake.tasks[1]["assignees"] = [{"username": "jane"}]
    assert mod.summary(me, ctx)["state"] == "ok"


def test_changes_go_through_the_chat_add_on_when_a_channel_is_named(mod, fake, addon, ctx_for, stand_in, monkeypatch):
    ctx = ctx_for(addon)
    chat = stand_in("chatx", kind="integration", provides=["chat_messages"], entry_schema={"token": "secret"})
    (chat.path / "provider.py").write_text(
        (chat.path / "provider.py").read_text()
        + "\nSENT = []\n\ndef post_message(entries, ctx, channel, text, entry=None):\n"
          "    SENT.append((channel, text, entry))\n    return {'ok': True, 'id': 'm1'}\n")
    addons.set_entry(chat, "team", {"token": "HISHO_T"})
    addons.enable("chatx")
    on = {"main": {**ENTRY["main"], "discord": True, "discord_channel": "vikunja-tasks",
                   "discord_webhook": "NOT_USED"}}
    monkeypatch.setattr(mod.vk, "post_discord", lambda *a: pytest.fail("the webhook is not used when a channel is named"))
    mod.summary(on, ctx)
    fake.tasks[2]["done"] = True
    assert mod.summary(on, ctx)["problems"] == []
    [(channel, text, entry)] = addons.provider(addons.get("chatx")).SENT
    assert (channel, entry) == ("vikunja-tasks", None)
    assert text.startswith("📋 **Website: Vikunja task changes**\n• ✅ [#2]")


def test_no_chat_add_on_is_a_problem_and_retried(mod, fake, addon, ctx_for):
    ctx = ctx_for(addon)
    on = {"main": {**ENTRY["main"], "discord": True, "discord_channel": "vikunja-tasks"}}
    mod.summary(on, ctx)
    fake.tasks[2]["done"] = True
    s = mod.summary(on, ctx)
    assert [p["title"] for p in s["problems"]] == ["Website: Discord post failed"]
    assert "chat messages" in s["problems"][0]["detail"]


def test_a_lasting_problem_is_told_to_the_alerts_channel_once_and_when_fixed(mod, fake, addon, ctx_for, monkeypatch):
    ctx = ctx_for(addon)
    said = []
    monkeypatch.setattr(mod, "post_chat", lambda entry, text, channel=None: said.append((channel, text)))
    on = {"main": {**ENTRY["main"], "alerts_channel": "alerts"}}
    fake.down = True
    mod.summary(on, ctx)
    mod.summary(on, ctx)
    assert said == []                                           # a blip is not news
    mod.summary(on, ctx)
    mod.summary(on, ctx)
    assert len(said) == 1 and said[0][0] == "alerts"
    assert said[0][1].startswith("⚠️ Hisho: Can't reach main (3 checks in a row) · https://tasks.example.com: ")
    fake.down = False
    mod.summary(on, ctx)
    assert said[-1] == ("alerts", "✅ Hisho: Can't reach main is fixed")
    mod.summary(on, ctx)
    assert len(said) == 2


def test_move_to_a_column_by_name(mod, fake, addon, ctx_for):
    ctx = ctx_for(addon)
    assert press(mod, ctx, "task-3", "move", "in progress")["message"] == "#3 Speaker labels: moved to In Progress"
    assert fake.moved == [(46, {"task_id": 3, "bucket_id": 46, "project_view_id": 60})]
    assert press(mod, ctx, "task-3", "move", "prod")["message"].endswith("moved to Prod Released")   # start of a name
    res = press(mod, ctx, "task-3", "move", "Nowhere")
    assert not res["ok"] and "columns: Backlog, In Progress, Prod Released" in res["error"]
    assert "not in a project" in press(mod, ctx, "task-9", "move", "Backlog")["error"]
