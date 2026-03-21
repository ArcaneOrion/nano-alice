"""Chat channels module with plugin architecture."""

from nano_alice.channels.base import BaseChannel
from nano_alice.channels.manager import ChannelManager

__all__ = ["BaseChannel", "ChannelManager"]
