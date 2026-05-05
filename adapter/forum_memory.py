"""Forum Memory - Diary storage for AstrBook.

This module provides a shared diary storage that can be accessed
from any session (QQ, Telegram, etc.) to recall the bot's forum experiences.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot import logger
from astrbot.api.star import StarTools


@dataclass
class MemoryItem:
    """A single diary entry."""

    content: str
    """Diary content written by the agent."""

    timestamp: datetime = field(default_factory=datetime.now)
    """When this diary was created."""

    metadata: dict = field(default_factory=dict)
    """Additional metadata."""

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "memory_type": "diary",
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryItem":
        """Create from dictionary."""
        return cls(
            content=data["content"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            metadata=data.get("metadata", {}),
        )


class ForumMemory:
    """Diary storage for the bot's forum experiences.

    This class stores the bot's forum diary entries
    in a way that can be accessed from any session through LLM tools.

    Features:
    - Automatic persistence to disk
    - Memory limit to prevent unbounded growth
    - Human-readable summaries for LLM consumption
    """

    def __init__(self, max_items: int = 50, storage_dir: Path | str | None = None):
        """Initialize forum memory.

        Args:
            max_items: Maximum number of memory items to keep.
            storage_dir: Optional storage directory path. If not provided,
                         uses StarTools.get_data_dir() to get plugin data directory.
        """
        self._max_items = max_items
        self._memories: list[MemoryItem] = []

        # Determine storage path
        if storage_dir is not None:
            if isinstance(storage_dir, str):
                storage_dir = Path(storage_dir)
            self._storage_path = storage_dir / "forum_memory.json"
        else:
            # Use StarTools to get plugin data directory
            try:
                data_dir = StarTools.get_data_dir("astrbot-plugin-astrbook")
                self._storage_path = data_dir / "forum_memory.json"
            except Exception as e:
                # Fallback if StarTools is not initialized
                logger.warning(
                    f"[ForumMemory] StarTools.get_data_dir() failed: {e}, "
                    "using fallback path"
                )
                from astrbot.core.utils.astrbot_path import get_astrbot_data_path

                self._storage_path = (
                    Path(get_astrbot_data_path())
                    / "plugin_data"
                    / "astrbot-plugin-astrbook"
                    / "forum_memory.json"
                )

        # Ensure directory exists
        os.makedirs(self._storage_path.parent, exist_ok=True)

        # Load existing memories
        self._load()

    def add_diary(
        self,
        content: str,
        metadata: dict[str, Any] | None = None,
    ):
        """Add a new diary entry.

        Args:
            content: Diary content written by the agent
            metadata: Additional metadata
        """
        item = MemoryItem(
            content=content,
            metadata=metadata or {},
        )
        self._memories.append(item)

        # Trim if exceeds limit
        if len(self._memories) > self._max_items:
            self._memories = self._memories[-self._max_items :]

        # Persist to disk
        self._save()

        logger.debug(f"[ForumMemory] Added diary: {content[:50]}...")

    def get_diaries(
        self,
        limit: int | None = None,
    ) -> list[MemoryItem]:
        """Get diary entries.

        Args:
            limit: Maximum number of entries to return (optional)

        Returns:
            List of diary entries, newest first.
        """
        items = self._memories[::-1]  # Newest first

        if limit:
            items = items[:limit]

        return items

    def get_summary(self, limit: int = 10) -> str:
        """Get a human-readable summary of recent diary entries.

        This is designed to be consumed by LLM for cross-session recall.

        Args:
            limit: Maximum number of entries to include

        Returns:
            Formatted summary string.
        """
        items = self.get_diaries(limit=limit)

        if not items:
            return "还没有写过论坛日记。"

        lines = ["📔 我在 AstrBook 论坛的日记："]

        for item in items:
            time_str = item.timestamp.strftime("%m-%d %H:%M")
            lines.append(f"  📝 [{time_str}] {item.content}")

        return "\n".join(lines)

    def clear(self):
        """Clear all memories."""
        self._memories.clear()
        self._save()
        logger.info("[ForumMemory] Cleared all memories")

    def _load(self):
        """Load memories from disk."""
        if not self._storage_path.exists():
            return

        try:
            with open(self._storage_path, encoding="utf-8") as f:
                data = json.load(f)

            # Only load diary entries (filter out legacy non-diary items)
            self._memories = [
                MemoryItem.from_dict(d) for d in data if d.get("memory_type") == "diary"
            ]
            logger.debug(f"[ForumMemory] Loaded {len(self._memories)} diary entries")
        except Exception as e:
            logger.error(f"[ForumMemory] Failed to load: {e}")
            self._memories = []

    def _save(self):
        """Save memories to disk."""
        try:
            data = [m.to_dict() for m in self._memories]
            with open(self._storage_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[ForumMemory] Failed to save: {e}")

    def __len__(self) -> int:
        """Get number of memories."""
        return len(self._memories)
