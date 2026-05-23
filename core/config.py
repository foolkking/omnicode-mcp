"""
Application configuration and settings bridged to omnicode
"""

from omnicode.config.settings import Settings, get_settings  # noqa: F401

# Global settings instance for backward compatibility
settings = get_settings()

__all__ = ["Settings", "get_settings", "settings"]
