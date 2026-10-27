"""A small Vikunja API client (tested against Vikunja v2.5): log in, read projects, tasks and Kanban
buckets, and the few writes the add-on's buttons make. There is no delete here on purpose: nothing in
the add-on can remove a task, a comment, a label or a project.

HTTP goes through one function, ``http(method, url, headers, body, timeout) -> (status, headers, data)``,
so tests can stand in for Vikunja."""
from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

ZERO = "0001-01-01T00:00:00Z"   # Vikunja's "no date"
PER_PAGE = 50                   # Vikunja's default max_items_per_page
PRIORITIES = {0: "unset", 1: "low", 2: "medium", 3: "high", 4: "urgent", 5: "DO NOW"}
UA = "Hisho-Vikunja-addon/0.1"
#: tokens from /login live about an hour on a default Vikunja; reuse one for a while, log in again on 401
TOKEN_REUSE = 15 * 60
_tokens: dict[tuple[str, str], tuple[float, str]] = {}


class VikunjaError(RuntimeError):
    """Vikunja said no, or could not be reached. ``kind`` is unreachable | login | http."""

    def __init__(self, msg: str, kind: str = "http", status: int = 0) -> None:
        super().__init__(msg)
        self.kind, self.status = kind, status


def http(method: str, url: str, headers: dict[str, str], body: Any, timeout: float) -> tuple[int, dict, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "User-Agent": UA, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as f:
            raw = f.read()
            return f.status, dict(f.headers), json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()[:400]
        try:
            msg = json.loads(raw).get("message") or ""
        except (ValueError, AttributeError):
            msg = raw.decode(errors="replace")
        return e.code, dict(e.headers or {}), {"message": msg}
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise VikunjaError(f"not reachable: {getattr(e, 'reason', e)}", "unreachable") from None


def parse_time(value: Any) -> datetime | None:
    """A Vikunja timestamp as an aware datetime; None for its zero date or nothing."""
    if not value or str(value).startswith("0001-01-01"):
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def due_from_text(text: str, now: datetime | None = None) -> str:
    """'2026-11-03', 'today', 'tomorrow', '+3' (days) or 'none' -> what Vikunja's due_date takes.
    A day means 5 pm local time that day."""
    t = text.strip().lower()
    if t in ("none", "no", "clear", "-", "0"):
        return ZERO
    now = (now or datetime.now().astimezone())
    if t in ("today", "tomorrow"):
        day = now.date() + timedelta(days=1 if t == "tomorrow" else 0)
    elif re.fullmatch(r"\+\d{1,3}d?", t):
        day = now.date() + timedelta(days=int(t.strip("+d")))
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        day = datetime.strptime(t, "%Y-%m-%d").date()
    else:
        raise ValueError(f"{text!r} is not a date: use YYYY-MM-DD, today, tomorrow, +N or none")
    local = datetime(day.year, day.month, day.day, 17, 0).astimezone()
    return local.isoformat()


def priority_from_text(text: str) -> int:
    t = text.strip().lower().lstrip("!")
    names = {v.lower(): k for k, v in PRIORITIES.items()}
    if t in names:
        return names[t]
    if t.isdigit() and int(t) in PRIORITIES:
        return int(t)
    raise ValueError(f"priority is 0-5 ({', '.join(f'{k} {v}' for k, v in PRIORITIES.items())})")


def parse_new_task(text: str) -> dict[str, Any]:
    """What was typed for "add task": ``Title words due:2026-11-03 !3 *label | description``.
    ``due:`` takes what due_from_text takes, ``!N`` a priority 1-5, ``*label`` an existing label (repeatable);
    everything after the first `` | `` is the description."""
    head, _, desc = text.partition(" | ")
    title, labels, out = [], [], {}
    for word in head.split():
        if word.lower().startswith("due:"):
            out["due_date"] = due_from_text(word[4:])
        elif re.fullmatch(r"!\d", word):
            out["priority"] = priority_from_text(word)
        elif word.startswith("*") and len(word) > 1:
            labels.append(word[1:])
        else:
            title.append(word)
    if not title:
        raise ValueError("the task needs a title")
    out.update(title=" ".join(title)[:250], labels=labels, description=desc.strip())
    return out


def as_html(text: str) -> str:
    """Vikunja keeps descriptions and comments as HTML; typed text becomes paragraphs."""
    return "".join(f"<p>{html.escape(p.strip())}</p>" for p in re.split(r"\n\s*\n|\\n", text) if p.strip())


class Client:
    def __init__(self, url: str, *, user: Callable[[], str] | None = None, password: Callable[[], str] | None = None,
                 token: Callable[[], str] | None = None, transport: Callable[..., Any] | None = None,
                 timeout: float = 15) -> None:
        self.base = url.rstrip("/")
        if self.base.endswith("/api/v1"):
            self.base = self.base[: -len("/api/v1")]
        self.api = self.base + "/api/v1"
        self._user, self._password, self._token = user, password, token
        self.transport = transport or (lambda *a: http(*a))   # looked up per call, so tests can replace http
        self.timeout = timeout

    # ── plumbing ──────────────────────────────────────────────────────────
    def _login(self, fresh: bool = False) -> str:
        if self._token:
            return self._token()
        if not (self._user and self._password):
            raise VikunjaError("no login: give a token, or a user and a password", "login")
        user = self._user()
        key = (self.base, user)
        hit = _tokens.get(key)
        if hit and not fresh and time.time() - hit[0] < TOKEN_REUSE:
            return hit[1]
        status, _, data = self.transport("POST", self.api + "/login", {},
                                         {"username": user, "password": self._password()}, self.timeout)
        if status != 200 or not isinstance(data, dict) or not data.get("token"):
            _tokens.pop(key, None)
            raise VikunjaError(f"login refused (HTTP {status}): {(data or {}).get('message') or 'no token'}",
                               "login", status)
        _tokens[key] = (time.time(), data["token"])
        return data["token"]

    def call(self, method: str, path: str, body: Any = None, *, auth: bool = True) -> tuple[Any, dict]:
        assert method in ("GET", "PUT", "POST"), "the add-on never deletes"
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {self._login(fresh=attempt == 1)}"} if auth else {}
            status, hdrs, data = self.transport(method, self.api + path, headers, body, self.timeout)
            if status == 401 and auth and attempt == 0 and not self._token:
                continue  # the reused token ran out: log in again once
            if status == 401 and auth:
                raise VikunjaError(f"login refused on {path} (HTTP 401)", "login", 401)
            if status >= 400:
                raise VikunjaError(f"{method} {path}: HTTP {status} {(data or {}).get('message') or ''}".strip(),
                                   "http", status)
            return data, {k.lower(): v for k, v in (hdrs or {}).items()}
        raise AssertionError("unreachable")

    def pages(self, path: str) -> list[dict[str, Any]]:
        out, page = [], 1
        sep = "&" if "?" in path else "?"
        while True:
            data, hdrs = self.call("GET", f"{path}{sep}per_page={PER_PAGE}&page={page}")
            out += data or []
            if page >= int(hdrs.get("x-pagination-total-pages") or 1) or not data or page >= 40:
                return out
            page += 1

    # ── reads ─────────────────────────────────────────────────────────────
    def info(self) -> dict[str, Any]:
        return self.call("GET", "/info", auth=False)[0]

    def me(self) -> dict[str, Any]:
        return self.call("GET", "/user")[0]

    def projects(self) -> list[dict[str, Any]]:
        return self.pages("/projects?is_archived=true")

    def tasks(self, project_id: int) -> list[dict[str, Any]]:
        """Every task of the project, done ones too."""
        return [t for t in self.pages(f"/tasks?filter=project_id+%3D+{int(project_id)}")
                if t.get("project_id") == int(project_id)]

    def buckets(self, project_id: int) -> list[dict[str, Any]]:
        """The project's Kanban columns with their tasks' ids (empty when it has no Kanban view)."""
        kanban = self.kanban(project_id)
        if not kanban:
            return []
        data = self.call("GET", f"/projects/{int(project_id)}/views/{kanban['id']}/tasks?per_page={PER_PAGE}")[0] or []
        done = kanban.get("done_bucket_id")
        return [{"id": b["id"], "title": b.get("title") or f"bucket {b['id']}", "done": b["id"] == done,
                 "tasks": [t["id"] for t in b.get("tasks") or []]} for b in data]

    def kanban(self, project_id: int) -> dict[str, Any] | None:
        views = self.call("GET", f"/projects/{int(project_id)}/views")[0] or []
        return next((v for v in views if v.get("view_kind") == "kanban"), None)

    def task(self, task_id: int) -> dict[str, Any]:
        return self.call("GET", f"/tasks/{int(task_id)}")[0]

    def labels(self) -> list[dict[str, Any]]:
        return self.pages("/labels")

    # ── writes (the add-on's buttons; never a delete) ─────────────────────
    def update_task(self, task_id: int, **changes: Any) -> dict[str, Any]:
        """Vikunja's update takes the whole task, so read it, change the fields, send it back."""
        task = self.task(task_id)
        task.update(changes)
        return self.call("POST", f"/tasks/{int(task_id)}", task)[0]

    def create_task(self, project_id: int, title: str, description: str = "", due_date: str | None = None,
                    priority: int | None = None, label_ids: list[int] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"title": title, "description": as_html(description) if description else ""}
        if due_date:
            body["due_date"] = due_date
        if priority is not None:
            body["priority"] = priority
        task = self.call("PUT", f"/projects/{int(project_id)}/tasks", body)[0]
        for lid in label_ids or []:
            self.call("PUT", f"/tasks/{task['id']}/labels", {"label_id": int(lid)})
        return task

    def move(self, project_id: int, task_id: int, column: str) -> str:
        """Put the task in the Kanban column named ``column`` (Vikunja marks it done in the done column and
        reopens it when it leaves). Returns the column's title."""
        kanban = self.kanban(project_id)
        if not kanban:
            raise VikunjaError("this project has no Kanban board")
        buckets = self.call("GET", f"/projects/{int(project_id)}/views/{kanban['id']}/buckets")[0] or []
        want = column.strip().lower()
        b = (next((b for b in buckets if b["title"].lower() == want), None)
             or next((b for b in buckets if b["title"].lower().startswith(want)), None))
        if not b:
            raise VikunjaError(f"no column {column!r} (columns: {', '.join(x['title'] for x in buckets)})")
        self.call("POST", f"/projects/{int(project_id)}/views/{kanban['id']}/buckets/{b['id']}/tasks",
                  {"task_id": int(task_id), "bucket_id": b["id"], "project_view_id": kanban["id"]})
        return b["title"]

    def comment(self, task_id: int, text: str) -> dict[str, Any]:
        return self.call("PUT", f"/tasks/{int(task_id)}/comments", {"comment": as_html(text)})[0]


def post_discord(webhook_url: str, content: str, transport: Callable[..., Any] | None = None) -> None:
    """One message to a Discord webhook. The webhook's address is a secret: it never goes in an error."""
    status, _, data = (transport or http)("POST", webhook_url + "?wait=false", {},
                                {"content": content[:1990], "allowed_mentions": {"parse": []}}, 20)
    if status >= 300:
        raise VikunjaError(f"Discord webhook answered HTTP {status}", "http", status)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
