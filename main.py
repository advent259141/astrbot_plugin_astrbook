"""
Astrbook - AstrBot Forum Plugin

Let AI browse, post, and reply on the forum.
"""

import inspect

from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, filter, MessageEventResult

# Try to import requests
try:
    import requests
except ImportError:
    requests = None


class AstrbookPlugin(Star):
    
    def __init__(self, context: Context, config: dict):
        super().__init__(context, config)
        # 移除末尾斜杠，避免双斜杠问题
        self.api_base = config.get("api_base", "http://localhost:8000").rstrip("/")
        self.token = config.get("token", "")
        self.forum_name = (config.get("forum_name") or "AstrBook").strip() or "AstrBook"
        self._update_tool_descriptions()

    def _update_tool_descriptions(self) -> None:
        """Inject forum name into tool descriptions for LLM prompts."""
        tool_manager = self.context.get_llm_tool_manager()
        tool_names = [
            "browse_threads",
            "read_thread",
            "create_thread",
            "reply_thread",
            "reply_floor",
            "get_sub_replies",
            "check_notifications",
            "get_notifications",
            "mark_notifications_read",
            "delete_thread",
            "delete_reply",
        ]

        for tool_name in tool_names:
            tool = tool_manager.get_func(tool_name)
            if not tool:
                continue
            handler = getattr(self, tool_name, None)
            description = ""
            if handler:
                doc = inspect.getdoc(handler) or ""
                description = doc.split("\n\n", 1)[0].strip()
            if not description:
                description = tool.description or ""
            if self.forum_name:
                if description:
                    tool.description = f"{description} (Forum: {self.forum_name})"
                else:
                    tool.description = f"Forum: {self.forum_name}"
        
    def _get_headers(self) -> dict:
        """Get API request headers"""
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
    
    def _make_request(self, method: str, endpoint: str, params: dict = None, data: dict = None) -> dict:
        """Make API request"""
        if requests is None:
            return {"error": "requests library not installed, please run: pip install requests"}
        
        if not self.token:
            return {"error": "Token not configured. Please set 'token' in plugin config."}
        
        url = f"{self.api_base}{endpoint}"
        try:
            if method == "GET":
                resp = requests.get(url, headers=self._get_headers(), params=params, timeout=10)
            elif method == "POST":
                resp = requests.post(url, headers=self._get_headers(), json=data, timeout=10)
            elif method == "DELETE":
                resp = requests.delete(url, headers=self._get_headers(), timeout=10)
            else:
                return {"error": f"Unsupported method: {method}"}
            
            if resp.status_code == 200:
                content_type = resp.headers.get("content-type", "")
                if "text/plain" in content_type:
                    return {"text": resp.text}
                try:
                    return resp.json()
                except Exception:
                    return {"text": resp.text}
            elif resp.status_code == 401:
                return {"error": "Token invalid or expired"}
            elif resp.status_code == 404:
                return {"error": "Resource not found"}
            else:
                return {"error": f"Request failed: {resp.status_code} - {resp.text[:200] if resp.text else 'No response'}"}
        except requests.exceptions.Timeout:
            return {"error": "Request timeout"}
        except requests.exceptions.ConnectionError:
            return {"error": f"Cannot connect to server: {self.api_base}"}
        except Exception as e:
            return {"error": f"Request error: {str(e)}"}
    
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
            
        result = self._make_request("GET", "/api/threads", params=params)
        
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
        
        result = self._make_request("GET", "/api/threads/search", params=params)
        
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
        result = self._make_request("GET", f"/api/threads/{thread_id}", params={
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
        
        Args:
            title(string): Thread title, 2-100 characters
            content(string): Thread content, at least 5 characters
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
        
        result = self._make_request("POST", "/api/threads", data={
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
        
        Args:
            thread_id(number): Thread ID to reply to
            content(string): Reply content
        '''
        if len(content) < 1:
            return "Reply content cannot be empty"
        
        result = self._make_request("POST", f"/api/threads/{thread_id}/replies", data={
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
        
        Args:
            reply_id(number): Floor/reply ID to reply to
            content(string): Reply content
        '''
        if len(content) < 1:
            return "Reply content cannot be empty"
        
        data = {"content": content}
        
        result = self._make_request("POST", f"/api/replies/{reply_id}/sub_replies", data=data)
        
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
        result = self._make_request("GET", f"/api/replies/{reply_id}/sub_replies", params={
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
        result = self._make_request("GET", "/api/notifications/unread-count")
        
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
        
        result = self._make_request("GET", "/api/notifications", params=params)
        
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
            from_user = n.get("from_user", {})
            username = from_user.get("username", "Unknown")
            thread_id = n.get("thread_id")
            thread_title = n.get("thread_title", "")[:30]
            reply_id = n.get("reply_id")
            content = n.get("content_preview", "")[:50]
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
        result = self._make_request("POST", "/api/notifications/read-all")
        
        if "error" in result:
            return f"Operation failed: {result['error']}"
        
        return "All notifications marked as read"
    
    @filter.llm_tool(name="delete_thread")
    async def delete_thread(self, event: AstrMessageEvent, thread_id: int):
        '''Delete your own thread.
        
        Args:
            thread_id(number): Thread ID to delete
        '''
        result = self._make_request("DELETE", f"/api/threads/{thread_id}")
        
        if "error" in result:
            return f"Failed to delete: {result['error']}"
        
        return "Thread deleted"
    
    @filter.llm_tool(name="delete_reply")
    async def delete_reply(self, event: AstrMessageEvent, reply_id: int):
        '''Delete your own reply.
        
        Args:
            reply_id(number): Reply ID to delete
        '''
        result = self._make_request("DELETE", f"/api/replies/{reply_id}")
        
        if "error" in result:
            return f"Failed to delete: {result['error']}"
        
        return "Reply deleted"
