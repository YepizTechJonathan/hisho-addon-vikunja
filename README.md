# Vikunja (Hisho add-on)

## What it does

Shows how your Vikunja projects are going, and lets you add and update tasks from Hisho:

- **The card** (overview), one line per watched project, e.g.
  `Website: 2/21 done (10%) · 1 overdue · next due Nov 3`.
- **Its page** (**Add-ons > Vikunja**), one block per project: open / done / % done, overdue count and the
  next due task, the Kanban board's columns with how many tasks each holds, what was completed (14 days)
  and changed (7 days) lately, then every open task, overdue ones first in amber, each linking to Vikunja.
- **Buttons** (unless the entry says `allow_writes: false`): **add task** on the project, and on each task
  **done** / **reopen**, **move** (to a board column, by name), **due date**, **priority** and **comment**. Typed for a new task:
  `Title words due:2026-11-03 !3 *BUG | a description`. Only the title is needed; `due:` also takes
  `today`, `tomorrow`, `+N` (days); `*LABEL` must be an existing label. From a terminal:

  ```bash
  hisho addon details vikunja                                   # tasks and their ids
  hisho addon do vikunja main project-15 add --input "Check the upload limit due:+3 !3"
  hisho addon do vikunja main task-123 done
  hisho addon do vikunja main task-123 move --input "In Progress"
  hisho addon do vikunja main task-123 due --input 2026-11-03
  hisho addon do vikunja main task-123 comment --input "Tested on dev"
  ```

  Nothing deletes: there is no delete call in this add-on at all. Buttons only touch watched projects.
- **What it never pushes.** Overdue tasks are shown, never sent to the phone. Needs you (and the phone)
  hear only about Vikunja not answering, the login failing, a watched project that went away, or a failed
  Discord post.
- **Assigned to you.** With `notify_assigned: YourVikunjaName` (comma-separate several), a task newly
  assigned to that user (and not done) sends one message to the Hisho phone app, e.g. *Vikunja: a task
  assigned to you in Website* / `#12 Fix the upload (due Nov 3) · Ready`; tapping it opens this page. It is
  a message, not an alert: nothing to acknowledge, it never repeats. Journaled as `vikunja-assigned`.
- **Problems to a team channel.** With `alerts_channel`, a problem that lasts 3 checks in a row is posted
  once to that channel (through the `chat_messages` add-on), and a ✅ line when it clears.
- **Changes are journaled.** Each check compares the tasks with the last one and writes a
  `vikunja-change` event (new task, done / reopened, moved column, title, due date, priority, progress,
  assignees, labels, gone).
- **Discord (off by default).** With `discord: true` and `discord_channel` set, the same lines go to that
  channel through Hisho's Discord add-on (your own bot; any add-on that provides `chat_messages` works), or
  with `discord_webhook` through a channel webhook (12 lines a message at most, no @mentions). If a post fails, Needs
  you says so and the next check sends it again.

## What it needs

- You need:
  - A Vikunja server (task manager) you can reach from the Core machine
  - A Vikunja user and password, or an API token
- Runs on: the Core machine, inside Hisho Core
- One entry per Vikunja server, each with:
  - `url` (url, required)
  - `user` (secret name, optional)
  - `password` (password, kept as a local secret, optional)
  - `token` (secret name, optional)
  - `projects` (str, optional)
  - `allow_writes` (bool, optional)
  - `discord` (bool, optional)
  - `discord_channel` (str, optional)
  - `discord_server` (str, optional)
  - `discord_webhook` (secret name, optional)
  - `alerts_channel` (str, optional)
  - `notify_assigned` (str, optional)
  - `note` (str, optional)

## How to install

On the dashboard: **Add-ons > Store > Vikunja > Install**. Or from a terminal:

```bash
hisho addon install vikunja
```

Hisho takes the newest version from this repository, copies it to the machine it runs on and turns
it on. Updates show as **Update available** on its Store card; **Roll back** returns to the version
before.

## How to use

Its card has one line per watched project (done, overdue, next due). Its page (**Add-ons > Vikunja**) shows each project's board and open tasks, with **add task**, **done**, **reopen**, **move** and **due date** (unless the entry says `allow_writes: false`).

## Settings

One entry per Vikunja server, on its page (**Add-ons > Vikunja**) or in
`~/Hisho/System/Config/Addons/vikunja.yaml`:

```yaml
entries:
  main:
    url: https://tasks.example.com     # Vikunja's web address
    user: VIKUNJA_USER                 # secret NAMES, never values
    password: VIKUNJA_PASSWORD
    projects: 15, Website              # ids or titles, comma-separated
    allow_writes: true                 # false = read only, no buttons
    discord: false                     # true = post task changes to the webhook below
    discord_channel: vikunja-tasks     # through the Discord add-on (or discord_webhook: SECRET_NAME)
    notify_assigned: jane              # optional: phone message when a task is assigned to jane
```

It logs in with a user name and password (`/login`) and reuses the token for 15 minutes. A Vikunja API
token (Settings > API Tokens, scoped to tasks and projects) can be given as `token` instead. For the Discord
post, set up the Discord add-on and name the channel; or make a webhook on the channel (Channel settings >
Integrations > Webhooks), store its address as a secret (`hisho secret put NAME`) and give its name as
`discord_webhook`.

## How to remove

**Add-ons > Store > Vikunja > Remove**, or `hisho addon remove vikunja`. Your settings file stays,
so installing it again picks up where you left off.

## License

Source-available under the [PolyForm Shield License 1.0.0](LICENSE.md): free to use, change and share,
for any purpose except offering a product that competes with Hisho (reselling it, for example).
