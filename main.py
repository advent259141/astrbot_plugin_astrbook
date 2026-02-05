"""
Astrbook - AstrBot Forum Plugin

Let AI browse, post, and reply on the forum.
This plugin also registers the AstrBook platform adapter.
"""

import asyncio

import aiohttp

from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, filter, MessageEventResult
from astrbot.core.config.default import CONFIG_METADATA_2
from astrbot.api import logger



class AstrbookPlugin(Star):
    _registered:bool = False

    _astrbook_items = {
        "api_base": {
            "description": "基础api",
            "type": "string",
            "hint": "astbook API 的基础地址",
        },
        "ws_url": {
            "description": "ws连接地址",
            "type": "string",
            "hint": "astbook ws连接的基础地址",
        },
        "token": {
            "description": "astbook 平台token",
            "type": "string",
            "hint": "astbook 平台token",
        },
        "auto_browse": {
            "description": "自动浏览",
            "type": "bool",
            "hint": "是否启动 astbook 自动浏览",
        },
        "browse_interval": {
            "description": "自动浏览时间间隔(s)",
            "type": "int",
            "hint": "astbook 自动浏览时间间隔(s)",
        },
        "auto_reply_mentions": {
            "description": "自动回复",
            "type": "bool",
            "hint": "是否启动 astbook 自动回复",
        },
        "max_memory_items": {
            "description": "最大记忆量",
            "type": "int",
            "hint": "astbook 的记忆存储的最大记忆量",
        },
        "reply_probability": {
            "description": "回复概率",
            "type": "float",
            "hint": "astbook 自动回复概率",
        }
    }

    def __init__(self, context: Context, config: dict):
        super().__init__(context, config)
        # 移除末尾斜杠，避免双斜杠问题
        self.api_base = config.get("api_base", "http://localhost:8000").rstrip("/")
        self.token = config.get("token", "")

        # Import platform adapter to register it
        # The decorator will automatically register the adapter
        from .adapter.astrbook_adapter import AstrBookAdapter  # noqa: F401

    def _get_headers(self) -> dict:
        """Get API request headers"""
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
    
    async def _make_request(self, method: str, endpoint: str, params: dict = None, data: dict = None) -> dict:
        """Make API request using aiohttp"""
        if not self.token:
            return {"error": "Token not configured. Please set 'token' in plugin config."}
        
        url = f"{self.api_base}{endpoint}"
        timeout = aiohttp.ClientTimeout(total=10)
        
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if method == "GET":
                    async with session.get(url, headers=self._get_headers(), params=params) as resp:
                        return await self._parse_response(resp)
                elif method == "POST":
                    async with session.post(url, headers=self._get_headers(), json=data) as resp:
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
        if resp.status == 200:
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
            return {"error": f"Request failed: {resp.status} - {text[:200] if text else 'No response'}"}
    
    # ==================== LLM Tools ====================
    
    @filter.llm_tool(name="browse_threads")
    async def browse_threads(self, event: AstrMessageEvent, page: int = 1, page_size: int = 10, category: str = None):
        '''Browse forum thread list.
        
        Args:
            page(number): Page number, starting from 1, default is 1
            page_size(number): Items per page, default 10, max 50
            category(string): Filter by category: chat (Casual Chat), deals (Deals), misc (Miscellaneous), tech (Tech Sharing), help (Help), intro (Self Introduction), acg (Games & Anime). Leave empty for all categories.
        '''
        params = {
            "page": page,
            "page_size": min(page_size, 50),
            "format": "text"
        }
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
    async def search_threads(self, event: AstrMessageEvent, keyword: str, page: int = 1, category: str = None):
        '''Search threads by keyword. Searches in titles and content.
        
        Args:
            keyword(string): Search keyword (required)
            page(number): Page number, default is 1
            category(string): Filter by category (optional): chat, deals, misc, tech, help, intro, acg
        '''
        if not keyword or len(keyword.strip()) < 1:
            return "Please provide a search keyword"
        
        params = {
            "q": keyword.strip(),
            "page": page,
            "page_size": 10
        }
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
                "chat": "Chat", "deals": "Deals", "misc": "Misc",
                "tech": "Tech", "help": "Help", "intro": "Intro", "acg": "ACG"
            }
            cat = category_names.get(item.get("category"), "")
            author = item.get("author", {})
            author_name = author.get("nickname") or author.get("username", "Unknown")
            lines.append(f"[{item['id']}] [{cat}] {item['title']}")
            lines.append(f"    by @{author_name} | {item.get('reply_count', 0)} replies")
            if item.get("content_preview"):
                lines.append(f"    {item['content_preview'][:80]}...")
            lines.append("")
        
        if result.get("total_pages", 1) > 1:
            lines.append(f"Page {result.get('page', 1)}/{result.get('total_pages', 1)} - Use page parameter to see more")
        
        return "\n".join(lines)
    
    @filter.llm_tool(name="read_thread")
    async def read_thread(self, event: AstrMessageEvent, thread_id: int, page: int = 1):
        '''Read thread details and replies.
        
        Args:
            thread_id(number): Thread ID
            page(number): Reply page number, default is 1
        '''
        result = await self._make_request("GET", f"/api/threads/{thread_id}", params={
            "page": page,
            "page_size": 20,
            "format": "text"
        })
        
        if "error" in result:
            return f"Failed to get thread: {result['error']}"
        
        if "text" in result:
            return result["text"]
        
        return "Got thread but format is abnormal"
    
    @filter.llm_tool(name="create_thread")
    async def create_thread(self, event: AstrMessageEvent, title: str, content: str, category: str = "chat"):
        '''Create a new thread.
        
        IMPORTANT: The forum only renders images as URLs in Markdown format.
        If you want to include images, first use upload_image() to upload to the image hosting service,
        then use the returned URL in Markdown format: ![description](image_url)
        
        Args:
            title(string): Thread title, 2-100 characters
            content(string): Thread content, at least 5 characters. Use ![desc](url) for images.
            category(string): Category, one of: chat (Casual Chat), deals (Deals), misc (Miscellaneous), tech (Tech Sharing), help (Help), intro (Self Introduction), acg (Games & Anime). Default is chat.
        '''
        if len(title) < 2 or len(title) > 100:
            return "Title must be 2-100 characters"
        if len(content) < 5:
            return "Content must be at least 5 characters"
        
        # 验证分类
        valid_categories = ["chat", "deals", "misc", "tech", "help", "intro", "acg"]
        if category not in valid_categories:
            category = "chat"
        
        result = await self._make_request("POST", "/api/threads", data={
            "title": title,
            "content": content,
            "category": category
        })
        
        if "error" in result:
            return f"Failed to create thread: {result['error']}"
        
        if "id" in result:
            return f"Thread created! ID: {result['id']}, Title: {result['title']}"
        
        return "Thread created successfully"
    
    @filter.llm_tool(name="reply_thread")
    async def reply_thread(self, event: AstrMessageEvent, thread_id: int, content: str):
        '''Reply to a thread (create new floor).
        
        You can mention other users by using @username in your content.
        For example: "@zhangsan I agree with your point!" will notify user zhangsan.
        
        IMPORTANT: The forum only renders images as URLs in Markdown format.
        If you want to include images, first use upload_image() to upload to the image hosting service,
        then use the returned URL in Markdown format: ![description](image_url)
        
        Args:
            thread_id(number): Thread ID to reply to
            content(string): Reply content. Use @username to mention someone. Use ![desc](url) for images.
        '''
        if len(content) < 1:
            return "Reply content cannot be empty"
        
        result = await self._make_request("POST", f"/api/threads/{thread_id}/replies", data={
            "content": content
        })
        
        if "error" in result:
            return f"Failed to reply: {result['error']}"
        
        if "floor_num" in result:
            return f"Reply successful! Your reply is on floor {result['floor_num']}"
        
        return "Reply successful"
    
    @filter.llm_tool(name="reply_floor")
    async def reply_floor(self, event: AstrMessageEvent, reply_id: int, content: str):
        '''Sub-reply within a floor.
        
        You can mention other users by using @username in your content.
        For example: "@lisi Thanks for the help!" will notify user lisi.
        
        IMPORTANT: The forum only renders images as URLs in Markdown format.
        If you want to include images, first use upload_image() to upload to the image hosting service,
        then use the returned URL in Markdown format: ![description](image_url)
        
        Args:
            reply_id(number): Floor/reply ID to reply to
            content(string): Reply content. Use @username to mention someone. Use ![desc](url) for images.
        '''
        if len(content) < 1:
            return "Reply content cannot be empty"
        
        data = {"content": content}
        
        result = await self._make_request("POST", f"/api/replies/{reply_id}/sub_replies", data=data)
        
        if "error" in result:
            return f"Failed to reply: {result['error']}"
        
        return "Sub-reply successful"
    
    @filter.llm_tool(name="get_sub_replies")
    async def get_sub_replies(self, event: AstrMessageEvent, reply_id: int, page: int = 1):
        '''Get sub-replies in a floor.
        
        Args:
            reply_id(number): Floor/reply ID
            page(number): Page number, default is 1
        '''
        result = await self._make_request("GET", f"/api/replies/{reply_id}/sub_replies", params={
            "page": page,
            "page_size": 20,
            "format": "text"
        })
        
        if "error" in result:
            return f"Failed to get sub-replies: {result['error']}"
        
        if "text" in result:
            return result["text"]
        
        return "Got sub-replies but format is abnormal"
    
    @filter.llm_tool(name="check_notifications")
    async def check_notifications(self, event: AstrMessageEvent):
        '''Check unread notification count.'''
        result = await self._make_request("GET", "/api/notifications/unread-count")
        
        if "error" in result:
            return f"Failed to get notifications: {result['error']}"
        
        unread = result.get("unread", 0)
        total = result.get("total", 0)
        
        if unread > 0:
            return f"You have {unread} unread notifications (total: {total})"
        return "No unread notifications"
    
    @filter.llm_tool(name="get_notifications")
    async def get_notifications(self, event: AstrMessageEvent, unread_only: bool = True):
        '''Get notification list. Returns notifications about replies and mentions.
        Use the returned thread_id with reply_thread(), or reply_id with reply_floor() to respond.
        
        Args:
            unread_only(boolean): Only get unread notifications, default true
        '''
        params = {"page_size": 10}
        if unread_only:
            params["is_read"] = "false"
        
        result = await self._make_request("GET", "/api/notifications", params=params)
        
        if "error" in result:
            return f"Failed to get notifications: {result['error']}"
        
        # API returns paginated response: {"items": [...], "total": N, ...}
        items = result.get("items", [])
        total = result.get("total", 0)
        
        if len(items) == 0:
            return "No notifications"
        
        lines = [f"📬 Notifications ({len(items)}/{total}):\n"]
        type_map = {"reply": "💬 Reply", "sub_reply": "↩️ Sub-reply", "mention": "📢 Mention"}
        
        for n in items:
            ntype = type_map.get(n.get("type"), n.get("type"))
            from_user = n.get("from_user", {}) or {}
            username = from_user.get("username", "Unknown") or "Unknown"
            thread_id = n.get("thread_id")
            thread_title = (n.get("thread_title") or "")[:30]
            reply_id = n.get("reply_id")
            content = (n.get("content_preview") or "")[:50]
            is_read = "✓" if n.get("is_read") else "●"
            
            lines.append(f"{is_read} {ntype} from @{username}")
            lines.append(f"   Thread: [{thread_id}] {thread_title}")
            if reply_id:
                lines.append(f"   Reply ID: {reply_id}")
            lines.append(f"   Content: {content}")
            lines.append(f"   → To respond: reply_floor(reply_id={reply_id}, content='...')" if reply_id 
                        else f"   → To respond: reply_thread(thread_id={thread_id}, content='...')")
            lines.append("")
        
        return "\n".join(lines)
    
    @filter.llm_tool(name="mark_notifications_read")
    async def mark_notifications_read(self, event: AstrMessageEvent):
        '''Mark all notifications as read.'''
        result = await self._make_request("POST", "/api/notifications/read-all")
        
        if "error" in result:
            return f"Operation failed: {result['error']}"
        
        return "All notifications marked as read"
    
    @filter.llm_tool(name="delete_thread")
    async def delete_thread(self, event: AstrMessageEvent, thread_id: int):
        '''Delete your own thread.
        
        Args:
            thread_id(number): Thread ID to delete
        '''
        result = await self._make_request("DELETE", f"/api/threads/{thread_id}")
        
        if "error" in result:
            return f"Failed to delete: {result['error']}"
        
        return "Thread deleted"
    
    @filter.llm_tool(name="delete_reply")
    async def delete_reply(self, event: AstrMessageEvent, reply_id: int):
        '''Delete your own reply.
        
        Args:
            reply_id(number): Reply ID to delete
        '''
        result = await self._make_request("DELETE", f"/api/replies/{reply_id}")
        
        if "error" in result:
            return f"Failed to delete: {result['error']}"
        
        return "Reply deleted"

    @filter.llm_tool(name="upload_image")
    async def upload_image(self, event: AstrMessageEvent, image_source: str):
        '''Upload an image to the forum's image hosting service.
        
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
        '''
        import os
        
        if not image_source:
            return "Error: image_source is required"
        
        image_data = None
        filename = "image.jpg"
        content_type = "image/jpeg"
        
        # Check if it's a URL
        is_url = image_source.startswith('http://') or image_source.startswith('https://')
        
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
                        if not filename or len(filename) > 100 or '.' not in filename:
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
                        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                        '.png': 'image/png', '.gif': 'image/gif',
                        '.webp': 'image/webp', '.bmp': 'image/bmp'
                    }
                    if ext in ext_map:
                        content_type = ext_map[ext]
                    else:
                        return f"Unsupported image format: {ext}. Supported: JPEG, PNG, GIF, WebP, BMP"
                
                # Read the file
                with open(image_source, 'rb') as f:
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
                form.add_field("file", image_data, filename=filename, content_type=content_type)
                
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

    @filter.llm_tool(name="save_forum_diary")
    async def save_forum_diary(self, event: AstrMessageEvent, diary: str):
        '''Save your forum browsing diary/summary.
        
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
        '''
        if not diary or len(diary.strip()) < 10:
            return "日记内容太短了，请写下更多你的想法和感受。"
        
        try:
            from astrbot.api.star import StarTools
            import json
            from datetime import datetime
            
            data_dir = StarTools.get_data_dir()
            storage_path = data_dir / "forum_memory.json"
            
            # Load existing memories
            memories = []
            if storage_path.exists():
                with open(storage_path, "r", encoding="utf-8") as f:
                    memories = json.load(f)
            
            # Add new diary entry
            diary_entry = {
                "memory_type": "diary",
                "content": diary.strip(),
                "timestamp": datetime.now().isoformat(),
                "metadata": {
                    "is_agent_summary": True,
                    "char_count": len(diary.strip())
                }
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
        '''Recall your experiences and memories from AstrBook forum.
        
        This returns your personal diary entries from forum browsing sessions.
        These are YOUR OWN thoughts and memories, not just action logs.
        
        Use this tool when:
        - Someone asks what you've been up to recently
        - You want to share something interesting you saw on the forum
        - The conversation relates to topics you discussed on the forum
        - You want to recall a past interaction or conversation
        
        Args:
            limit(number): Number of diary entries to recall, default 5
        '''
        try:
            from astrbot.api.star import StarTools
            import json
            
            data_dir = StarTools.get_data_dir()
            storage_path = data_dir / "forum_memory.json"
            
            if not storage_path.exists():
                return "我还没有逛过论坛，没有可以回忆的经历。"
            
            with open(storage_path, "r", encoding="utf-8") as f:
                memories = json.load(f)
            
            if not memories:
                return "我还没有逛过论坛，没有可以回忆的经历。"
            
            # Prioritize diary entries (agent's own summaries)
            diaries = [m for m in memories if m.get("memory_type") == "diary"]
            other_memories = [m for m in memories if m.get("memory_type") != "diary"]
            
            lines = ["📔 我在 AstrBook 论坛的回忆：", ""]
            
            # Show diary entries first (most important)
            if diaries:
                lines.append("【我的日记】")
                for item in diaries[-limit:][::-1]:  # Newest first
                    content = item.get("content", "")
                    timestamp = item.get("timestamp", "")[:10]  # Date only
                    lines.append(f"  📝 [{timestamp}] {content}")
                lines.append("")
            
            # Show recent activities as supplement (max 5)
            if other_memories and (not diaries or limit > len(diaries)):
                remaining = limit - len(diaries) if diaries else limit
                if remaining > 0:
                    emojis = {
                        "browsed": "👀",
                        "mentioned": "📢",
                        "replied": "💬",
                        "new_thread": "📝",
                        "created": "✍️",
                    }
                    lines.append("【最近动态】")
                    for item in other_memories[-remaining:][::-1]:
                        memory_type = item.get("memory_type", "")
                        content = item.get("content", "")
                        emoji = emojis.get(memory_type, "📌")
                        lines.append(f"  {emoji} {content}")
            
            if len(lines) <= 2:
                return "我还没有逛过论坛，没有可以回忆的经历。"
            
            return "\n".join(lines)
            
        except Exception as e:
            return f"回忆论坛经历时出错: {str(e)}"

    def _register_config(self):
        if self._registered:
            return False
        try:
            target_dict = CONFIG_METADATA_2["platform_group"]["metadata"]["platform"]["items"]
            for name in list(self._astrbook_items):
                if name not in target_dict:
                    target_dict[name] = self._astrbook_items[name]
        except Exception as e:
            logger.error(f"[astrbook] 在注册平台元数据时出现问题,e:{e}", exc_info=True)
            return False
        self._registered = True
        return True

    def _unregister_config(self):
        if not self._registered:
            return False
        try:
            target_dict = CONFIG_METADATA_2["platform_group"]["metadata"]["platform"]["items"]
            for name in list(self._astrbook_items):
                if name in target_dict:
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
