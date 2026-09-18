# processor/query_processor/nodes/node_web_search_mcp.py
import asyncio
import json

from agents.mcp import MCPServerStreamableHttp

from config.zhipu_mcp_config import mcp_config
from processor.query_processor.base import NodeBase
from processor.query_processor.state import QueryGraphState
from tool.logger import logger
from utils.json_format_utils import format_json

class NodeWebSearchMcp(NodeBase):
    """
    节点功能，调用外部搜索引擎补充信息
    """

    # 覆盖基类的 name 属性，标识节点名称
    name: str = "node_web_search_mcp"

    def process(self, state: QueryGraphState) -> QueryGraphState:

        query = state.get("rewritten_query", "")
        docs = []
        # 如果没有查询内容，直接返回
        if query:
            result = asyncio.run(self._mcp_call(query))

            # 统一输出结构化结果，供后续 rerank/引用使用
            # 每条：{title, url, snippet}
            for item in self._parse_results(result):
                # 智谱 webSearchPro 的字段是 link/content，兼容百炼的 url/snippet
                snippet = (item.get("content") or item.get("snippet") or "").strip()
                url = (item.get("link") or item.get("url") or "").strip()
                title = (item.get("title") or "").strip()
                if not snippet:
                    continue
                docs.append({"title": title, "url": url, "snippet": snippet})

            logger.info(f"MCP 搜索结果: {len(docs)} 条")

        if docs:
            return {"web_search_docs": docs}
        return {}

    @staticmethod
    def _parse_results(result) -> list:
        """
        解析 MCP 工具返回。

        智谱 webSearchPro 的 content[0].text 是双层 JSON 编码：
        字符串 -> JSON 数组文本 -> list[dict]，需要连续解码；
        顶层同时兼容百炼的 {"pages": [...]} 形态。
        """
        if not result or not getattr(result, "content", None):
            return []

        text = result.content[0].text

        # 业务侧错误（如欠费、参数非法）不会抛异常，而是 is_error=True
        if getattr(result, "is_error", False):
            logger.warning(f"MCP 返回业务错误: {str(text)[:200]}")
            return []

        try:
            data = json.loads(text)
            while isinstance(data, str):
                data = json.loads(data)
        except (TypeError, ValueError) as e:
            logger.warning(f"MCP 搜索结果解析失败: {e}")
            return []

        if isinstance(data, dict):
            return data.get("pages") or data.get("results") or []
        return data if isinstance(data, list) else []

    async def _mcp_call(self, query):

        search_mcp = MCPServerStreamableHttp(
            name="search_mcp",
            params={
                "url": mcp_config.mcp_base_url,
                "headers": {"Authorization": f"Bearer {mcp_config.api_key}"},
                "timeout": 10,
            },
            cache_tools_list=True,
            max_retry_attempts=1,
        )

        try:
            await search_mcp.connect()
            result = await search_mcp.call_tool(
                tool_name="webSearchPro",
                arguments={"search_query": query, "count": 3},
            )
            return result
        except Exception as e:
            logger.warning(f"MCP 网络搜索失败，跳过: {type(e).__name__}: {e}")
            return None
        finally:
            try:
                await search_mcp.cleanup()
            except Exception:
                pass

if __name__ == "__main__":

    init_state = {
        "rewritten_query": "关于brother HAK180烫金机，如何调节转印温度？"
    }

    # 执行节点的业务调用
    node_web_search_mcp = NodeWebSearchMcp()
    result = node_web_search_mcp(init_state)
    logger.info(format_json(result, indent=4))