"""Le journal : chaque écriture (ordre, étiquette, approbation) laisse une ligne.

Fichier JSONL borné : au-delà de `max_bytes`, l'ancien fichier devient `.1`.
"""

from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path
from typing import Any

from .model import now_ms


class Journal:
    def __init__(self, path: Path | None, max_bytes: int = 2_000_000, keep_in_memory: int = 500):
        self.path = path
        self.max_bytes = max_bytes
        self.recent: deque[dict[str, Any]] = deque(maxlen=keep_in_memory)
        if path and path.exists():
            try:
                with path.open(encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            self.recent.append(json.loads(line))
            except (OSError, json.JSONDecodeError):
                pass

    def write(self, action: str, **fields: Any) -> dict[str, Any]:
        entry = {"ts": now_ms(), "action": action, **fields}
        self.recent.append(entry)
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                    os.replace(self.path, self.path.with_suffix(self.path.suffix + ".1"))
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError:
                pass  # le journal ne doit jamais faire tomber la maison
        return entry

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, len(self.recent) or 1, 500))
        return list(self.recent)[-limit:]
