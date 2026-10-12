"""Adapter contract for external tools.

The toolkit wraps mature tools instead of re-implementing Unity parsers.
Milestone 1 implements *discovery only* (``status()``), used by
``aniimo-pal doctor``. Extraction/conversion methods are added in milestone 2
and must keep these rules:

* Inputs are read-only. Adapters write only into a caller-provided work
  directory outside the scanned input tree.
* Adapters never decrypt, de-obfuscate or bypass protection. Files whose
  manifest entry has ``details.encryption_flag == true`` are refused.
* Every adapter run records the tool name and version in its output so results
  are traceable back to a manifest entry hash.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ToolStatus:
    name: str
    available: bool
    location: str | None = None
    version: str | None = None
    license: str = ""
    homepage: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ToolAdapter(ABC):
    name: str = ""
    license: str = ""
    homepage: str = ""
    #: Manifest ``kind`` values this tool is expected to handle.
    handles: tuple[str, ...] = ()

    @abstractmethod
    def status(self) -> ToolStatus:
        """Report whether the tool can be used on this machine. Must not run conversions."""


def find_executable(configured: str, candidates: tuple[str, ...]) -> str | None:
    """Return a configured path if it exists, else the first candidate on PATH."""
    if configured:
        p = Path(configured).expanduser()
        return str(p) if p.is_file() else None
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found
    return None
