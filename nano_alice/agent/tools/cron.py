"""Cron tool for scheduling reminders and tasks.

DEPRECATED: This tool is renamed to 'scheduler'. Import from scheduler instead.
This file remains for backward compatibility during the transition period.
"""

from nano_alice.agent.tools.scheduler import SchedulerTool

# Backward compatibility alias
CronTool = SchedulerTool
