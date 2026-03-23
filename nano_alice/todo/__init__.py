"""TODO service for periodic task checking.

This module was renamed from 'heartbeat' to 'todo' to better reflect
its purpose: checking TODO.md for pending tasks.
"""

from nano_alice.todo.service import TODOService

__all__ = ["TODOService"]
