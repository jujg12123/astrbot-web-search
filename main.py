"""多引擎搜索插件 for AstrBot

功能：
  /websearch [关键词] — 手动搜索（结构化输出）
  /websearch changelog — 查看更新日志
  web_search — LLM 函数工具，AI 可在对话中主动调用联网搜索

引擎：Bing（默认）/ 搜狗 / Google
"""

import asyncio
import re
import urllib.request
import urllib.error
import urllib.parse
import html as html_module
from dataclasses import dataclass, field

from astrbot.api import AstrBotConfig, logger, FunctionTool
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

# ═════════════════════════ 更新日志 ═════════════════════════
CHANGELOG = """
📋 **多引擎搜索插件 更新日志**

**v3.1.3** (2026-06-08)
- 🔧 修复搜索结果被 skills 层二次加工导致信息失真的问题
  - 移除 LLM 工具中的"帮用户总结"指令，搜索结果原样返回给 AI
  - 用户命令输出保留结构化格式但去除非核心的"建议"段落
- 📊 默认结果数从 5 提升到 8，信息量更大
- 🌐 默认搜索引擎仍为 Bing

**v3.1.2** (2026-06-08)
- 🔧 修复插件配置不生效的问题（改为接收 AstrBotConfig）
- 🌐 默认搜索引擎改为 Bing（国内可直连，结果全面）
- 📝 新增 /websearch changelog 命令查看更新日志

**v3.1.0** (2026-06-08)
- 🤖 LLM 函数工具改用 add_llm_tools() 显式注册
- 📊 搜索结果双格式：LLM 收到纯文本，用户看到富格式
- 🧹 新增 terminate() 清理机制

**v3.0.x** (2026-06-07)
- 🌐 多引擎支持：搜狗 / Google / Bing
- ⚙️ 插件配置界面
""".strip()

# ═════════════════════════ 搜索引擎 ═════════════════════════
SEARCH_ENGINES = {
    "bing": {
        "name": "Bing",
        "url": "https://cn.bing.com/search?q={query}&count={num}",
        "headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    },
    "sogou": {
        "name": "搜狗",
        "url": "https://www.sogou.com/web?query={query}",
        "headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    },
    "google": {
        "name": "Google",
        "url": "https://www.google.com/search?q={query}&num={num}&hl=zh-CN",
        "headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    },
}


# ═════════════════════════ HTML 解析 ═════════════════════════
def _parse_bing(html: str) -> list:
    results = []
    blocks = re.findall(r'<li[^>]*class="b_algo"[^>]*>.*?</li>', html, re.DOTALL)
    for block in blocks[:10]:
        tm = re.search(r'<h2[^>]*>(.*?)</h2>', block, re.DOTALL)
        if not tm:
            continue
        title = html_module.unescape(re.sub(r"<.*?>", "", tm.group(1)).strip())
        if not title or len(title) <= 3:
            continue
        sm = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
        snippet = html_module.unescape(re.sub(r"<.*?>", "", sm.group(1)).strip())[:250] if sm else ""
        lm = re.search(r'<a[^>]*href="([^"]+)"', block)
        url = lm.group(1) if lm else ""
        results.append({"title": title, "snippet": snippet, "url": url})
        if len(results) >= 10:
            break
    return results


def _parse_sogou(html: str) -> list:
    results = []
    titles = re.findall(
        r'<h3[^>]*class="(?:vr-title[^"]*)"[^>]*>.*?<a[^>]*>(.*?)</a>',
        html, re.DOTALL,
    )
    descs = re.findall(
        r'<div[^>]*class="(?:fz-mid[^"]*)"[^>]*>(.*?)</div>',
        html, re.DOTALL,
    )
    links = re.findall(
        r'<h3[^>]*class="(?:vr-title[^"]*)"[^>]*>.*?<a[^>]*href="([^"]*)"',
        html, re.DOTALL,
    )
    for i in range(min(len(titles), 10)):
        title = html_module.unescape(re.sub(r"<.*?>", "", titles[i]).strip())
        if not title or len(title) <= 3:
            continue
        snippet = html_module.unescape(re.sub(r"<.*?>", "", descs[i]).strip())[:250] if i < len(descs) else ""
        url = links[i] if i < len(links) else ""
        if url.startswith("/"):
            url = "https://www.sogou.com" + url
        results.append({"title": title, "snippet": snippet, "url": url})
        if len(results) >= 10:
            break
    return results


def _parse_google(html: str) -> list:
    results = []
    blocks = re.findall(
        r'<div[^>]*class="[^"]*g[^"]*"[^>]*>.*?<h3[^>]*>(.*?)</h3>.*?</div>',
        html, re.DOTALL,
    )
    for block in blocks[:10]:
        tm = re.search(r'<h3[^>]*>(.*?)</h3>', block, re.DOTALL)
        if not tm:
            continue
        title = html_module.unescape(re.sub(r"<.*?>", "", tm.group(1)).strip())
        if not title or len(title) <= 3:
            continue
        sm = re.search(r'<[^>]*class="[^"]*[^"]*"[^>]*>(.*?)</[^>]*>', block, re.DOTALL)
        snippet = html_module.unescape(re.sub(r"<.*?>", "", sm.group(1)).strip())[:250] if sm else ""
        lm = re.search(r'href="(/url\?q=([^"&]+))', block)
        url = urllib.parse.unquote(lm.group(2)) if lm else ""
        if url:
            results.append({"title": title, "snippet": snippet, "url": url})
        if len(results) >= 10:
            break
    return results


PARSERS = {"bing": _parse_bing, "sogou": _parse_sogou, "google": _parse_google}


# ═════════════════════════ 格式化 ═════════════════════════
def _format_for_user(query: str, results: list, engine_name: str) -> str:
    """用户命令输出：结构化但不累赘"""
    if not results:
        return f'🔍 未找到关于「{query}」的搜索结果，请换个关键词试试。'
    top = results[:8]
    lines = [
        f'🔍 **「{query}」— {engine_name}搜索结果（{len(top)} 条）**',
        '',
    ]
    for i, r in enumerate(top, 1):
        lines.append(f'{i}. **{r["title"]}**')
        if r["snippet"]:
            lines.append(f'   {r["snippet"]}')
        if r["url"]:
            lines.append(f'   🔗 {r["url"]}')
        if i < len(top):
            lines.append('')
    return '\n'.join(lines)


def _format_for_llm(query: str, results: list, engine_name: str) -> str:
    """LLM 工具输出：纯信息，不加总结指令，避免二次加工失真"""
    if not results:
        return f'未找到关于 "{query}" 的搜索结果。'
    lines = [f'{engine_name} 搜索 "{query}" 的结果（共 {min(len(results), 8)} 条）：', '']
    for i, r in enumerate(results[:8], 1):
        lines.append(f'{i}. {r["title"]}')
        if r["snippet"]:
            lines.append(f'   {r["snippet"]}')
        if r["url"]:
            lines.append(f'   {r["url"]}')
    return '\n'.join(lines)


# ═════════════════════════ 搜索后端 ═════════════════════════
class SearchBackend:
    def __init__(self, engine: str = "bing"):
        self.engine = engine

    def search(self, query: str, max_results: int = 8) -> str:
        """供 LLM 工具调用，返回纯搜索结果"""
        cfg = SEARCH_ENGINES.get(self.engine, SEARCH_ENGINES["bing"])
        try:
            url = cfg["url"].format(query=urllib.parse.quote(query), num=max_results)
            req = urllib.request.Request(url, headers=dict(cfg["headers"]))
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            results = PARSERS.get(self.engine, _parse_bing)(html)
            return _format_for_llm(query, results, cfg["name"])
        except urllib.error.HTTPError as e:
            return f'搜索 HTTP 错误({e.code})，请稍后重试。'
        except urllib.error.URLError as e:
            return f'搜索网络错误：{e.reason}'
        except TimeoutError:
            return '搜索超时，请稍后重试。'
        except Exception as e:
            return f'搜索出错：{type(e).__name__} - {e}'

    def search_for_user(self, query: str, max_results: int = 8) -> str:
        """供用户命令调用，返回结构化显示"""
        cfg = SEARCH_ENGINES.get(self.engine, SEARCH_ENGINES["bing"])
        try:
            url = cfg["url"].format(query=urllib.parse.quote(query), num=max_results)
            req = urllib.request.Request(url, headers=dict(cfg["headers"]))
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            results = PARSERS.get(self.engine, _parse_bing)(html)
            return _format_for_user(query, results, cfg["name"])
        except urllib.error.HTTPError as e:
            return f'🔍 搜索 HTTP 错误({e.code})，请稍后重试。'
        except urllib.error.URLError as e:
            return f'🔍 搜索网络错误：{e.reason}'
        except TimeoutError:
            return '🔍 搜索超时，请稍后重试。'
        except Exception as e:
            return f'🔍 搜索出错：{type(e).__name__} - {e}'


# ═════════════════════════ LLM 函数工具 ═════════════════════════
@dataclass
class WebSearchTool(FunctionTool):
    backend: SearchBackend = field(repr=False, default_factory=SearchBackend)
    name: str = "web_search"
    description: str = (
        "搜索互联网获取实时信息。当你需要最新事实、新闻、数据、百科等"
        "超出你知识范围的信息时调用此工具。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词或问题。",
                },
                "max_results": {
                    "type": "integer",
                    "description": "返回结果数量，1-10，默认8。",
                    "default": 8,
                },
            },
            "required": ["query"],
        }
    )

    async def run(self, event: AstrMessageEvent, query: str, max_results: int = 8) -> str:
        _ = event
        logger.info(f"[WebSearch] LLM 调用搜索: '{query}' (max={max_results})")
        result = await asyncio.to_thread(self.backend.search, query, max(1, min(max_results, 10)))
        return result


# ═════════════════════════ 插件 ═════════════════════════
@register(
    "astrbot_plugin_web_search",
    "openclaw",
    "多引擎搜索（Bing/搜狗/Google），LLM可主动调用，默认Bing",
    "3.1.3",
    "https://github.com/jujg12123/astrbot-web-search",
)
class WebSearchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)

        config = config or {}
        engine = config.get("engine", "bing")
        if engine not in SEARCH_ENGINES:
            engine = "bing"

        self._backend = SearchBackend(engine)
        self._tool = WebSearchTool(backend=self._backend)
        context.add_llm_tools(self._tool)
        logger.info(f"[WebSearch] v3.1.3 已就绪 | 引擎={engine}")

    async def terminate(self):
        self.context.provider_manager.llm_tools.remove_func(self._tool.name)
        logger.info("[WebSearch] 已卸载")

    @filter.command("websearch", alias={"搜索"})
    async def on_command(self, event: AstrMessageEvent, query: GreedyStr):
        query = query.strip()

        if query.lower() in ("changelog", "更新日志", "版本"):
            yield event.plain_result(CHANGELOG)
            return

        if not query:
            yield event.plain_result(
                "🔍 **多引擎搜索插件 v3.1.3**\n"
                "用法：/websearch 关键词\n"
                f"当前引擎：{self._backend.engine}\n"
                "输入 /websearch changelog 查看更新日志"
            )
            return

        result = await asyncio.to_thread(self._backend.search_for_user, query)
        yield event.plain_result(result)
