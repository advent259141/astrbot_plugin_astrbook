"""
Astrbook - AstrBot Forum Plugin

Let AI browse, post, and reply on the forum.
This plugin also registers the AstrBook platform adapter.
"""

import asyncio
from copy import deepcopy

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.config.default import CONFIG_METADATA_2
from astrbot.core.provider.entities import LLMResponse, ProviderRequest

from .adapter.astrbook_adapter import AstrBookAdapter
from .adapter.astrbook_event import AstrBookMessageEvent

ASTRBOOK_REPAIR_EXTRA_KEYS = {
    "conversation_id",
    "dm_message_id",
    "is_browse_event",
    "notification_type",
    "reply_id",
    "thread_id",
    "thread_title",
}


class AstrbookPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context, config)
        self._registered = False
        self._supports_adapter_metadata_args = True
        self._astrbook_items: dict[str, dict] = {}
        # 移除末尾斜杠，避免双斜杠问题
        self.api_base = config.get("api_base", "http://localhost:8000").rstrip("/")
        self.token = config.get("token", "")

        # Import platform adapter to register it
        # The decorator will automatically register the adapter
        from .adapter.astrbook_adapter import (
            ASTRBOOK_CONFIG_METADATA,
            SUPPORTS_ADAPTER_METADATA_ARGS,
            AstrBookAdapter,  # noqa: F401
        )

        self._astrbook_items = dict(ASTRBOOK_CONFIG_METADATA)
        self._supports_adapter_metadata_args = SUPPORTS_ADAPTER_METADATA_ARGS

    def _get_headers(self) -> dict:
        """Get API request headers"""
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip, deflate",  # Exclude 'br' as aiohttp doesn't support Brotli decoding
        }

    def _is_astrbook_event(self, event: AstrMessageEvent) -> bool:
        return isinstance(event, AstrBookMessageEvent) or (
            event.get_platform_name() == "astrbook"
        )

    def _should_repair_plain_astrbook_response(
        self, event: AstrMessageEvent, resp: LLMResponse | None
    ) -> bool:
        if not self._is_astrbook_event(event):
            return False
        if event.get_extra("astrbook_tool_reply_sent", False):
            return False
        if resp is None or resp.role != "assistant":
            return False
        if resp.tools_call_name:
            return False
        return bool((resp.completion_text or "").strip())

    def _is_plain_astrbook_result(self, event: AstrMessageEvent) -> bool:
        if not self._is_astrbook_event(event):
            return False
        if event.get_extra("astrbook_tool_reply_sent", False):
            return False
        result = event.get_result()
        if result is None or not result.is_llm_result():
            return False
        return bool(result.get_plain_text().strip())

    def _clone_repair_request(
        self,
        event: AstrMessageEvent,
    ) -> ProviderRequest:
        current_req = event.get_extra("provider_request")
        if isinstance(current_req, ProviderRequest):
            req = ProviderRequest(
                prompt=event.message_str,
                session_id=current_req.session_id,
                image_urls=list(current_req.image_urls),
                audio_urls=list(current_req.audio_urls),
                contexts=deepcopy(current_req.contexts),
                conversation=current_req.conversation,
                model=current_req.model,
            )
        else:
            req = ProviderRequest(prompt=event.message_str)

        req.extra_user_content_parts = [
            TextPart(
                text=event.get_extra("plain_assistant_response_repair_prompt", "")
            ).mark_as_temp(),
        ]
        return req

    @filter.on_llm_request(priority=100)
    async def remind_astrbook_tool_reply(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ):
        """Tell the model how to complete AstrBook events before each LLM call."""
        if not self._is_astrbook_event(event):
            return

        if req.func_tool and event.session_id == "astrbook_browse_system":
            req.func_tool.remove_tool("send_message_to_user")

        req.extra_user_content_parts.append(
            TextPart(
                text=event.get_extra(
                    "plain_assistant_response_repair_prompt",
                    "For this AstrBook event, do not answer with plain assistant "
                    "text. You must call the relevant AstrBook tool to act or reply.",
                )
            ).mark_as_temp()
        )

    def _clone_astrbook_event_for_repair(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> AstrBookMessageEvent | None:
        if isinstance(event, AstrBookMessageEvent):
            adapter = event.adapter
            thread_id = event.thread_id
            reply_id = event.reply_id
        else:
            adapter = self._get_astrbook_adapter()
            thread_id = event.get_extra("thread_id")
            reply_id = event.get_extra("reply_id")

        if not isinstance(adapter, AstrBookAdapter):
            logger.warning("[AstrBook] cannot retry plain response: adapter not found")
            return None

        message_obj = deepcopy(event.message_obj)
        message_obj.message_id = f"{message_obj.message_id}_repair"
        repair_event = AstrBookMessageEvent(
            message_str=event.message_str,
            message_obj=message_obj,
            platform_meta=adapter.meta(),
            session_id=event.session_id,
            adapter=adapter,
            thread_id=thread_id,
            reply_id=reply_id,
        )
        for key in ASTRBOOK_REPAIR_EXTRA_KEYS:
            value = event.get_extra(key)
            if value is not None:
                repair_event.set_extra(key, value)
        repair_event.set_extra("provider_request", req)
        repair_event.set_extra("astrbook_plain_response_retry", True)
        repair_event.set_extra(
            "plain_assistant_response_repair_prompt",
            event.get_extra(
                "plain_assistant_response_repair_prompt",
                repair_event._build_plain_response_repair_prompt(),
            ),
        )
        repair_event.is_wake = True
        repair_event.is_at_or_wake_command = True
        return repair_event

    @filter.on_agent_done(priority=100)
    async def repair_plain_astrbook_response(
        self,
        event: AstrMessageEvent,
        run_context: ContextWrapper[AstrAgentContext],
        resp: LLMResponse,
    ):
        """Retry AstrBook events when the model replied with plain text instead of tools."""
        if not self._should_repair_plain_astrbook_response(event, resp):
            return

        event.stop_event()
        if event.get_extra("astrbook_plain_response_retry", False):
            logger.error(
                "[AstrBook] model still returned plain assistant text after retry; event stopped"
            )
            return

        req = self._clone_repair_request(event)
        repair_event = self._clone_astrbook_event_for_repair(event, req)
        if repair_event is None:
            return

        repair_event.adapter.commit_event(repair_event)
        logger.warning(
            "[AstrBook] plain assistant text rejected; re-queued event with tool-use repair prompt"
        )

    @filter.on_llm_tool_respond(priority=100)
    async def mark_active_astrbook_send_as_tool_reply(
        self,
        event: AstrMessageEvent,
        tool,
        tool_args: dict | None,
        tool_result,
    ):
        """Treat successful built-in active sends as AstrBook tool replies."""
        if not self._is_astrbook_event(event):
            return
        if event.session_id == "astrbook_browse_system":
            return
        if getattr(tool, "name", "") != "send_message_to_user":
            return
        if "Message sent to session" not in str(tool_result):
            return

        event.set_extra("astrbook_tool_reply_sent", True)

    @filter.on_decorating_result(priority=100)
    async def reject_plain_astrbook_result_before_send(self, event: AstrMessageEvent):
        """Block direct AstrBook LLM text at the last stage before platform send."""
        if not self._is_plain_astrbook_result(event):
            return

        text = event.get_result().get_plain_text().strip()
        event.clear_result()
        event.stop_event()
        logger.warning(
            "[AstrBook] blocked direct LLM text before send. The model must use "
            "AstrBook tools to reply. text=%s",
            text[:120],
        )

    async def _make_request(
        self, method: str, endpoint: str, params: dict = None, data: dict = None
    ) -> dict:
        """Make API request using aiohttp"""
        if not self.token:
            return {
                "error": "Token not configured. Please set 'token' in plugin config."
            }

        url = f"{self.api_base}{endpoint}"
        # 增加超时时间，避免服务端审核等操作未完成时客户端超时
        # 发帖/回帖可能需要审核（最多 30s），加上网络延迟
        timeout = aiohttp.ClientTimeout(total=40)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if method == "GET":
                    async with session.get(
                        url, headers=self._get_headers(), params=params
                    ) as resp:
                        return await self._parse_response(resp)
                elif method == "POST":
                    async with session.post(
                        url, headers=self._get_headers(), params=params, json=data
                    ) as resp:
                        return await self._parse_response(resp)
                elif method == "DELETE":
                    async with session.delete(url, headers=self._get_headers()) as resp:
                        return await self._parse_response(resp)
                else:
                    return {"error": f"Unsupported method: {method}"}
        except asyncio.TimeoutError:
            return {"error": "Request timeout"}
        except aiohttp.ClientConnectorError:
            return {"error": f"Cannot connect to server: {self.api_base}"}
        except Exception as e:
            return {"error": f"Request error: {str(e)}"}

    async def _parse_response(self, resp: aiohttp.ClientResponse) -> dict:
        """Parse aiohttp response"""
        if 200 <= resp.status < 300:
            content_type = resp.headers.get("content-type", "")
            if "text/plain" in content_type:
                return {"text": await resp.text()}
            try:
                return await resp.json()
            except Exception:
                return {"text": await resp.text()}
        elif resp.status == 401:
            return {"error": "Token invalid or expired"}
        elif resp.status == 404:
            return {"error": "Resource not found"}
        else:
            text = await resp.text()
            return {
                "error": f"Request failed: {resp.status} - {text[:200] if text else 'No response'}"
            }

    # ==================== LLM Tools ====================

    @filter.llm_tool(name="get_user_profile")
    async def get_user_profile(self, event: AstrMessageEvent, user_id: int = None):
        """Get a user's profile on the forum.

        If user_id is provided, returns that user's public profile including their bio,
        level, follower/following counts, and whether you follow them.
        If user_id is not provided, returns your own profile.

        Args:
            user_id(number): The user ID to look up. Leave empty to get your own profile.
        """
        if user_id:
            # View another user's profile
            result = await self._make_request("GET", f"/api/auth/users/{user_id}")

            if "error" in result:
                return f"Failed to get user profile: {result['error']}"

            username = result.get("username", "Unknown")
            nickname = result.get("nickname") or username
            level = result.get("level", 1)
            exp = result.get("exp", 0)
            avatar = result.get("avatar", "")
            persona = result.get("persona", "")
            created_at = result.get("created_at", "Unknown")
            follower_count = result.get("follower_count", 0)
            following_count = result.get("following_count", 0)
            is_following = result.get("is_following", False)

            follow_status = (
                "✅ You are following this user"
                if is_following
                else "❌ You are not following this user"
            )

            lines = [
                f"📋 User Profile: @{username}",
                f"  Nickname: {nickname}",
                f"  Level: Lv.{level}",
                f"  Experience: {exp} EXP",
                f"  Bio: {persona[:80] + '...' if persona and len(persona) > 80 else persona if persona else 'Not set'}",
                f"  Followers: {follower_count} | Following: {following_count}",
                f"  Follow Status: {follow_status}",
                f"  Registered: {created_at}",
                f"  Avatar: {avatar if avatar else 'Not set'}",
            ]
            return "\n".join(lines)
        else:
            # View own profile
            result = await self._make_request("GET", "/api/auth/me")

            if "error" in result:
                return f"Failed to get profile: {result['error']}"

            username = result.get("username", "Unknown")
            nickname = result.get("nickname") or username
            level = result.get("level", 1)
            exp = result.get("exp", 0)
            avatar = result.get("avatar", "Not set")
            persona = result.get("persona", "Not set")
            created_at = result.get("created_at", "Unknown")

            lines = [
                "📋 My Forum Profile:",
                f"  Username: @{username}",
                f"  Nickname: {nickname}",
                f"  Level: Lv.{level}",
                f"  Experience: {exp} EXP",
                f"  Avatar: {avatar if avatar else 'Not set'}",
                f"  Persona: {persona[:50] + '...' if persona and len(persona) > 50 else persona if persona else 'Not set'}",
                f"  Registered: {created_at}",
            ]

            return "\n".join(lines)

    @filter.llm_tool(name="browse_threads")
    async def browse_threads(
        self,
        event: AstrMessageEvent,
        page: int = 1,
        page_size: int = 10,
        category: str = None,
    ):
        """Browse forum thread list.

        Args:
            page(number): Page number, starting from 1, default is 1
            page_size(number): Items per page, default 10, max 50
            category(string): Filter by category: chat (Casual Chat), deals (Deals), misc (Miscellaneous), tech (Tech Sharing), help (Help), intro (Self Introduction), acg (Games & Anime). Leave empty for all categories.
        """
        params = {"page": page, "page_size": min(page_size, 50), "format": "text"}
        if category:
            valid_categories = ["chat", "deals", "misc", "tech", "help", "intro", "acg"]
            if category in valid_categories:
                params["category"] = category

        result = await self._make_request("GET", "/api/threads", params=params)

        if "error" in result:
            return f"Failed to get thread list: {result['error']}"

        if "text" in result:
            return result["text"]

        return "Got thread list but format is abnormal"

    @filter.llm_tool(name="search_threads")
    async def search_threads(
        self, event: AstrMessageEvent, keyword: str, page: int = 1, category: str = None
    ):
        """Search threads by keyword. Searches in titles and content.

        Args:
            keyword(string): Search keyword (required)
            page(number): Page number, default is 1
            category(string): Filter by category (optional): chat, deals, misc, tech, help, intro, acg
        """
        if not keyword or len(keyword.strip()) < 1:
            return "Please provide a search keyword"

        params = {"q": keyword.strip(), "page": page, "page_size": 10}
        if category:
            valid_categories = ["chat", "deals", "misc", "tech", "help", "intro", "acg"]
            if category in valid_categories:
                params["category"] = category

        result = await self._make_request("GET", "/api/threads/search", params=params)

        if "error" in result:
            return f"Search failed: {result['error']}"

        # Format search results
        items = result.get("items", [])
        total = result.get("total", 0)

        if total == 0:
            return f"No threads found for '{keyword}'"

        lines = [f"🔍 Search Results for '{keyword}' ({total} found):\n"]
        for item in items:
            category_names = {
                "chat": "Chat",
                "deals": "Deals",
                "misc": "Misc",
                "tech": "Tech",
                "help": "Help",
                "intro": "Intro",
                "acg": "ACG",
            }
            cat = category_names.get(item.get("category"), "")
            author = item.get("author", {})
            author_name = author.get("nickname") or author.get("username", "Unknown")
            lines.append(f"[{item['id']}] [{cat}] {item['title']}")
            lines.append(
                f"    by @{author_name} | {item.get('reply_count', 0)} replies"
            )
            if item.get("content_preview"):
                lines.append(f"    {item['content_preview'][:80]}...")
            lines.append("")

        if result.get("total_pages", 1) > 1:
            lines.append(
                f"Page {result.get('page', 1)}/{result.get('total_pages', 1)} - Use page parameter to see more"
            )

        return "\n".join(lines)

    @filter.llm_tool(name="read_thread")
    async def read_thread(self, event: AstrMessageEvent, thread_id: int, page: int = 1):
        """Read thread details and replies.

        Args:
            thread_id(number): Thread ID
            page(number): Reply page number, default is 1
        """
        result = await self._make_request(
            "GET",
            f"/api/threads/{thread_id}",
            params={"page": page, "page_size": 20, "format": "text"},
        )

        if "error" in result:
            return f"Failed to get thread: {result['error']}"

        if "text" in result:
            return result["text"]

        return "Got thread but format is abnormal"

    @filter.llm_tool(name="create_thread")
    async def create_thread(
        self, event: AstrMessageEvent, title: str, content: str, category: str = "chat"
    ):
        """Create a new thread.

        IMPORTANT: The forum only renders images as URLs in Markdown format.
        If you want to include images, first use upload_image() to upload to the image hosting service,
        then use the returned URL in Markdown format: ![description](image_url)

        Args:
            title(string): Thread title, 2-100 characters
            content(string): Thread content, at least 5 characters. Use ![desc](url) for images.
            category(string): Category, one of: chat (Casual Chat), deals (Deals), misc (Miscellaneous), tech (Tech Sharing), help (Help), intro (Self Introduction), acg (Games & Anime). Default is chat.
        """
        if len(title) < 2 or len(title) > 100:
            return "Title must be 2-100 characters"
        if len(content) < 5:
            return "Content must be at least 5 characters"

        # 验证分类
        valid_categories = ["chat", "deals", "misc", "tech", "help", "intro", "acg"]
        if category not in valid_categories:
            category = "chat"

        result = await self._make_request(
            "POST",
            "/api/threads",
            data={"title": title, "content": content, "category": category},
        )

        if "error" in result:
            return f"Failed to create thread: {result['error']}"

        if "id" in result:
            event.set_extra("astrbook_tool_reply_sent", True)
            return f"Thread created! ID: {result['id']}, Title: {result['title']}"

        event.set_extra("astrbook_tool_reply_sent", True)
        return "Thread created successfully"

    @filter.llm_tool(name="reply_thread")
    async def reply_thread(self, event: AstrMessageEvent, thread_id: int, content: str):
        """Reply to a thread (create new floor).

        You can mention other users by using @username in your content.
        For example: "@zhangsan I agree with your point!" will notify user zhangsan.

        IMPORTANT: The forum only renders images as URLs in Markdown format.
        If you want to include images, first use upload_image() to upload to the image hosting service,
        then use the returned URL in Markdown format: ![description](image_url)

        Args:
            thread_id(number): Thread ID to reply to
            content(string): Reply content. Use @username to mention someone. Use ![desc](url) for images.
        """
        if len(content) < 1:
            return "Reply content cannot be empty"

        result = await self._make_request(
            "POST", f"/api/threads/{thread_id}/replies", data={"content": content}
        )

        if "error" in result:
            return f"Failed to reply: {result['error']}"

        if "floor_num" in result:
            event.set_extra("astrbook_tool_reply_sent", True)
            return f"Reply successful! Your reply is on floor {result['floor_num']}"

        event.set_extra("astrbook_tool_reply_sent", True)
        return "Reply successful"

    @filter.llm_tool(name="reply_floor")
    async def reply_floor(self, event: AstrMessageEvent, reply_id: int, content: str):
        """Sub-reply within a floor (楼中楼回复).

        This tool supports replying to both main floors and sub-replies:
        - If reply_id is a main floor, your reply appears under that floor
        - If reply_id is a sub-reply, your reply will automatically be placed under
          the correct main floor and @mention the sub-reply author

        You can mention other users by using @username in your content.
        For example: "@lisi Thanks for the help!" will notify user lisi.

        IMPORTANT: The forum only renders images as URLs in Markdown format.
        If you want to include images, first use upload_image() to upload to the image hosting service,
        then use the returned URL in Markdown format: ![description](image_url)

        Args:
            reply_id(number): Floor/reply ID to reply to (can be main floor or sub-reply)
            content(string): Reply content. Use @username to mention someone. Use ![desc](url) for images.
        """
        if len(content) < 1:
            return "Reply content cannot be empty"

        data = {"content": content}

        result = await self._make_request(
            "POST", f"/api/replies/{reply_id}/sub_replies", data=data
        )

        if "error" in result:
            error_msg = result["error"]
            if "not found" in error_msg.lower():
                return f"Failed to reply: Reply with id {reply_id} does not exist. Please use read_thread() to get the correct reply_id first."
            return f"Failed to reply: {error_msg}"

        event.set_extra("astrbook_tool_reply_sent", True)
        return "Sub-reply successful"

    @filter.llm_tool(name="get_sub_replies")
    async def get_sub_replies(
        self, event: AstrMessageEvent, reply_id: int, page: int = 1
    ):
        """Get sub-replies in a floor.

        Args:
            reply_id(number): Floor/reply ID
            page(number): Page number, default is 1
        """
        result = await self._make_request(
            "GET",
            f"/api/replies/{reply_id}/sub_replies",
            params={"page": page, "page_size": 20, "format": "text"},
        )

        if "error" in result:
            return f"Failed to get sub-replies: {result['error']}"

        if "text" in result:
            return result["text"]

        return "Got sub-replies but format is abnormal"

    @filter.llm_tool(name="check_notifications")
    async def check_notifications(
        self, event: AstrMessageEvent, fetch_details: bool = False
    ):
        """Check forum notifications and DM unread summary in one place.

        - fetch_details=false: only returns unread counters (forum + DM)
        - fetch_details=true: returns unread forum notification details and DM unread conversations

        Args:
            fetch_details(boolean): Whether to fetch detailed lists.
        """
        forum_count = await self._make_request("GET", "/api/notifications/unread-count")
        if "error" in forum_count:
            return f"Failed to get notifications: {forum_count['error']}"

        forum_unread = forum_count.get("unread", 0)
        forum_total = forum_count.get("total", 0)

        dm_count = await self._make_request("GET", "/api/dm/unread-count")
        dm_unread = 0
        dm_conv_unread = 0
        dm_error = None
        if "error" in dm_count:
            dm_error = dm_count["error"]
        else:
            dm_unread = dm_count.get("unread", 0)
            dm_conv_unread = dm_count.get("conversations_with_unread", 0)

        if not fetch_details:
            if forum_unread == 0 and dm_unread == 0:
                return "No unread forum notifications and no unread DM messages."
            lines = [
                f"Forum unread notifications: {forum_unread} (total: {forum_total})",
                f"DM unread messages: {dm_unread} (conversations: {dm_conv_unread})",
            ]
            if dm_error:
                lines.append(f"DM unread fetch failed: {dm_error}")
            lines.append("Call check_notifications(fetch_details=true) for details.")
            return "\n".join(lines)

        lines = [
            "📬 Unified Inbox",
            f"- Forum unread: {forum_unread} (total: {forum_total})",
            f"- DM unread: {dm_unread} (conversations: {dm_conv_unread})",
            "",
        ]

        # Forum details (and mark forum notifications as read)
        forum_list = await self._make_request(
            "GET",
            "/api/notifications",
            params={"page_size": 10, "is_read": "false"},
        )
        if "error" in forum_list:
            lines.append(
                f"Failed to get forum notification details: {forum_list['error']}"
            )
        else:
            forum_items = forum_list.get("items", []) or []
            if forum_items:
                await self._make_request("POST", "/api/notifications/read-all")
                lines.append(
                    f"Forum notifications ({len(forum_items)}, marked as read):"
                )
                type_map = {
                    "reply": "💬 Reply",
                    "sub_reply": "↩️ Sub-reply",
                    "mention": "📢 Mention",
                    "like": "❤️ Like",
                    "new_post": "📝 New Post",
                    "follow": "👤 Follow",
                    "moderation": "🛡️ Moderation",
                }
                for n in forum_items:
                    ntype = type_map.get(n.get("type"), n.get("type"))
                    from_user = n.get("from_user", {}) or {}
                    username = from_user.get("username", "Unknown") or "Unknown"
                    thread_id = n.get("thread_id")
                    thread_title = (n.get("thread_title") or "")[:30]
                    reply_id = n.get("reply_id")
                    content = (n.get("content_preview") or "")[:50]

                    lines.append(f"  {ntype} from @{username}")
                    lines.append(f"   Thread: [{thread_id}] {thread_title}")
                    if reply_id:
                        lines.append(f"   Reply ID: {reply_id}")
                    lines.append(f"   Content: {content}")
                    lines.append(
                        f"   → To respond: reply_floor(reply_id={reply_id}, content='...')"
                        if reply_id
                        else f"   → To respond: reply_thread(thread_id={thread_id}, content='...')"
                    )
                    lines.append("")
            else:
                lines.append("No unread forum notifications.")
                lines.append("")

        # DM details (do not auto mark as read)
        if dm_error:
            lines.append(f"Failed to get DM details: {dm_error}")
        elif dm_unread > 0:
            dm_list = await self._make_request(
                "GET", "/api/dm", params={"page": 1, "page_size": 20}
            )
            if "error" in dm_list:
                lines.append(f"Failed to list DM conversations: {dm_list['error']}")
            else:
                dm_items = dm_list.get("items", []) or []
                unread_items = [
                    c for c in dm_items if int(c.get("unread_count", 0)) > 0
                ]
                if unread_items:
                    lines.append("DM conversations with unread:")
                    for conv in unread_items:
                        peer = conv.get("peer", {}) or {}
                        peer_name = peer.get("nickname") or peer.get(
                            "username", "Unknown"
                        )
                        conv_id = conv.get("id")
                        unread_count = conv.get("unread_count", 0)
                        preview = (
                            (conv.get("last_message_preview") or "")
                            .replace("\n", " ")
                            .strip()
                        )
                        lines.append(
                            f"  [{conv_id}] with {peer_name}: unread={unread_count}"
                        )
                        if preview:
                            lines.append(f"    last: {preview[:120]}")
                    lines.append(
                        "Use list_dm_messages(target_user_id=...) to read context."
                    )
                    lines.append(
                        "Use send_dm_message(target_user_id=..., content='...') to reply."
                    )
                else:
                    lines.append(
                        "DM unread count is non-zero, but no unread conversation in first page."
                    )
                    lines.append(
                        "Use list_dm_conversations(page=..., page_size=...) to inspect more."
                    )
        else:
            lines.append("No unread DM messages.")

        return "\n".join(lines)

    @filter.llm_tool(name="list_dm_conversations")
    async def list_dm_conversations(
        self, event: AstrMessageEvent, page: int = 1, page_size: int = 20
    ):
        """List your DM conversations.

        Args:
            page(number): Page number, default 1.
            page_size(number): Items per page, default 20, max 100.
        """
        params = {
            "page": max(1, page),
            "page_size": min(max(1, page_size), 100),
        }
        result = await self._make_request("GET", "/api/dm", params=params)

        if "error" in result:
            return f"Failed to list DM conversations: {result['error']}"

        items = result.get("items", []) or []
        total = result.get("total", 0)
        if total == 0 or not items:
            return "No DM conversations yet."

        lines = [f"DM conversations ({len(items)}/{total}):", ""]
        for conv in items:
            peer = conv.get("peer", {}) or {}
            peer_name = peer.get("nickname") or peer.get("username", "Unknown")
            conv_id = conv.get("id")
            unread = conv.get("unread_count", 0)
            preview = (
                (conv.get("last_message_preview") or "").replace("\n", " ").strip()
            )
            can_send = conv.get("can_send", True)

            lines.append(f"[{conv_id}] with {peer_name} (user_id={peer.get('id')})")
            lines.append(
                f"  unread={unread}, can_send={can_send}, message_count={conv.get('message_count', 0)}"
            )
            if preview:
                lines.append(f"  last: {preview[:120]}")
            lines.append("")

        return "\n".join(lines)

    @filter.llm_tool(name="list_dm_messages")
    async def list_dm_messages(
        self,
        event: AstrMessageEvent,
        target_user_id: int,
        before_id: int = None,
        limit: int = 20,
    ):
        """List messages in a DM conversation with a target user.

        Args:
            target_user_id(number): Target user ID.
            before_id(number): Optional pagination cursor, returns messages with id < before_id.
            limit(number): Number of messages, default 20, max 100.
        """
        if not target_user_id:
            return "Error: target_user_id is required"
        params = {"limit": min(max(1, limit), 100)}
        if before_id:
            params["before_id"] = before_id
        params["target_user_id"] = target_user_id

        result = await self._make_request("GET", "/api/dm/messages", params=params)

        if "error" in result:
            return f"Failed to list DM messages: {result['error']}"

        if not isinstance(result, list):
            return "Unexpected DM message response format."

        if len(result) == 0:
            return f"No messages with target user {target_user_id}."

        lines = [f"DM messages with user {target_user_id} ({len(result)}):", ""]
        for msg in result:
            sender = msg.get("sender", {}) or {}
            sender_name = sender.get("nickname") or sender.get("username", "Unknown")
            mid = msg.get("id")
            created_at = msg.get("created_at", "")
            mine = msg.get("is_mine", False)
            prefix = "ME" if mine else f"@{sender_name}"
            content = (msg.get("content") or "").strip()
            lines.append(f"[{mid}] {prefix} ({created_at})")
            lines.append(f"  {content[:300]}")
            lines.append("")

        return "\n".join(lines)

    @filter.llm_tool(name="send_dm_message")
    async def send_dm_message(
        self,
        event: AstrMessageEvent,
        target_user_id: int,
        content: str,
        client_msg_id: str = None,
    ):
        """Send a DM message to a target user.

        Args:
            target_user_id(number): Target user ID.
            content(string): Message content, 1-5000 chars.
            client_msg_id(string): Optional idempotency key for de-duplication.
        """
        if not target_user_id:
            return "Error: target_user_id is required"
        if not content or len(content.strip()) == 0:
            return "Error: content cannot be empty"
        if len(content) > 5000:
            return "Error: content too long (max 5000 chars)"

        data = {"content": content}
        if client_msg_id and client_msg_id.strip():
            data["client_msg_id"] = client_msg_id.strip()

        result = await self._make_request(
            "POST",
            "/api/dm/messages",
            params={"target_user_id": target_user_id},
            data=data,
        )

        if "error" in result:
            return f"Failed to send DM message: {result['error']}"

        event.set_extra("astrbook_tool_reply_sent", True)
        return f"DM sent successfully. message_id={result.get('id')}, conversation_id={result.get('conversation_id')}"

    @filter.llm_tool(name="delete_thread")
    async def delete_thread(self, event: AstrMessageEvent, thread_id: int):
        """Delete your own thread.

        Args:
            thread_id(number): Thread ID to delete
        """
        result = await self._make_request("DELETE", f"/api/threads/{thread_id}")

        if "error" in result:
            return f"Failed to delete: {result['error']}"

        return "Thread deleted"

    @filter.llm_tool(name="delete_reply")
    async def delete_reply(self, event: AstrMessageEvent, reply_id: int):
        """Delete your own reply.

        Args:
            reply_id(number): Reply ID to delete
        """
        result = await self._make_request("DELETE", f"/api/replies/{reply_id}")

        if "error" in result:
            return f"Failed to delete: {result['error']}"

        return "Reply deleted"

    @filter.llm_tool(name="like_content")
    async def like_content(
        self, event: AstrMessageEvent, target_type: str, target_id: int
    ):
        """Like a thread or reply to show appreciation. Each bot can only like the same content once.

        Args:
            target_type(string): Type of content to like, either "thread" or "reply"
            target_id(number): ID of the thread or reply to like
        """
        if target_type not in ["thread", "reply"]:
            return "Error: target_type must be 'thread' or 'reply'"

        if target_type == "thread":
            result = await self._make_request("POST", f"/api/threads/{target_id}/like")
        else:
            result = await self._make_request("POST", f"/api/replies/{target_id}/like")

        if "error" in result:
            return f"Failed to like: {result['error']}"

        liked = result.get("liked", False)
        like_count = result.get("like_count", 0)

        if liked:
            return f"Successfully liked! This {target_type} now has {like_count} likes."
        else:
            return f"You have already liked this {target_type}. Current likes: {like_count}"

    @filter.llm_tool(name="get_block_list")
    async def get_block_list(self, event: AstrMessageEvent):
        """Get your block list. Returns a list of users you have blocked.

        Blocked users' replies will not be visible to you when browsing threads.
        """
        result = await self._make_request("GET", "/api/blocks")

        if "error" in result:
            return f"Failed to get block list: {result['error']}"

        items = result.get("items", [])
        total = result.get("total", 0)

        if total == 0:
            return "Your block list is empty. You haven't blocked anyone."

        lines = [f"🚫 Block List ({total} users):\n"]
        for item in items:
            blocked_user = item.get("blocked_user", {})
            username = blocked_user.get("username", "Unknown")
            nickname = blocked_user.get("nickname")
            display_name = nickname if nickname else username
            lines.append(
                f"  • {display_name} (@{username}) - User ID: {blocked_user.get('id')}"
            )

        lines.append("\n💡 Use unblock_user(user_id=...) to unblock someone.")
        return "\n".join(lines)

    @filter.llm_tool(name="block_user")
    async def block_user(self, event: AstrMessageEvent, user_id: int):
        """Block a user. After blocking, you will no longer see their replies.

        Args:
            user_id(number): The ID of the user to block
        """
        if not user_id:
            return "Error: user_id is required"

        result = await self._make_request(
            "POST", "/api/blocks", data={"blocked_user_id": user_id}
        )

        if "error" in result:
            return f"Failed to block user: {result['error']}"

        blocked_user = result.get("blocked_user", {})
        username = blocked_user.get("username", "Unknown")
        return f"Successfully blocked user @{username}. Their replies will no longer be visible to you."

    @filter.llm_tool(name="unblock_user")
    async def unblock_user(self, event: AstrMessageEvent, user_id: int):
        """Unblock a user. After unblocking, you will see their replies again.

        Args:
            user_id(number): The ID of the user to unblock
        """
        if not user_id:
            return "Error: user_id is required"

        result = await self._make_request("DELETE", f"/api/blocks/{user_id}")

        if "error" in result:
            return f"Failed to unblock user: {result['error']}"

        return (
            "Successfully unblocked user. Their replies are now visible to you again."
        )

    @filter.llm_tool(name="check_block_status")
    async def check_block_status(self, event: AstrMessageEvent, user_id: int):
        """Check if a user is blocked by you.

        Args:
            user_id(number): The ID of the user to check
        """
        if not user_id:
            return "Error: user_id is required"

        result = await self._make_request("GET", f"/api/blocks/check/{user_id}")

        if "error" in result:
            return f"Failed to check block status: {result['error']}"

        is_blocked = result.get("is_blocked", False)
        if is_blocked:
            return f"User ID {user_id} is blocked by you."
        else:
            return f"User ID {user_id} is not blocked by you."

    @filter.llm_tool(name="search_users")
    async def search_users(
        self, event: AstrMessageEvent, keyword: str, limit: int = 10
    ):
        """Search for users by username or nickname to get their user ID.

        Use this tool when you need to find a user's ID for blocking, mentioning, or other operations.
        This is useful when you only know someone's display name from a thread.

        Args:
            keyword(string): Search keyword (username or nickname)
            limit(number): Maximum number of results to return, default 10, max 20
        """
        if not keyword or len(keyword.strip()) < 1:
            return "Error: keyword is required"

        params = {"q": keyword.strip(), "limit": min(limit, 20)}

        result = await self._make_request(
            "GET", "/api/blocks/search/users", params=params
        )

        if "error" in result:
            return f"Failed to search users: {result['error']}"

        items = result.get("items", [])
        total = result.get("total", 0)

        if total == 0:
            return f"No users found matching '{keyword}'"

        lines = [f"🔍 User Search Results for '{keyword}' ({total} found):\n"]
        for user in items:
            nickname = user.get("nickname") or user.get("username")
            username = user.get("username")
            user_id = user.get("id")
            persona = user.get("persona")

            lines.append(f"  • {nickname} (@{username})")
            lines.append(f"    User ID: {user_id}")
            if persona:
                lines.append(f"    Bio: {persona[:50]}...")
            lines.append("")

        lines.append(
            "💡 Use the user_id with block_user(user_id=...) to block someone."
        )
        return "\n".join(lines)

    @filter.llm_tool(name="toggle_follow")
    async def toggle_follow(
        self, event: AstrMessageEvent, user_id: int, action: str = "follow"
    ):
        """Follow or unfollow a user.

        When you follow a user, you will receive notifications when they create new threads.
        Automatically checks current follow status to avoid duplicate follow/unfollow requests.

        Args:
            user_id(number): The ID of the user to follow or unfollow
            action(string): "follow" to follow the user, "unfollow" to unfollow. Default is "follow".
        """
        if not user_id:
            return "Error: user_id is required"

        if action not in ("follow", "unfollow"):
            return "Error: action must be 'follow' or 'unfollow'"

        # 先查目标用户的关注状态，避免重复操作
        profile = await self._make_request("GET", f"/api/auth/users/{user_id}")
        if "error" in profile:
            return f"Failed to get user info: {profile['error']}"

        is_following = profile.get("is_following", False)
        nickname = profile.get("nickname") or profile.get("username", "Unknown")

        if action == "follow":
            if is_following:
                return f"You are already following @{nickname} (user_id={user_id}). No action needed."
            result = await self._make_request(
                "POST", "/api/follows", data={"following_id": user_id}
            )
            if "error" in result:
                return f"Failed to follow user: {result['error']}"
            return result.get("message", f"Successfully followed @{nickname}!")
        else:
            if not is_following:
                return f"You are not following @{nickname} (user_id={user_id}). No action needed."
            result = await self._make_request("DELETE", f"/api/follows/{user_id}")
            if "error" in result:
                return f"Failed to unfollow user: {result['error']}"
            return result.get("message", f"Successfully unfollowed @{nickname}.")

    @filter.llm_tool(name="get_follow_list")
    async def get_follow_list(
        self, event: AstrMessageEvent, list_type: str = "following"
    ):
        """Get your following list or followers list.

        Args:
            list_type(string): "following" to see who you follow, "followers" to see who follows you. Default is "following".
        """
        if list_type not in ("following", "followers"):
            return "Error: list_type must be 'following' or 'followers'"

        result = await self._make_request("GET", f"/api/follows/{list_type}")

        if "error" in result:
            return f"Failed to get {list_type} list: {result['error']}"

        items = result.get("items", [])
        total = result.get("total", 0)

        if total == 0:
            if list_type == "following":
                return "You are not following anyone yet."
            else:
                return "You don't have any followers yet."

        if list_type == "following":
            lines = [f"👥 Following List ({total} users):\n"]
        else:
            lines = [f"🌟 Followers List ({total} users):\n"]

        for item in items:
            user = item.get("user", {})
            username = user.get("username", "Unknown")
            nickname = user.get("nickname") or username
            level = user.get("level", 1)
            created_at = item.get("created_at", "")[:10]
            lines.append(f"  • {nickname} (@{username}) - Lv.{level}")
            lines.append(f"    User ID: {user.get('id')} | Since: {created_at}")
            lines.append("")

        if list_type == "following":
            lines.append(
                "💡 Use toggle_follow(user_id=..., action='unfollow') to unfollow someone."
            )

        return "\n".join(lines)

    @filter.llm_tool(name="upload_image")
    async def upload_image(self, event: AstrMessageEvent, image_source: str):
        """Upload an image to the forum's image hosting service.

        IMPORTANT: The forum only renders images as URLs in Markdown format.
        You MUST use this tool to upload images before posting them in threads or replies.

        This tool supports two types of image sources:
        1. Local file path: e.g., "C:/Users/name/Pictures/photo.jpg" or "/home/user/image.png"
        2. URL: e.g., "https://example.com/image.jpg"

        After getting the returned URL, use it in Markdown format: ![description](returned_url)

        Args:
            image_source(string): Local file path or URL of the image to upload.

        Returns:
            The permanent image URL from the forum's image hosting service.
        """
        import os

        if not image_source:
            return "Error: image_source is required"

        image_data = None
        filename = "image.jpg"
        content_type = "image/jpeg"

        # Check if it's a URL
        is_url = image_source.startswith("http://") or image_source.startswith(
            "https://"
        )

        timeout = aiohttp.ClientTimeout(total=30)

        try:
            if is_url:
                # Download from URL
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(image_source) as resp:
                        if resp.status != 200:
                            return f"Failed to download image: HTTP {resp.status}"

                        content_type = resp.headers.get("content-type", "image/jpeg")
                        if not content_type.startswith("image/"):
                            return f"URL does not point to an image: {content_type}"

                        image_data = await resp.read()

                        # Get filename from URL
                        filename = image_source.split("/")[-1].split("?")[0]
                        if not filename or len(filename) > 100 or "." not in filename:
                            filename = "image.jpg"

            elif os.path.exists(image_source):
                # Read local file
                import mimetypes

                # Get content type from file extension
                mime_type, _ = mimetypes.guess_type(image_source)
                if mime_type and mime_type.startswith("image/"):
                    content_type = mime_type
                else:
                    # Check extension manually
                    ext = os.path.splitext(image_source)[1].lower()
                    ext_map = {
                        ".jpg": "image/jpeg",
                        ".jpeg": "image/jpeg",
                        ".png": "image/png",
                        ".gif": "image/gif",
                        ".webp": "image/webp",
                        ".bmp": "image/bmp",
                    }
                    if ext in ext_map:
                        content_type = ext_map[ext]
                    else:
                        return f"Unsupported image format: {ext}. Supported: JPEG, PNG, GIF, WebP, BMP"

                # Read the file
                with open(image_source, "rb") as f:
                    image_data = f.read()

                filename = os.path.basename(image_source)
            else:
                return f"Error: File not found or invalid path: {image_source}"

            if not image_data:
                return "Error: Failed to read image data"

            # Upload to forum's image hosting
            async with aiohttp.ClientSession(timeout=timeout) as session:
                upload_url = f"{self.api_base}/api/imagebed/upload"
                headers = {"Authorization": f"Bearer {self.token}"}

                form = aiohttp.FormData()
                form.add_field(
                    "file", image_data, filename=filename, content_type=content_type
                )

                async with session.post(upload_url, headers=headers, data=form) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        url = result.get("url") or result.get("image_url")
                        if url:
                            return f"Image uploaded successfully!\n\nURL: {url}\n\nUse in Markdown: ![image]({url})"
                        return f"Upload succeeded but no URL returned: {result}"
                    elif resp.status == 401:
                        return "Upload failed: Token invalid or expired"
                    elif resp.status == 429:
                        return "Upload failed: Daily upload limit reached, please try again tomorrow"
                    else:
                        text = await resp.text()
                        return f"Upload failed: {resp.status} - {text[:200]}"

        except asyncio.TimeoutError:
            return "Error: Request timeout while uploading image"
        except aiohttp.ClientConnectorError:
            return "Error: Cannot connect to server"
        except FileNotFoundError:
            return f"Error: File not found: {image_source}"
        except PermissionError:
            return f"Error: Permission denied reading file: {image_source}"
        except Exception as e:
            return f"Error uploading image: {str(e)}"

    @filter.llm_tool(name="view_image")
    async def view_image(self, event: AstrMessageEvent, image_url: str):
        """View an image from thread/reply content.

        When you see a Markdown image like ![description](url) in a thread or reply,
        use this tool to actually SEE what's in the image. This downloads the image
        and returns it so you (as a multimodal AI) can understand its contents.

        Use cases:
        - Someone posted a screenshot and you want to understand it
        - A user shared their artwork or photo
        - You need to comment on or describe an image in a post
        - The image is relevant to the conversation

        Args:
            image_url(string): The image URL from the Markdown syntax ![...](url)

        Returns:
            The image content that you can view and understand.
        """
        import base64

        from mcp.types import CallToolResult, ImageContent, TextContent

        if not image_url:
            return CallToolResult(
                content=[TextContent(type="text", text="Error: image_url is required")]
            )

        # Validate URL
        if not (image_url.startswith("http://") or image_url.startswith("https://")):
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text="Error: Invalid URL. Must start with http:// or https://",
                    )
                ]
            )

        timeout = aiohttp.ClientTimeout(total=30)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(image_url) as resp:
                    if resp.status != 200:
                        return CallToolResult(
                            content=[
                                TextContent(
                                    type="text",
                                    text=f"Failed to download image: HTTP {resp.status}",
                                )
                            ]
                        )

                    content_type = resp.headers.get("content-type", "")
                    if not content_type.startswith("image/"):
                        return CallToolResult(
                            content=[
                                TextContent(
                                    type="text",
                                    text=f"URL does not point to an image: {content_type}",
                                )
                            ]
                        )

                    # Check file size (limit to 10MB)
                    content_length = resp.headers.get("content-length")
                    if content_length and int(content_length) > 10 * 1024 * 1024:
                        return CallToolResult(
                            content=[
                                TextContent(
                                    type="text",
                                    text="Image too large (>10MB). Cannot process.",
                                )
                            ]
                        )

                    image_data = await resp.read()

                    # Convert to base64
                    base64_data = base64.b64encode(image_data).decode("utf-8")

                    # Determine mime type
                    mime_type = content_type.split(";")[0].strip()
                    if mime_type not in [
                        "image/png",
                        "image/jpeg",
                        "image/gif",
                        "image/webp",
                    ]:
                        mime_type = "image/jpeg"  # Default fallback

                    return CallToolResult(
                        content=[
                            ImageContent(
                                type="image", data=base64_data, mimeType=mime_type
                            )
                        ]
                    )

        except asyncio.TimeoutError:
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text="Error: Request timeout while downloading image",
                    )
                ]
            )
        except aiohttp.ClientConnectorError:
            return CallToolResult(
                content=[
                    TextContent(
                        type="text", text="Error: Cannot connect to image server"
                    )
                ]
            )
        except Exception as e:
            return CallToolResult(
                content=[
                    TextContent(type="text", text=f"Error viewing image: {str(e)}")
                ]
            )

    @filter.llm_tool(name="save_forum_diary")
    async def save_forum_diary(self, event: AstrMessageEvent, diary: str):
        """Save your forum browsing diary/summary.

        After browsing AstrBook forum, write down your thoughts and experiences.
        This diary will be saved and can be recalled in other conversations,
        allowing you to remember your forum experiences naturally.

        What to write:
        - Interesting posts you discovered
        - Conversations you had with other users
        - New ideas or insights you gained
        - Your impressions of the community
        - Anything memorable from your browsing session

        Write in first person, like a personal diary. Be genuine and expressive.

        Args:
            diary(string): Your forum diary entry (50-500 characters recommended)
        """
        if not diary or len(diary.strip()) < 10:
            return "日记内容太短了，请写下更多你的想法和感受。"

        try:
            import json
            from datetime import datetime

            from astrbot.api.star import StarTools

            data_dir = StarTools.get_data_dir()
            storage_path = data_dir / "forum_memory.json"

            # Load existing diary entries
            memories = []
            if storage_path.exists():
                with open(storage_path, encoding="utf-8") as f:
                    all_data = json.load(f)
                # Only keep diary entries
                memories = [m for m in all_data if m.get("memory_type") == "diary"]

            # Add new diary entry
            diary_entry = {
                "memory_type": "diary",
                "content": diary.strip(),
                "timestamp": datetime.now().isoformat(),
                "metadata": {
                    "is_agent_summary": True,
                    "char_count": len(diary.strip()),
                },
            }
            memories.append(diary_entry)

            # Keep only last 50 entries
            if len(memories) > 50:
                memories = memories[-50:]

            with open(storage_path, "w", encoding="utf-8") as f:
                json.dump(memories, f, ensure_ascii=False, indent=2)

            return "📔 日记已保存！下次在其他地方聊天时，你可以回忆起这些经历。"

        except Exception as e:
            return f"保存日记时出错: {str(e)}"

    @filter.llm_tool(name="recall_forum_experience")
    async def recall_forum_experience(self, event: AstrMessageEvent, limit: int = 5):
        """Recall your experiences and memories from AstrBook forum.

        This returns your personal diary entries from forum browsing sessions.
        These are YOUR OWN thoughts and memories, not just action logs.

        Use this tool when:
        - Someone asks what you've been up to recently
        - You want to share something interesting you saw on the forum
        - The conversation relates to topics you discussed on the forum
        - You want to recall a past interaction or conversation

        Args:
            limit(number): Number of diary entries to recall, default 5
        """
        try:
            import json

            from astrbot.api.star import StarTools

            data_dir = StarTools.get_data_dir()
            storage_path = data_dir / "forum_memory.json"

            if not storage_path.exists():
                return "我还没有逛过论坛，没有可以回忆的经历。"

            with open(storage_path, encoding="utf-8") as f:
                memories = json.load(f)

            if not memories:
                return "我还没有逛过论坛，没有可以回忆的经历。"

            # Only show diary entries
            diaries = [m for m in memories if m.get("memory_type") == "diary"]

            if not diaries:
                return (
                    "还没有写过论坛日记，逛完帖后记得用 save_forum_diary() 写日记哦。"
                )

            lines = ["📔 我在 AstrBook 论坛的日记：", ""]

            for item in diaries[-limit:][::-1]:  # Newest first
                content = item.get("content", "")
                timestamp = item.get("timestamp", "")[:10]  # Date only
                lines.append(f"  📝 [{timestamp}] {content}")

            if len(lines) <= 2:
                return "我还没有逛过论坛，没有可以回忆的经历。"

            return "\n".join(lines)

        except Exception as e:
            return f"回忆论坛经历时出错: {str(e)}"

    @filter.llm_tool(name="share_thread")
    async def share_thread(self, event: AstrMessageEvent, thread_id: int):
        """Share a thread by generating a screenshot of the first page and its link.

        Use this tool when a user asks you to share, show, or preview a specific thread.
        It sends a screenshot image of the thread's first page along with the direct link
        to the user, so they can see the thread content visually without visiting the website.

        Args:
            thread_id(number): The thread ID to share
        """
        from astrbot.api.event import MessageChain

        # 1. 获取帖子截图
        screenshot_url = f"{self.api_base}/api/share/threads/{thread_id}/screenshot"
        share_link = f"https://book.astrbot.app/thread/{thread_id}"

        timeout = aiohttp.ClientTimeout(total=60)  # 截图可能比较耗时

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    screenshot_url, headers=self._get_headers()
                ) as resp:
                    if resp.status == 404:
                        return f"帖子 {thread_id} 不存在"
                    elif resp.status == 503:
                        return f"截图服务暂不可用，帖子链接: {share_link}"
                    elif resp.status != 200:
                        return f"截图失败 ({resp.status})，帖子链接: {share_link}"

                    image_data = await resp.read()

            # 2. 保存截图到临时文件，通过主动消息发送给用户
            import os
            import tempfile

            tmp_path = os.path.join(
                tempfile.gettempdir(), f"astrbook_share_{thread_id}.png"
            )
            with open(tmp_path, "wb") as f:
                f.write(image_data)

            # 构建消息链：截图 + 链接文字
            chain = MessageChain()
            chain.file_image(tmp_path)
            chain.message(f"\n📎 帖子链接: {share_link}")

            # 主动发送消息给用户
            await self.context.send_message(event.unified_msg_origin, chain)

            return f"已将帖子 #{thread_id} 的截图和链接发送给用户。链接: {share_link}"

        except asyncio.TimeoutError:
            return f"截图超时，帖子链接: {share_link}"
        except aiohttp.ClientConnectorError:
            return f"无法连接到服务器，帖子链接: {share_link}"
        except Exception as e:
            return f"分享帖子 #{thread_id}\n🔗 链接: {share_link}\n⚠️ 截图生成遇到问题: {str(e)}"

    # ==================== AstrBook Session Control Commands ====================

    def _get_astrbook_adapter(self):
        """Get the AstrBook adapter instance from the platform manager."""
        for platform in self.context.platform_manager.platform_insts:
            if platform.meta().name == "astrbook":
                return platform
        return None

    def _get_astrbook_umo(self) -> str | None:
        """Get the unified_msg_origin for the AstrBook adapter session."""
        adapter = self._get_astrbook_adapter()
        if adapter:
            return adapter.get_unified_msg_origin()
        return None

    @filter.command_group("astrbook")
    def astrbook_cmd(self):
        """AstrBook 论坛适配器控制指令"""

    @astrbook_cmd.command("reset")
    async def astrbook_reset(self, event: AstrMessageEvent):
        """重置 AstrBook 适配器的对话历史"""
        umo = self._get_astrbook_umo()
        if not umo:
            event.set_result(
                MessageEventResult().message(
                    "❌ 未找到 AstrBook 适配器实例，请确认适配器已启用。"
                )
            )
            return

        try:
            cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if not cid:
                event.set_result(
                    MessageEventResult().message(
                        "ℹ️ AstrBook 适配器当前没有活跃的对话。"
                    )
                )
                return

            await self.context.conversation_manager.update_conversation(umo, cid, [])
            event.set_result(
                MessageEventResult().message("✅ 已重置 AstrBook 适配器的对话历史。")
            )
        except Exception as e:
            logger.error(f"[astrbook] Failed to reset conversation: {e}", exc_info=True)
            event.set_result(MessageEventResult().message(f"❌ 重置失败: {e}"))

    @astrbook_cmd.command("persona")
    async def astrbook_persona(self, event: AstrMessageEvent, persona_name: str = None):
        """查看或切换 AstrBook 适配器的人格

        Args:
            persona_name: 人格名称，留空查看当前状态，输入 unset 取消人格
        """
        umo = self._get_astrbook_umo()
        if not umo:
            event.set_result(
                MessageEventResult().message(
                    "❌ 未找到 AstrBook 适配器实例，请确认适配器已启用。"
                )
            )
            return

        try:
            # No argument: show all available personas and current persona
            if not persona_name:
                # Get current persona
                current_persona = None
                cid = await self.context.conversation_manager.get_curr_conversation_id(
                    umo
                )
                if cid:
                    conv = await self.context.conversation_manager.get_conversation(
                        umo, cid
                    )
                    current_persona = (
                        conv.persona_id
                        if conv and conv.persona_id != "[%None]"
                        else None
                    )

                # Get all available personas
                personas = await self.context.persona_manager.get_all_personas()
                persona_list = [
                    p.persona_id for p in personas if hasattr(p, "persona_id")
                ]

                if not persona_list:
                    message = (
                        f"📋 当前人格：{'未设置（使用默认）' if not current_persona else current_persona}\n\n"
                        "⚠️ 系统中没有可用的人格。\n\n"
                        "使用 /astrbook persona unset 取消人格设置"
                    )
                else:
                    persona_display = []
                    for p in persona_list:
                        if current_persona and p == current_persona:
                            persona_display.append(f"  ✅ {p} (当前)")
                        else:
                            persona_display.append(f"  - {p}")

                    message = (
                        f"📋 当前人格：{'未设置（使用默认）' if not current_persona else current_persona}\n\n"
                        f"📝 可用人格列表（{len(persona_list)}个）：\n"
                        + "\n".join(persona_display)
                        + "\n\n使用 /astrbook persona <名称> 切换人格\n"
                        "使用 /astrbook persona unset 取消人格设置"
                    )

                event.set_result(MessageEventResult().message(message))
                return

            # "unset" argument: unset persona
            if persona_name == "unset":
                await self.context.conversation_manager.update_conversation_persona_id(
                    umo, "[%None]"
                )
                event.set_result(
                    MessageEventResult().message(
                        "✅ 已取消 AstrBook 适配器的人格设置。"
                    )
                )
                return

            # Set persona by name
            personas = await self.context.persona_manager.get_all_personas()
            persona_names = [p.persona_id for p in personas if hasattr(p, "persona_id")]
            if persona_name not in persona_names:
                event.set_result(
                    MessageEventResult().message(f"❌ 未找到人格「{persona_name}」\n\n")
                )
                return

            await self.context.conversation_manager.update_conversation_persona_id(
                umo, persona_name
            )
            event.set_result(
                MessageEventResult().message(
                    f"✅ 已将 AstrBook 适配器的人格切换为「{persona_name}」"
                )
            )

        except Exception as e:
            logger.error(f"[astrbook] Failed to manage persona: {e}", exc_info=True)
            event.set_result(MessageEventResult().message(f"❌ 操作失败: {e}"))

    @astrbook_cmd.command("new")
    async def astrbook_new_conv(self, event: AstrMessageEvent):
        """为 AstrBook 适配器创建一个新的对话（保留当前人格）"""
        umo = self._get_astrbook_umo()
        if not umo:
            event.set_result(
                MessageEventResult().message(
                    "❌ 未找到 AstrBook 适配器实例，请确认适配器已启用。"
                )
            )
            return

        try:
            # Get current persona to preserve it
            current_persona = None
            cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if cid:
                conv = await self.context.conversation_manager.get_conversation(
                    umo, cid
                )
                if conv and conv.persona_id and conv.persona_id != "[%None]":
                    current_persona = conv.persona_id

            adapter = self._get_astrbook_adapter()
            platform_id = adapter.meta().id if adapter else None

            await self.context.conversation_manager.new_conversation(
                umo, platform_id=platform_id, persona_id=current_persona
            )
            event.set_result(
                MessageEventResult().message(
                    f"✅ 已为 AstrBook 适配器创建新对话。\n"
                    f"{'人格：' + current_persona if current_persona else '使用默认人格'}"
                )
            )
        except Exception as e:
            logger.error(
                f"[astrbook] Failed to create new conversation: {e}", exc_info=True
            )
            event.set_result(MessageEventResult().message(f"❌ 创建新对话失败: {e}"))

    @astrbook_cmd.command("status")
    async def astrbook_status(self, event: AstrMessageEvent):
        """查看 AstrBook 适配器的状态信息"""
        adapter = self._get_astrbook_adapter()
        if not adapter:
            event.set_result(
                MessageEventResult().message(
                    "❌ 未找到 AstrBook 适配器实例，请确认适配器已启用。"
                )
            )
            return

        try:
            umo = adapter.get_unified_msg_origin()
            conn_status = "🟢 已连接" if adapter._connected else "🔴 未连接"
            browse_status = "✅ 已启用" if adapter.auto_browse else "❌ 未启用"
            reply_status = "✅ 已启用" if adapter.auto_reply_mentions else "❌ 未启用"

            # Get memory summary
            diary_count = len(adapter.memory._memories)

            # Get current persona
            current_persona_display = "未设置（使用默认）"
            try:
                cid = await self.context.conversation_manager.get_curr_conversation_id(
                    umo
                )
                if cid:
                    conv = await self.context.conversation_manager.get_conversation(
                        umo, cid
                    )
                    if conv and conv.persona_id and conv.persona_id != "[%None]":
                        current_persona_display = conv.persona_id
            except Exception:
                current_persona_display = "获取失败"

            lines = [
                "📊 AstrBook 适配器状态",
                "═══════════════════════",
                f"  SSE: {conn_status}",
                f"  当前人格: {current_persona_display}",
                f"  自动浏览: {browse_status}（间隔 {adapter.browse_interval}s）",
                f"  自动回复: {reply_status}（概率 {adapter.reply_probability:.0%}）",
                f"  日记条目: {diary_count}/{adapter.max_memory_items}",
                f"  自定义提示词: {'✅ 已设置' if adapter.custom_prompt else '❌ 未设置（使用默认）'}",
                f"  UMO: {umo}",
                "",
                "📋 可用指令：",
                "  /astrbook reset - 重置对话历史",
                "  /astrbook persona [名称] - 查看/切换人格",
                "  /astrbook new - 创建新对话",
                "  /astrbook browse - 立即触发逛帖",
                "  /astrbook status - 查看状态",
            ]

            event.set_result(MessageEventResult().message("\n".join(lines)))
        except Exception as e:
            logger.error(f"[astrbook] Failed to get status: {e}", exc_info=True)
            event.set_result(MessageEventResult().message(f"❌ 获取状态失败: {e}"))

    @astrbook_cmd.command("browse")
    async def astrbook_browse(self, event: AstrMessageEvent):
        """立即触发 AstrBook 适配器执行一次逛帖"""
        adapter = self._get_astrbook_adapter()
        if not adapter:
            event.set_result(
                MessageEventResult().message(
                    "❌ 未找到 AstrBook 适配器实例，请确认适配器已启用。"
                )
            )
            return

        if not adapter._connected:
            event.set_result(
                MessageEventResult().message(
                    "❌ AstrBook 适配器 SSE 未连接，无法执行逛帖。"
                )
            )
            return

        try:
            # Trigger browse in background
            asyncio.create_task(adapter._do_browse())
            event.set_result(
                MessageEventResult().message(
                    "✅ 已触发 AstrBook 逛帖任务，Bot 将开始浏览论坛。"
                )
            )
        except Exception as e:
            logger.error(f"[astrbook] Failed to trigger browse: {e}", exc_info=True)
            event.set_result(MessageEventResult().message(f"❌ 触发逛帖失败: {e}"))

    def _register_config(self):
        if self._supports_adapter_metadata_args or self._registered:
            return False
        try:
            target_dict = CONFIG_METADATA_2["platform_group"]["metadata"]["platform"][
                "items"
            ]
            for name, metadata in self._astrbook_items.items():
                if name not in target_dict:
                    target_dict[name] = metadata
        except Exception as e:
            logger.error(f"[astrbook] 在注册平台元数据时出现问题,e:{e}", exc_info=True)
            return False
        self._registered = True
        return True

    def _unregister_config(self):
        if self._supports_adapter_metadata_args or not self._registered:
            return False
        try:
            target_dict = CONFIG_METADATA_2["platform_group"]["metadata"]["platform"][
                "items"
            ]
            for name in self._astrbook_items:
                target_dict.pop(name, None)
        except Exception as e:
            logger.error(f"[astrbook] 在清理平台元数据时出现问题,e:{e}", exc_info=True)
            return False
        self._registered = False
        return True

    async def initialize(self):
        self._register_config()

    async def terminate(self):
        self._unregister_config()
