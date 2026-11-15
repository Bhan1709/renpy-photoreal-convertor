"""Context-window monitoring and conversation backups for OpenCode sessions."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterable, Mapping
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import tiktoken

logger = logging.getLogger(__name__)

DEFAULT_SESSION_PATH: Final = (
    Path.home() / ".config" / "opencode" / "cache" / "current_session.json"
)
DEFAULT_BACKUP_DIRECTORY: Final = Path("logs") / "conversations"
DEFAULT_POLL_INTERVAL_SECONDS: Final = 5.0
DEFAULT_BACKUP_COOLDOWN_SECONDS: Final = 60.0
CONTEXT_RESERVE_TOKENS: Final = 4096
TOKEN_ENCODING_NAME: Final = "cl100k_base"


@lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding(TOKEN_ENCODING_NAME)


def calculate_tokens_from_text(text: str) -> int:
    """Return the number of tokens in ``text`` using OpenCode's tokenizer."""
    return len(_encoding().encode(text))


def render_backup(
    *,
    profile_key: str,
    model_name: str,
    context_limit: int,
    token_count: int,
    messages: Iterable[Mapping[str, Any]],
    timestamp: datetime,
) -> str:
    """Render a conversation snapshot as Markdown."""
    lines = [
        (
            f"# Context Backup | Profile: {profile_key} | Model: {model_name} "
            f"({token_count}/{context_limit} tokens)"
        ),
        f"- **Timestamp:** {timestamp.isoformat()}\n",
    ]
    for message in messages:
        role = str(message.get("role", "user")).upper()
        content = message.get("content", "")
        lines.append(f"### {role}\n{content}\n")
    return "\n".join(lines)


class ConversationMonitor:
    """Watch an OpenCode session and save Markdown backups near the context limit."""

    def __init__(
        self,
        *,
        profile_key: str,
        model_name: str,
        context_limit: int,
        session_path: Path = DEFAULT_SESSION_PATH,
        backup_directory: Path = DEFAULT_BACKUP_DIRECTORY,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        backup_cooldown: float = DEFAULT_BACKUP_COOLDOWN_SECONDS,
        backup_threshold: int | None = None,
    ) -> None:
        self.profile_key = profile_key
        self.model_name = model_name
        self.context_limit = context_limit
        self.session_path = session_path
        self.backup_directory = backup_directory
        self.poll_interval = poll_interval
        self.backup_cooldown = backup_cooldown
        self.backup_threshold = (
            max(context_limit - CONTEXT_RESERVE_TOKENS, 0)
            if backup_threshold is None
            else backup_threshold
        )

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_signature: tuple[int, int] | None = None
        self._last_backup_time = 0.0

    @property
    def is_running(self) -> bool:
        """Whether the background monitor thread is currently running."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the background monitoring thread."""
        if self.is_running:
            raise RuntimeError("Conversation monitor is already running.")

        self._stop_event.clear()
        self._last_signature = None
        self._last_backup_time = 0.0
        self._thread = threading.Thread(
            target=self._run,
            name=f"context-monitor-{self.profile_key}",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "👀 Backup monitor active for [%s] -> %s (Limit: %d tokens)",
            self.profile_key,
            self.model_name,
            self.context_limit,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the background monitor thread and wait for it to finish."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning(
                    "Conversation monitor did not stop within %.1f seconds.", timeout
                )
        self._thread = None

    def check_once(self) -> Path | None:
        """Inspect the session once, writing a backup when the threshold is reached."""
        messages = self._read_messages()
        if not messages:
            return None

        total_text = "".join(str(message.get("content", "")) for message in messages)
        signature = (len(messages), len(total_text))
        if signature == self._last_signature:
            return None
        self._last_signature = signature

        token_count = calculate_tokens_from_text(total_text)
        if token_count < self.backup_threshold:
            return None

        now = time.monotonic()
        if now - self._last_backup_time < self.backup_cooldown:
            return None

        backup_path = self._write_backup(messages, token_count)
        self._last_backup_time = now
        logger.info(
            "💾 [Monitor] Saved conversation backup (%d tokens) -> %s",
            token_count,
            backup_path,
        )
        return backup_path

    def _run(self) -> None:
        while not self._stop_event.wait(self.poll_interval):
            try:
                self.check_once()
            except Exception:
                logger.exception(
                    "Unexpected error while monitoring the OpenCode session."
                )

    def _read_messages(self) -> list[Mapping[str, Any]]:
        try:
            data = json.loads(self.session_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            # OpenCode may be writing the file while we read it; retry on the next poll.
            logger.debug("Could not read session file %s: %s", self.session_path, exc)
            return []

        if not isinstance(data, Mapping):
            return []

        messages = data.get("messages")
        if not isinstance(messages, list):
            return []

        return [message for message in messages if isinstance(message, Mapping)]

    def _write_backup(
        self, messages: Iterable[Mapping[str, Any]], token_count: int
    ) -> Path:
        timestamp = datetime.now()
        backup_path = (
            self.backup_directory
            / f"{self.profile_key}_backup_{timestamp:%Y%m%d_%H%M%S}.md"
        )
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path.write_text(
            render_backup(
                profile_key=self.profile_key,
                model_name=self.model_name,
                context_limit=self.context_limit,
                token_count=token_count,
                messages=messages,
                timestamp=timestamp,
            ),
            encoding="utf-8",
        )
        return backup_path
