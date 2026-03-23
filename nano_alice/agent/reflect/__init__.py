"""Reflection processor for internal signal handling.

This module processes internal signals in Reflect Mode, separate from
Chat Mode used for user conversations.
"""

from nano_alice.agent.reflect.internal_state import InternalState
from nano_alice.agent.reflect.processor import ReflectProcessor

__all__ = ["ReflectProcessor", "InternalState"]
