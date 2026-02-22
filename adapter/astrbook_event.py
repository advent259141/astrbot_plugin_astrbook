"""AstrBook Message Event - Event class for forum interactions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain

if TYPE_CHECKING:
    from .astrbook_adapter import AstrBookAdapter


class AstrBookMessageEvent(AstrMessageEvent):
    """Message event for AstrBook forum interactions.

    This event class handles forum interactions.
    Note: LLM uses tools (reply_thread, reply_floor) to send messages,
    so send() method is a no-op for AstrBook.
    """

    def __init__(
        self,
        message_str: str,
        message_obj,
        platform_meta,
        session_id: str,
        adapter: AstrBookAdapter,
        thread_id: int | None = None,
        reply_id: int | None = None,
    ):
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self._adapter = adapter
        self._thread_id = thread_id
        self._reply_id = reply_id

    @property
    def adapter(self) -> AstrBookAdapter:
        """Get the adapter instance."""
        return self._adapter

    @property
    def thread_id(self) -> int | None:
        """Get the thread ID this event is associated with."""
        return self._thread_id

    @property
    def reply_id(self) -> int | None:
        """Get the reply ID this event is responding to."""
        return self._reply_id

    async def send(self, message: MessageChain | None):
        """Send method - LLM uses tools to send messages, so this is a no-op.
        
        For AstrBook, the LLM should use reply_thread() or reply_floor() tools
        to send messages to the forum.
        """
        # LLM uses tools (reply_thread, reply_floor) to send messages
        # This method is intentionally a no-op to avoid double sending
        logger.error("[AstrBook] send() called - LLM should use tools to reply")
        pass

    async def send_streaming(self, message_chain: MessageChain):
        """Streaming send - not supported for forum."""
        logger.debug("[AstrBook] send_streaming() called - not supported for forum")
        pass

    def get_thread_context(self) -> dict:
        """Get context information about the current thread.

        Useful for plugins that need to know the forum context.
        """
        return {
            "thread_id": self._thread_id,
            "thread_title": self.get_extra("thread_title"),
            "reply_id": self._reply_id,
            "notification_type": self.get_extra("notification_type"),
            "is_browse_event": self.get_extra("is_browse_event", False),
        }
