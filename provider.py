"""Vikunja integration for Hisho (see addon.yaml). Runs inside Core; talks to Vikunja's API with
vikunja.py (next to this file), which has no delete call at all.

summary() runs after each fleet check: it reads the watched projects, journals what changed since the
last check (``vikunja-change`` events: new tasks, done/reopened, moved column, due date, priority...)
and, only when the entry turns ``discord`` on, posts the same lines to a Discord webhook. Overdue tasks
are shown on the card and the page, never sent to the phone: only "can't reach", "can't log in", "no such
project" and "Discord post failed" are problems.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_spec = importlib.util.spec_from_file_location("hisho_addon_vikunja_client", Path(__file__).with_name("vikunja.py"))
vk = importlib.util.module_from_spec(_spec)
assert _spec.loader
_spec.loader.exec_module(vk)

RECENT_CHANGED = timedelta(days=7)
RECENT_DONE = timedelta(days=14)
MAX_ITEMS = 80          # open tasks listed on the page per project
MAX_LINES = 12          # change lines per Discord message (as the sync it replaced did)
TASK_BUTTONS = ("done", "undone", "move", "due", "priority", "comment")
ALERT_AFTER = 3         # checks in a row a problem must last before alerts_channel hears of it
_frontend: dict[str, str] = {}


# ── connection ────────────────────────────────────────────────────────────

def client(entry: dict[str, Any], ctx: Any) -> Any:
    def ref(key: str):
        name = entry.get(key)
        return (lambda: ctx.secret(name)) if name else None
    return vk.Client(entry["url"], user=ref("user"), password=ref("password"), token=ref("token"))


def writes_allowed(entry: dict[str, Any]) -> bool:
    return entry.get("allow_writes") is not False


def frontend(cl: Any) -> str:
    """Where task links point (Vikunja's frontend_url), asked once per server."""
    if cl.base not in _frontend:
        try:
            _frontend[cl.base] = str(cl.info().get("frontend_url") or cl.base).rstrip("/")
        except vk.VikunjaError:
            return cl.base
    return _frontend[cl.base]


def watched(entry: dict[str, Any], projects: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """The entry's ``projects`` (ids or titles, comma-separated) -> (found projects, names not found). Empty = every
    project the user sees (a Vikunja Hisho just installed has none named yet)."""
    if not str(entry.get("projects") or "").strip():
        return [p for p in projects if not p.get("is_archived")], []
    found, missing = [], []
    for want in [w.strip() for w in str(entry.get("projects") or "").split(",") if w.strip()]:
        p = next((p for p in projects if str(p["id"]) == want or p["title"].lower() == want.lower()), None)
        if p and p not in found:
            found.append(p)
        elif not p:
            missing.append(want)
    return found, missing


def read(cl: Any, project: dict[str, Any]) -> dict[str, Any]:
    tasks = cl.tasks(project["id"])
    buckets = cl.buckets(project["id"])
    column = {tid: b["title"] for b in buckets for tid in b["tasks"]}
    for t in tasks:
        t["_column"] = column.get(t["id"], "")
    return {"project": project, "tasks": tasks, "buckets": buckets}


# ── numbers ───────────────────────────────────────────────────────────────

def stats(snap: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or vk.now_utc()
    tasks = snap["tasks"]
    open_ = [t for t in tasks if not t.get("done")]
    done = [t for t in tasks if t.get("done")]
    due = sorted(((vk.parse_time(t.get("due_date")), t) for t in open_ if vk.parse_time(t.get("due_date"))),
                 key=lambda x: x[0])
    overdue = [t for d, t in due if d < now]
    upcoming = [(d, t) for d, t in due if d >= now]
    changed = sorted((t for t in tasks if (u := vk.parse_time(t.get("updated"))) and now - u < RECENT_CHANGED),
                     key=lambda t: t["updated"], reverse=True)
    completed = sorted((t for t in done if (d := vk.parse_time(t.get("done_at"))) and now - d < RECENT_DONE),
                       key=lambda t: t["done_at"], reverse=True)
    return {"open": len(open_), "done": len(done), "total": len(tasks),
            "percent": round(100 * len(done) / len(tasks)) if tasks else 0,
            "overdue": overdue, "next_due": upcoming[0] if upcoming else None,
            "changed": changed, "completed": completed,
            "columns": [(b["title"], len(b["tasks"])) for b in snap["buckets"]]}


def _day(d: datetime) -> str:
    return d.astimezone().strftime("%b %-d")


def _ago(d: datetime | None, now: datetime) -> str:
    if not d:
        return ""
    s = (now - d).total_seconds()
    return "just now" if s < 90 else f"{int(s // 60)} min ago" if s < 3600 else \
        f"{int(s // 3600)} h ago" if s < 86400 else f"{int(s // 86400)} d ago"


def headline(title: str, st: dict[str, Any]) -> str:
    bits = [f"{title}: {st['done']}/{st['total']} done ({st['percent']}%)"]
    if st["overdue"]:
        bits.append(f"{len(st['overdue'])} overdue")
    if st["next_due"]:
        bits.append(f"next due {_day(st['next_due'][0])}")
    return " · ".join(bits)


# ── changes: journal, and Discord when it is turned on ───────────────────

def _state_file(entry: str, project_id: int) -> Path:
    base = Path(os.environ.get("HISHO_STATE_DIR") or "~/.local/state/hisho").expanduser()
    return base / "addons" / "vikunja" / f"{entry}-{project_id}.json"


def brief(snap: dict[str, Any], link: str) -> dict[str, dict[str, Any]]:
    """What a change is measured on, per task id."""
    return {str(t["id"]): {"id": t["id"], "identifier": t.get("identifier") or f"#{t['id']}",
                           "title": t.get("title") or "(untitled)", "done": bool(t.get("done")),
                           "column": t.get("_column", ""), "priority": t.get("priority") or 0,
                           "percent_done": t.get("percent_done") or 0, "due_date": t.get("due_date") or vk.ZERO,
                           "assignees": sorted(a.get("username") or str(a.get("id")) for a in t.get("assignees") or []),
                           "labels": sorted(lb.get("title") or "" for lb in t.get("labels") or []),
                           "url": f"{link}/tasks/{t['id']}"} for t in snap["tasks"]}


def _md(text: Any) -> str:
    return re.sub(r"([*_`~>|\\\[\]])", r"\\\1", str(text))[:180]


def change_line(old: dict[str, Any] | None, new: dict[str, Any]) -> str | None:
    """One line about one task, in the words the sync it replaced used; None when nothing we show changed."""
    what = f"[{_md(new['identifier'])}]({new['url']}) **{_md(new['title'])}**"
    if old is None:
        return f"➕ New task: {what}" + (f" → `{_md(new['column'])}`" if new["column"] else "")
    parts = []
    if old["title"] != new["title"]:
        parts.append(f"title `{_md(old['title'])}` → `{_md(new['title'])}`")
    if old["column"] != new["column"] and new["column"]:
        parts.append(f"status `{_md(old['column'] or '-')}` → `{_md(new['column'])}`")
    if old["done"] != new["done"]:
        parts.append("marked complete" if new["done"] else "reopened")
    if old["priority"] != new["priority"]:
        parts.append(f"priority `{old['priority']}` → `{new['priority']}`")
    if old["percent_done"] != new["percent_done"]:
        parts.append(f"progress `{round(old['percent_done'] * 100)}%` → `{round(new['percent_done'] * 100)}%`")
    if old["due_date"] != new["due_date"]:
        d = vk.parse_time(new["due_date"])
        parts.append(f"due {_day(d)}" if d else "due date removed")
    if old["assignees"] != new["assignees"]:
        parts.append("assignees changed")
    if old["labels"] != new["labels"]:
        parts.append("labels changed")
    if not parts:
        return None
    return ("✅ " if new["done"] and not old["done"] else "📝 ") + f"{what}: " + "; ".join(parts)


def changes(before: dict[str, Any], now: dict[str, Any]) -> list[str]:
    out = [line for tid, new in sorted(now.items(), key=lambda kv: int(kv[0]))
           if (line := change_line(before.get(tid), new))]
    out += [f"🗑️ Removed or moved away: `{_md(old['identifier'])}` **{_md(old['title'])}**"
            for tid, old in sorted(before.items(), key=lambda kv: int(kv[0])) if tid not in now]
    return out


def discord_message(project_title: str, lines: list[str]) -> str:
    extra = f"\n…and {len(lines) - MAX_LINES} more changes." if len(lines) > MAX_LINES else ""
    return f"📋 **{_md(project_title)}: Vikunja task changes**\n" + "\n".join(f"• {x}" for x in lines[:MAX_LINES]) + extra


def post_chat(entry: dict[str, Any], text: str, channel: str | None = None) -> None:
    """Post through the add-on that provides chat_messages (Hisho's Discord add-on): ``channel`` (default
    ``discord_channel``) in ``discord_server`` (its entry; needed only when it has several). Raises when it can't."""
    from hisho import addons
    addon, module, ents = addons.capability("chat_messages")
    res = module.post_message(ents, addons.Context(addon), str(channel or entry["discord_channel"]), text,
                              entry.get("discord_server") or None)
    if not res.get("ok"):
        raise vk.VikunjaError(f"{addon.title}: {res.get('error') or 'not posted'}")


def send_phone(title: str, message: str, click: str = "/#/addons/vikunja") -> bool:
    """One message to the Hisho phone app, through Core (what `hisho notify send` does)."""
    from hisho.mcp_server import local_core
    reply = local_core().call("POST", "/api/notify/send", body={"title": title, "message": message, "click": click})
    return bool((reply or {}).get("ok"))


def assigned_pairs(now: dict[str, Any], users: str) -> set[str]:
    """``task:user`` for every open task one of ``users`` (Vikunja user names, comma-separated) is on."""
    watch = {u.strip().lower() for u in str(users or "").split(",") if u.strip()}
    return {f"{tid}:{a.lower()}" for tid, t in now.items() if not t["done"]
            for a in t["assignees"] if a.lower() in watch}


def tell_assigned(name: str, entry: dict[str, Any], project: dict[str, Any], now: dict[str, Any]) -> list[str]:
    """Phone message for open tasks newly assigned to ``notify_assigned`` users. Its own record of what was
    told, so a Discord retry or a restart never repeats it; the first look only remembers. Unassigned and
    assigned again = told again. Returns the lines sent."""
    if not entry.get("notify_assigned"):
        return []
    path = _state_file(name, project["id"]).with_suffix(".assigned.json")
    pairs = assigned_pairs(now, entry["notify_assigned"])
    try:
        told = set(json.loads(path.read_text()))
    except (OSError, ValueError):
        told = None
    fresh = sorted({p.split(":")[0] for p in pairs - (told or set())}, key=int) if told is not None else []
    lines = []
    for tid in fresh:
        t = now[tid]
        d = vk.parse_time(t["due_date"])
        lines.append(f"{t['identifier']} {t['title']}" + (f" (due {_day(d)})" if d else "")
                     + f" · {t['column'] or 'open'}")
    if lines:
        title = (f"Vikunja: {'a task' if len(lines) == 1 else f'{len(lines)} tasks'} assigned to you"
                 f" in {project['title']}")
        try:
            ok, why = send_phone(title[:120], "\n".join(lines)[:500]), ""
        except Exception as exc:  # noqa: BLE001 -- a missed push is journaled, never breaks the card
            ok, why = False, f"{type(exc).__name__}: {str(exc)[:120]}"
        try:
            from hisho.jobs import append_event
            append_event(Path(os.environ.get("HISHO_ROOT") or "~/Hisho").expanduser(), "vikunja-assigned",
                         {"addon": "vikunja", "entry": name, "project": project["title"], "tasks": lines,
                          "pushed": ok, **({"error": why} if why else {})})
        except Exception:  # noqa: BLE001
            pass
    if told != pairs:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(pairs)))
    return lines


def follow(name: str, entry: dict[str, Any], ctx: Any, snap: dict[str, Any], link: str,
           post: Any = None) -> list[str]:
    """Compare with the last check, journal the changes and (only with ``discord: true``) post them.
    The first check only remembers. Returns the change lines; raises when the Discord post failed, and then
    keeps the old state so the next check tries again."""
    pid = snap["project"]["id"]
    path, now = _state_file(name, pid), brief(snap, link)
    try:
        before = json.loads(path.read_text())
    except (OSError, ValueError):
        before = None
    said = changes(before, now) if before is not None else []
    tell_assigned(name, entry, snap["project"], now)
    if said:
        if entry.get("discord") is True and entry.get("discord_channel"):
            post_chat(entry, discord_message(snap["project"]["title"], said))
        elif entry.get("discord") is True and entry.get("discord_webhook"):
            (post or vk.post_discord)(ctx.secret(entry["discord_webhook"]),
                                      discord_message(snap["project"]["title"], said))
        try:
            from hisho.jobs import append_event
            append_event(Path(os.environ.get("HISHO_ROOT") or "~/Hisho").expanduser(), "vikunja-change",
                         {"addon": "vikunja", "entry": name, "project": snap["project"]["title"],
                          "project_id": pid, "changes": said})
        except Exception:  # noqa: BLE001 -- journaling never breaks the card
            pass
    if before != now:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(now, indent=1, sort_keys=True))
        os.replace(tmp, path)
    return said


# ── what Core calls ───────────────────────────────────────────────────────

def _problem(name: str, entry: dict[str, Any], exc: Exception) -> dict[str, Any]:
    kind = getattr(exc, "kind", "")
    title = {"unreachable": f"Can't reach {name}", "login": f"Can't log in to {name}"}.get(kind, f"{name} answered an error")
    return {"entry": name, "severity": "warn", "title": title, "key": kind or "error",
            "detail": f"{entry.get('url')}: {exc}"}


def summary(entries: dict[str, dict[str, Any]], ctx: Any) -> dict[str, Any]:
    if not entries:
        return {"state": "unknown", "headline": "No Vikunja yet", "problems": []}
    problems, heads = [], []
    for name, entry in entries.items():
        mine = check_entry(name, entry, ctx, heads)
        problems += mine
        try:
            tell_alerts(name, entry, [p for p in mine if not str(p.get("key", "")).startswith("discord-")])
        except Exception:  # noqa: BLE001 -- the alert post is extra; it never breaks the card
            pass
    return {"state": "warn" if problems else "ok", "headline": " · ".join(heads) or "not answering",
            "problems": problems}


def check_entry(name: str, entry: dict[str, Any], ctx: Any, heads: list[str]) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    cl = client(entry, ctx)
    try:
        projects, missing = watched(entry, cl.projects())
    except vk.VikunjaError as exc:
        return [_problem(name, entry, exc)]
    for want in missing:
        problems.append({"entry": name, "severity": "warn", "key": f"project-{want}",
                         "title": f"No project {want} on {name}",
                         "detail": f"{entry.get('url')}: the user Hisho logs in as can't see a project {want!r} "
                                   "(renamed, archived away, or not shared with it)"})
    for p in projects:
        try:
            snap = read(cl, p)
        except vk.VikunjaError as exc:
            problems.append(_problem(name, entry, exc))
            continue
        heads.append(headline(p["title"], stats(snap)))
        try:
            follow(name, entry, ctx, snap, frontend(cl))
        except Exception as exc:  # noqa: BLE001
            problems.append({"entry": name, "severity": "warn", "key": f"discord-{p['id']}",
                             "title": f"{p['title']}: Discord post failed",
                             "detail": f"{type(exc).__name__}: {str(exc)[:160]}. Hisho tries again next check."})
    if not projects and not missing:
        heads.append(f"{name}: connected, no project watched")
    return problems


def tell_alerts(name: str, entry: dict[str, Any], problems: list[dict[str, Any]]) -> list[str]:
    """Post a problem to ``alerts_channel`` once it has lasted ALERT_AFTER checks, and again when it clears.
    Its own record, so each is said once. Returns what was posted."""
    if not entry.get("alerts_channel"):
        return []
    path = _state_file(name, "alerts").with_suffix(".json")
    try:
        seen = json.loads(path.read_text())
    except (OSError, ValueError):
        seen = {}
    now = {str(p.get("key") or p["title"]): p for p in problems}
    said, out = {k: dict(v) for k, v in seen.items()}, []   # copies: compared with seen before saving
    for key, p in now.items():
        rec = said.get(key) or {"count": 0, "posted": False}
        rec["count"] += 1
        if rec["count"] >= ALERT_AFTER and not rec["posted"]:
            out.append(f"⚠️ Hisho: {p['title']} ({rec['count']} checks in a row) · {p['detail'][:200]}")
            rec["posted"] = True
        rec["title"] = p["title"]
        said[key] = rec
    for key in [k for k in said if k not in now]:
        if said[key].get("posted"):
            out.append(f"✅ Hisho: {said[key].get('title') or key} is fixed")
        del said[key]
    for text in out:
        post_chat(entry, text, entry["alerts_channel"])
    if said != seen:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(said))
    return out


def _task_item(t: dict[str, Any], link: str, now: datetime, writes: bool, columns: list[str] = ()) -> dict[str, Any]:
    due = vk.parse_time(t.get("due_date"))
    late = bool(due and due < now and not t.get("done"))
    facts = [t.get("_column") or ("done" if t.get("done") else "open")]
    if due:
        facts.append(("OVERDUE since " if late else "due ") + _day(due))
    if t.get("priority"):
        facts.append(f"priority {t['priority']} ({vk.PRIORITIES.get(t['priority'], '?')})")
    if t.get("labels"):
        facts.append("labels " + ", ".join(lb["title"] for lb in t["labels"]))
    if t.get("assignees"):
        facts.append("assigned " + ", ".join(a.get("username") or "?" for a in t["assignees"]))
    if t.get("percent_done"):
        facts.append(f"{round(t['percent_done'] * 100)}% done")
    if t.get("done") and vk.parse_time(t.get("done_at")):
        facts.append("completed " + _ago(vk.parse_time(t["done_at"]), now))
    facts.append("changed " + _ago(vk.parse_time(t.get("updated")), now))
    title = f"{t.get('identifier') or '#' + str(t['id'])} {t.get('title')}"
    actions = []
    if writes and t.get("done"):
        actions = [{"id": "undone", "label": "reopen", "confirm": f"Reopen {title}?"}]
    elif writes:
        actions = [{"id": "done", "label": "done", "confirm": f"Mark {title} done?"}]
        if columns:
            actions.append({"id": "move", "label": "move", "ask": f"Move {title} to which column? {', '.join(columns)}"})
        actions += [{"id": "due", "label": "due date",
                     "ask": f"New due date for {title}: YYYY-MM-DD, today, tomorrow, +N (days) or none"},
                    {"id": "priority", "label": "priority",
                     "ask": "Priority 0-5: " + ", ".join(f"{k} {v}" for k, v in vk.PRIORITIES.items())},
                    {"id": "comment", "label": "comment", "ask": f"Comment on {title}:"}]
    return {"id": f"task-{t['id']}", "name": title, "url": f"{link}/tasks/{t['id']}",
            "state": "warn" if late else "off" if t.get("done") else "up", "facts": facts, "actions": actions}


def _order(t: dict[str, Any]) -> tuple:
    due = vk.parse_time(t.get("due_date"))
    return (due is None, due or vk.now_utc(), -(t.get("priority") or 0), -(t["id"]))


def details(entries: dict[str, dict[str, Any]], ctx: Any) -> list[dict[str, Any]]:
    sections, now = [], vk.now_utc()
    for name, entry in entries.items():
        cl, writes = client(entry, ctx), writes_allowed(entry)
        try:
            projects, missing = watched(entry, cl.projects())
            link = frontend(cl)
        except vk.VikunjaError as exc:
            sections.append({"title": name, "entry": name, "state": "down",
                             "facts": [f"{entry.get('url')}: {exc}"], "items": []})
            continue
        for want in missing:
            sections.append({"title": f"{want} (not found)", "entry": name, "state": "warn",
                             "facts": [f"No project {want!r} visible on {name}"], "items": []})
        for p in projects:
            try:
                snap = read(cl, p)
            except vk.VikunjaError as exc:
                sections.append({"title": p["title"], "entry": name, "state": "down", "facts": [str(exc)], "items": []})
                continue
            st = stats(snap, now)
            facts = [f"{st['open']} open · {st['done']} done · {st['percent']}% done · "
                     + (f"{len(st['overdue'])} overdue" if st["overdue"] else "nothing overdue")
                     + (f" · next due {_day(st['next_due'][0])}: {st['next_due'][1]['title']}" if st["next_due"] else "")]
            if st["columns"]:
                facts.append("Board: " + " · ".join(f"{c} {n}" for c, n in st["columns"]))
            if st["completed"]:
                facts.append("Recently completed: " + "; ".join(
                    f"{t['title']} ({_ago(vk.parse_time(t['done_at']), now)})" for t in st["completed"][:5]))
            if st["changed"]:
                facts.append("Recently changed: " + "; ".join(
                    f"{t['title']} ({_ago(vk.parse_time(t['updated']), now)})" for t in st["changed"][:5]))
            facts.append(f"{name} · project {p['id']} · {'buttons change Vikunja' if writes else 'read only'}"
                         + (" · task changes posted to Discord" if entry.get("discord") is True else "")
                         + (f" · {entry['note']}" if entry.get("note") else ""))
            items = [{"id": f"project-{p['id']}", "name": f"Add a task to {p['title']}", "state": "up",
                      "url": f"{link}/projects/{p['id']}",
                      "facts": ["Type: Title words due:2026-11-03 !3 *LABEL | description (all but the title "
                                "optional; due: also takes today, tomorrow, +N)"],
                      "actions": [{"id": "add", "label": "add task",
                                   "ask": f"New task in {p['title']}: Title due:YYYY-MM-DD !priority *label | description"}]
                      if writes else []}]
            open_ = sorted((t for t in snap["tasks"] if not t.get("done")), key=_order)
            cols = [c for c, _n in st["columns"]]
            items += [_task_item(t, link, now, writes, cols) for t in open_[:MAX_ITEMS]]
            items += [_task_item(t, link, now, writes, cols) for t in st["completed"][:10]]
            sections.append({"title": p["title"], "entry": name, "state": "warn" if st["overdue"] else "ok",
                             "facts": facts, "items": items})
    return sections


def test(entry: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """The page's Test button: reads only."""
    cl = client(entry, ctx)
    try:
        version = cl.info().get("version", "?")
        who = cl.me().get("username", "?")
        projects, missing = watched(entry, cl.projects())
    except vk.VikunjaError as exc:
        return {"ok": False, "error": f"{entry.get('url')}: {exc}"}
    if missing:
        return {"ok": False, "error": f"Vikunja {version}, logged in as {who}, but no project "
                                      f"{', '.join(missing)} is visible to that user"}
    return {"ok": True, "message": f"Connected: Vikunja {version} as {who}; watching "
                                   + (", ".join(f"{p['title']} (#{p['id']})" for p in projects) or "no project yet")}


def act(entries: dict[str, dict[str, Any]], ctx: Any, entry: str, item: str, action: str) -> dict[str, Any]:
    """A button: add a task, mark done / reopen, change due date or priority, comment. Only in watched
    projects, only when the entry allows writes. Nothing here deletes."""
    e = entries[entry]
    if not writes_allowed(e):
        return {"ok": False, "error": f"{entry} is read only (allow_writes: false)"}
    typed = str(getattr(ctx, "input", "") or "").strip()
    cl = client(e, ctx)
    try:
        projects = {p["id"]: p for p in watched(e, cl.projects())[0]}
        if item.startswith("project-") and action == "add":
            pid = int(item[8:])
            if pid not in projects:
                return {"ok": False, "error": f"project {pid} is not watched by {entry}"}
            if not typed:
                return {"ok": False, "error": "nothing typed"}
            new = vk.parse_new_task(typed)
            known = {lb["title"].lower(): lb["id"] for lb in cl.labels()} if new["labels"] else {}
            unknown = [lb for lb in new["labels"] if lb.lower() not in known]
            if unknown:
                return {"ok": False, "error": f"no label {', '.join(unknown)} in Vikunja (it has: "
                                              f"{', '.join(sorted(known)) or 'none'}); Hisho doesn't make labels"}
            t = cl.create_task(pid, new["title"], new["description"], new.get("due_date"), new.get("priority"),
                               [known[lb.lower()] for lb in new["labels"]])
            return {"ok": True, "message": f"Added {t.get('identifier') or '#' + str(t['id'])} {t['title']} "
                                           f"to {projects[pid]['title']}", "task_id": t["id"]}
        if not item.startswith("task-") or not item[5:].isdigit() or action not in TASK_BUTTONS:
            return {"ok": False, "error": f"{action} on {item} is not a button of Vikunja"}
        task = cl.task(int(item[5:]))
        if task.get("project_id") not in projects:
            return {"ok": False, "error": f"task {item[5:]} is not in a project {entry} watches"}
        label = f"{task.get('identifier') or '#' + str(task['id'])} {task['title']}"
        if action in ("done", "undone"):
            cl.update_task(task["id"], done=action == "done")
            return {"ok": True, "message": f"{label}: {'done' if action == 'done' else 'reopened'}"}
        if not typed:
            return {"ok": False, "error": "nothing typed"}
        if action == "move":
            col = cl.move(task["project_id"], task["id"], typed)
            return {"ok": True, "message": f"{label}: moved to {col}"}
        if action == "due":
            due = vk.due_from_text(typed)
            cl.update_task(task["id"], due_date=due)
            d = vk.parse_time(due)
            return {"ok": True, "message": f"{label}: " + (f"due {_day(d)}" if d else "no due date")}
        if action == "priority":
            pr = vk.priority_from_text(typed)
            cl.update_task(task["id"], priority=pr)
            return {"ok": True, "message": f"{label}: priority {pr} ({vk.PRIORITIES[pr]})"}
        if action == "comment":
            cl.comment(task["id"], typed)
            return {"ok": True, "message": f"{label}: comment added"}
        return {"ok": False, "error": f"{action} is not a button of Vikunja"}
    except (vk.VikunjaError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
