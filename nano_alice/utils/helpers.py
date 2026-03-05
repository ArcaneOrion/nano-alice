"""Utility functions for nano-alice."""

from pathlib import Path
from datetime import datetime


def patch_httpx_for_utf8_headers() -> None:
    """
    Patch httpx to allow UTF-8 characters in response headers.

    Some proxy servers (e.g., elysiver.h-e.top) return headers containing
    non-ASCII characters (Chinese comments), which httpx 0.28.1 rejects by
    default because it enforces ASCII-only headers per HTTP spec.

    This monkey-patches httpx._models._normalize_header_value to use UTF-8
    instead of ASCII, allowing such responses to be processed.
    """
    try:
        import httpx._models as models

        def patched_normalize(value, encoding=None):
            # Handle bytes directly - just return as-is
            if isinstance(value, bytes):
                return value
            # Force UTF-8 encoding to handle non-ASCII characters in headers
            return value.encode(encoding or "utf-8")

        models._normalize_header_value = patched_normalize
    except ImportError:
        pass  # httpx not installed, nothing to patch


def ensure_dir(path: Path) -> Path:
    """Ensure a directory exists, creating it if necessary."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_data_path() -> Path:
    """Get the nano-alice data directory (~/.nano-alice)."""
    return ensure_dir(Path.home() / ".nano-alice")


def get_workspace_path(workspace: str | None = None) -> Path:
    """
    Get the workspace path.

    Args:
        workspace: Optional workspace path. Defaults to ~/.nano-alice/workspace.

    Returns:
        Expanded and ensured workspace path.
    """
    if workspace:
        path = Path(workspace).expanduser()
    else:
        path = Path.home() / ".nano-alice" / "workspace"
    return ensure_dir(path)


def get_sessions_path() -> Path:
    """Get the sessions storage directory."""
    return ensure_dir(get_data_path() / "sessions")


def get_skills_path(workspace: Path | None = None) -> Path:
    """Get the skills directory within the workspace."""
    ws = workspace or get_workspace_path()
    return ensure_dir(ws / "skills")


def timestamp() -> str:
    """Get current timestamp in ISO format."""
    return datetime.now().isoformat()


def truncate_string(s: str, max_len: int = 100, suffix: str = "...") -> str:
    """Truncate a string to max length, adding suffix if truncated."""
    if len(s) <= max_len:
        return s
    return s[: max_len - len(suffix)] + suffix


def safe_filename(name: str) -> str:
    """Convert a string to a safe filename."""
    # Replace unsafe characters
    unsafe = '<>:"/\\|?*'
    for char in unsafe:
        name = name.replace(char, "_")
    return name.strip()


def parse_session_key(key: str) -> tuple[str, str]:
    """
    Parse a session key into channel and chat_id.

    Args:
        key: Session key in format "channel:chat_id"

    Returns:
        Tuple of (channel, chat_id)
    """
    parts = key.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid session key: {key}")
    return parts[0], parts[1]
