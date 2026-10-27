# Changelog

## 0.5.1

- README says what you need (addon.yaml you_need) and how to use it; the README check requires both.

## 0.5.0

- **move** button: put a task in another Kanban column by name (`hisho addon do vikunja ENTRY task-ID move
  --input "In Progress"`); the start of a name is enough.

## 0.4.0

- `alerts_channel`: a problem (can't reach, can't log in, project gone) that lasts 3 checks is posted once to
  that chat channel through the `chat_messages` add-on, and again when it clears.

## 0.3.0

- `discord_channel` (and `discord_server`): post task changes through the add-on that provides
  `chat_messages` (Hisho's Discord add-on, your own bot) instead of a webhook. The webhook still works.

## 0.2.0

- `notify_assigned`: a message on the Hisho phone app when an open task is newly assigned to one of
  these Vikunja users (one message per check, journaled as `vikunja-assigned`; the first check only
  remembers, and a task unassigned and assigned again is told again).

## 0.1.0

- First version: per watched project done / total, % done, overdue, next due, Kanban column counts,
  recently completed and changed tasks; buttons to add a task (title, description, due date, priority,
  labels), mark done / reopen, change due date or priority and comment (`allow_writes: false` turns them
  off; nothing deletes). Task changes journaled as `vikunja-change`; optional Discord webhook post of them
  (off by default). Only unreachable / login / missing project / failed Discord post are problems.
