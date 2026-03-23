---
name: cron
description: Schedule reminders and recurring tasks.
aliases: [scheduler]
---

# Cron / Scheduler

Use the `scheduler` tool (aliased as `cron`) to schedule reminders or recurring tasks.

## Three Modes

1. **Reminder** - message is sent directly to user
2. **Task** - message is a task description, agent executes and sends result
3. **One-time** - runs once at a specific time, then auto-deletes

## Examples

Fixed reminder:
```
scheduler(action="add", message="Time to take a break!", every_seconds=1200)
```

Dynamic task (agent executes each time):
```
scheduler(action="add", message="Check GitHub stars and report", every_seconds=600)
```

One-time scheduled task (compute ISO datetime from current time):
```
scheduler(action="add", message="Remind me about the meeting", at="<ISO datetime>")
```

Timezone-aware cron:
```
scheduler(action="add", message="Morning standup", cron_expr="0 9 * * 1-5", tz="Asia/Shanghai")
```

List/remove:
```
scheduler(action="list")
scheduler(action="remove", job_id="abc123")
```

## Note on Tool Names

- **New name**: `scheduler` (preferred)
- **Old name**: `cron` (still works as alias)
Both names refer to the same underlying service. Use `scheduler` in new code.

## Time Expressions

| User says | Parameters |
|-----------|------------|
| every 20 minutes | every_seconds: 1200 |
| every hour | every_seconds: 3600 |
| every day at 8am | cron_expr: "0 8 * * *" |
| weekdays at 5pm | cron_expr: "0 17 * * 1-5" |
| 9am Shanghai time daily | cron_expr: "0 9 * * *", tz: "Asia/Shanghai" |
| at a specific time | at: ISO datetime string (compute from current time) |

## Timezone

Use `tz` with `cron_expr` to schedule in a specific IANA timezone. Without `tz`, the server's local timezone is used.
