"""Journalisation : console + fichier + tampon mémoire consultable dans l'interface.

Tous les messages passent par `redact()` : clés privées, jetons OAuth/JWT et en-têtes
Authorization ne sont JAMAIS écrits dans les logs, même par accident.
Variable d'environnement LOG_LEVEL (DEBUG par défaut) : niveau de la console.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOG_DIR / "converter.log"
LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]
# Loggers tiers utiles pour comprendre un blocage réseau/auth (requêtes HTTP, jetons)
WATCHED_LOGGERS = ("converter", "urllib3.connectionpool", "google.auth", "gspread")

_SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[A-Za-z0-9+/=\s\\]{40,}(?:-----END [A-Z ]*PRIVATE KEY-----)?"), "[CLÉ PRIVÉE MASQUÉE]"),
    (re.compile(r"(?i)(private_key\"?\s*[:=]\s*)\"[^\"]*\""), r'\1"[MASQUÉ]"'),
    (re.compile(r"ya29\.[A-Za-z0-9_\-\.]+"), "[JETON MASQUÉ]"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]*"), "[JWT MASQUÉ]"),
    (re.compile(r"(?i)(authorization\"?\s*[:=]\s*)(bearer\s+)?\S+"), r"\1[MASQUÉ]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.]{20,}"), r"\1[MASQUÉ]"),
]


def redact(text: str) -> str:
    """Masque tout ce qui ressemble à un secret."""
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


@dataclass
class LogEntry:
    time: str
    level: str
    logger: str
    message: str

    def line(self) -> str:
        return f"{self.time} | {self.level:<7} | {self.logger} | {self.message}"


class RingBufferHandler(logging.Handler):
    """Garde les N derniers messages en mémoire (affichés dans l'onglet Journal)."""

    def __init__(self, capacity: int = 2000):
        super().__init__(logging.DEBUG)
        self._entries: deque[LogEntry] = deque(maxlen=capacity)
        self._guard = threading.Lock()
        self._exc_formatter = logging.Formatter()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + self._exc_formatter.formatException(record.exc_info)
            entry = LogEntry(
                time=time.strftime("%H:%M:%S", time.localtime(record.created)),
                level=record.levelname,
                logger=record.name,
                message=redact(message),
            )
            with self._guard:
                self._entries.append(entry)
        except Exception:  # noqa: BLE001 - un log ne doit jamais faire planter l'app
            self.handleError(record)

    def entries(self) -> list[LogEntry]:
        with self._guard:
            return list(self._entries)

    def clear(self) -> None:
        with self._guard:
            self._entries.clear()


BUFFER = RingBufferHandler()
_setup_lock = threading.Lock()


def setup_logging(console_level: Optional[str] = None) -> None:
    """Installe les handlers (idempotent : peut être appelé à chaque exécution du script Streamlit)."""
    with _setup_lock:
        level = (console_level or os.environ.get("LOG_LEVEL", "DEBUG")).upper()
        fmt = RedactingFormatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", "%H:%M:%S")

        console = logging.StreamHandler(sys.stderr)
        console.setLevel(getattr(logging, level, logging.DEBUG))
        console.setFormatter(fmt)
        handlers: list[logging.Handler] = [BUFFER, console]
        try:
            LOG_DIR.mkdir(exist_ok=True)
            file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
            file_handler.setFormatter(fmt)
            handlers.append(file_handler)
        except OSError:  # dossier en lecture seule (hébergement) : on continue sans fichier
            pass

        for name in WATCHED_LOGGERS:
            logger = logging.getLogger(name)
            for old in [h for h in logger.handlers if getattr(h, "_converter_handler", False)]:
                logger.removeHandler(old)  # évite les doublons si le module est rechargé
            logger.setLevel(logging.DEBUG)
            logger.propagate = False
            for h in handlers:
                h._converter_handler = True  # type: ignore[attr-defined]
                logger.addHandler(h)


def get_entries(min_level: str = "DEBUG") -> list[LogEntry]:
    threshold = LEVELS.index(min_level) if min_level in LEVELS else 0
    return [e for e in BUFFER.entries() if LEVELS.index(e.level if e.level in LEVELS else "ERROR") >= threshold]


def entries_text(min_level: str = "DEBUG") -> str:
    return "\n".join(e.line() for e in get_entries(min_level))


def clear_entries() -> None:
    BUFFER.clear()
