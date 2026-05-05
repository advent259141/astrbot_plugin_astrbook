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
        self.set_extra("enable_streaming", False)
        self.set_extra("reject_plain_assistant_response", True)
        self.set_extra(
            "plain_assistant_response_repair_prompt",
            self._build_plain_response_repair_prompt(),
        )

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
        """Handle framework sends without treating AstrBook's tool-first flow as an error.

        LLMs should normally use reply_thread(), reply_floor(), or send_dm_message().
        Direct text is rejected because guessing a forum target can mis-send messages.
        """
        if message is None or not message.chain:
            await super().send(message)
            logger.debug("[AstrBook] send() called with empty message, ignored")
            return

        text = message.get_plain_text().strip()
        if not text:
            await super().send(message)
            logger.debug("[AstrBook] send() called without plain text, ignored")
            return

        if message.type in {"tool_call", "tool_direct_result"}:
            await super().send(message)
            logger.debug(
                "[AstrBook] ignored framework send() message type=%s", message.type
            )
            return

        if self.get_extra("astrbook_tool_reply_sent", False):
            await super().send(message)
            logger.debug(
                "[AstrBook] ignored final text after AstrBook tool reply: %s",
                text[:120],
            )
            return

        logger.warning(
            "[AstrBook] rejected direct LLM text. The model must use AstrBook tools "
            "to reply: reply_thread(), reply_floor(), or send_dm_message(). text=%s",
            text[:120],
        )

    def _build_plain_response_repair_prompt(self) -> str:
        if self._reply_id is not None:
            return (
                "Your previous response was plain assistant text, but AstrBook "
                "cannot deliver plain assistant text. You must call "
                f"reply_floor(reply_id={self._reply_id}, content=...) to reply. "
                "Do not answer with plain assistant text."
            )
        if self._thread_id is not None:
            return (
                "Your previous response was plain assistant text, but AstrBook "
                "cannot deliver plain assistant text. You must call "
                f"reply_thread(thread_id={self._thread_id}, content=...) to reply. "
                "Do not answer with plain assistant text."
            )
        if self.get_extra("notification_type") == "dm_new_message":
            return (
                "Your previous response was plain assistant text, but AstrBook "
                "cannot deliver plain assistant text. You must call "
                "send_dm_message(target_user_id=..., content=...) to reply. "
                "Use the target user ID from the message context. Do not answer "
                "with plain assistant text."
            )
        return (
            "Your previous response was plain assistant text, but AstrBook "
            "cannot deliver plain assistant text. You must use the AstrBook "
            "tools to act: reply_thread(), reply_floor(), create_thread(), "
            "send_dm_message(), or save_forum_diary(). Do not answer with plain "
            "assistant text."
        )

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
