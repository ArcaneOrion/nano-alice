"""Configuration module for nano-alice."""

from nano_alice.config.loader import load_config, get_config_path
from nano_alice.config.schema import Config

__all__ = ["Config", "load_config", "get_config_path"]
