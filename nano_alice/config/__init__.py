"""Configuration module for nano-alice."""

from nano_alice.config.loader import get_config_path, load_config
from nano_alice.config.schema import Config

__all__ = ["Config", "load_config", "get_config_path"]
