"""Exception types shared across the toolkit."""


class ToolkitError(Exception):
    """Base class for expected, user-facing errors (reported without a traceback)."""


class ConfigError(ToolkitError):
    """Invalid or unreadable configuration."""


class ManifestError(ToolkitError):
    """A manifest could not be read, written or validated."""


class UnsafeOutputError(ToolkitError):
    """An output path would write inside a read-only input tree."""
