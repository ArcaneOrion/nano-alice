# Heartbeat Tasks

- Use this file for durable background checks, recurring follow-ups, and tasks the agent should revisit on heartbeat ticks.
- Keep only active instructions here. Remove or clear completed tasks so heartbeat can stay quiet.
- Prefer concrete checks, cadence expectations, and push criteria.

## Examples

- Every workday morning, check today's calendar and draft a concise agenda if there are important events.
- Watch a long-running job and notify the user only when it finishes or fails.
- Re-check a waiting dependency every 30 minutes and push an update only when status changes.
