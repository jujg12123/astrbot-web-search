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

**v3.1.2** (2026-06-08)
- 🔧 修复插件配置不生效的问题（改为接收 AstrBotConfig）
- 🌐 默认搜索引擎改为 **Bing**（国内可直连，结果全面）
- 📝 新增 /websearch changelog 命令查看更新日志
- 🎨 配置界面增强：每个引擎附带详细描述

**v3.1.0** (2026-06-08)
- 🤖 LLM 函数工具改用 add_llm_tools() 显式注册
- 📊 搜索结果双格式：LLM 收到纯文本，用户看到富格式
- 🧹 新增 terminate() 清理机制
- 🔤 支持中文别名 /搜索

**v3.0.x** (2026-06-07)
- 🌐 多引擎支持：搜狗 / Google / Bing
- 📌 skill 风格结构化输出
- ⚙️ 插件配置界面
""".strip()

# ═════════════════════════ 搜索引擎 ═════════════════════════
SEARCH_ENGINES = {
    "bing": {
        "name": "Bing",
        "url": "https://cn.bing.com/search?q={query}&count={num}",
        "headers": {
            "User-Agent": "Mozilla/5.0 (compatible; AstrBot/1.0)",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    },
    "sogou": {
        "name": "搜狗",
        "url": "https://www.sogou.com/web?query={query}",
        "headers": {
            "User-Agent": "Mozilla/5.0 (compatible; AstrBot/1.0)",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    },
    "google": {
        "name": "Google",
        "url": "https://www.google.com/search?q={query}&num={num}&hl=zh-CN",
        "headers": {
            "User-Agent": "Mozilla/5.0 (compatible; AstrBot/1.0)",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    },
}


# ═════════════════════════ HTML 解析 ═════════════════════════
def _parse_bing(html: str) -> list:
    results = []
    blocks = re.findall(r'<li[^>]*class="b_algo"[^>]*>.*?</li>', html, re.DOTALL)
    for block in blocks[:10]:
        tm = re.search(r'<h2[^>]*>(.*?)</h2>', block, re.DOTALL)
        if not tm: continue
        title = html_module.unescape(re.sub(r"<.*?>", "", tm.group(1)).strip())
        if not title or len(title) <= 3: continue
        sm = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
        snippet = html_module.unescape(re.sub(r"<.*?>", "", sm.group(1)).strip())[:200] if sm else ""
        lm = re.search(r'<a[^>]*href="([^"]+)"', block)
        url = lm.group(1) if lm else ""
        results.append({"title": title, "snippet": snippet, "url": url})
        if len(results) >= 10: break
    return results


def _parse_sogou(html: str) -> list:
    results = []
    titles = re.findall(r'<h3[^>]*class="(?:vr-title[^"]*)"[^>]*>.*?<a[^>]*>(.*?)</a>', html, re.DOTALL)
    descs = re.findall(r'<div[^>]*class="(?:fz-mid[^"]*)"[^>]*>(.*?)</div>', html, re.DOTALL)
    links = re.findall(r'<h3[^>]*class="(?:vr-title[^"]*)"[^>]*>.*?<a[^>]*href="([^"]*)"', html, re.DOTALL)
    for i in range(min(len(titles), 10)):
        title = html_module.unescape(re.sub(r"<.*?>", "", titles[i]).strip())
        if not title or len(title) <= 3: continue
        snippet = html_module.unescape(re.sub(r"<.*?>", "", descs[i]).strip())[:200] if i < len(descs) else ""
        url = links[i] if i < len(links) else ""
        if url.startswith("/"): url = "https://www.sogou.com" + url
        results.append({"title": title, "snippet": snippet, "url": url})
        if len(results) >= 10: break
    return results


def _parse_google(html: str) -> list:
    results = []
    blocks = re.findall(r'<div[^>]*class="[^"]*g[^"]*"[^>]*>.*?<h3[^>]*>(.*?)</h3>.*?</div>', html, re.DOTALL)
    for block in blocks[:10]:
        tm = re.search(r'<h3[^>]*>(.*?)</h3>', block, re.DOTALL)
        if not tm: continue
        title = html_module.unescape(re.sub(r"<.*?>", "", tm.group(1)).strip())
        if not title or len(title) <= 3: continue
        sm = re.search(r'<[^>]*class="[^"]*[^"]*"[^>]*>(.*?)</[^>]*>', block, re.DOTALL)
        snippet = html_module.unescape(re.sub(r"<.*?>", "", sm.group(1)).strip())[:200] if sm else ""
        lm = re.search(r'href="(/url\?q=([^"&]+))', block)
        url = urllib.parse.unquote(lm.group(2)) if lm else ""
        if url: results.append({"title": title, "snippet": snippet, "url": url})
        if len(results) >= 10: break
    return results


PARSERS = {"bing": _parse_bing, "sogou": _parse_sogou, "google": _parse_google}


# ═════════════════════════ 格式化 ═════════════════════════
def _format_for_user(query: str, results: list, engine_name: str) -> str:
    if not results:
        return f'🔍 「{query}」未找到相关结果，请换个关键词试试。'
    top = results[:5]
    core = top[0]
    lines = [
        f'🔍 **关于「{query}」的搜索结果（{engine_name}）**',
        '',
        f'📌 **核心发现**',
        f'**{core["title"]}**',
        f'> {core["snippet"]}',
        f'> 🔗 {core["url"]}',
        '',
        f'📋 **详细信息**',
    ]
    for i, r in enumerate(top, 1):
        lines.append(f'{i}. **{r["title"]}**')
        if r["snippet"]: lines.append(f'   - {r["snippet"]}')
        if r["url"]: lines.append(f'   🔗 {r["url"]}')
    lines.append('')
    lines.append(f'💡 以上结果来自{engine_name}，可尝试更具体的关键词。')
    return '\n'.join(lines)


def _format_for_llm(query: str, results: list, engine_name: str) -> str:
    if not results:
        return f'未找到关于 "{query}" 的搜索结果。'
    lines = [f'以下是关于 "{query}" 的{engine_name}搜索结果：', '']
    for i, r in enumerate(results[:5], 1):
        lines.append(f'{i}. {r["title"]}')
        if r["snippet"]: lines.append(f'   摘要: {r["snippet"]}')
        if r["url"]: lines.append(f'   链接: {r["url"]}')
    lines.append('')
    lines.append('请根据以上搜索结果，帮用户总结要点并回答。')
    return '\n'.join(lines)


# ═════════════════════════ 搜索后端 ═════════════════════════
class SearchBackend:
    def __init__(self, engine: str = "bing"):
        self.engine = engine

    def search(self, query: str, max_results: int = 5) -> str:
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

    def search_for_user(self, query: str, max_results: int = 5) -> str:
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
        "搜索互联网获取实时信息。当你需要了解最新事实、新闻、数据、百科等"
        "超出你知识范围的信息时调用此工具。返回搜索结果后请根据内容回答用户。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词或问题，提取用户的核心查询意图。"
                },
                "max_results": {
                    "type": "integer",
                    "description": "返回结果数量，1-10，默认5。",
                    "default": 5,
                },
            },
            "required": ["query"],
        }
    )

    async def run(self, event: AstrMessageEvent, query: str, max_results: int = 5) -> str:
        _ = event
        logger.info(f"[WebSearch] LLM 调用搜索: '{query}' (max={max_results})")
        result = await asyncio.to_thread(self.backend.search, query, max(1, min(max_results, 10)))
        return result


# ═════════════════════════ 插件 ═════════════════════════
@register(
    "astrbot_plugin_web_search",
    "openclaw",
    "多引擎搜索（Bing/搜狗/Google），LLM可主动调用，搜索结果反馈给AI",
    "3.1.2",
    "https://github.com/jujg12123/astrbot-web-search",
)
class WebSearchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)

        # 从 AstrBotConfig 加载配置（修复配置不生效问题）
        config = config or {}
        engine = config.get("engine", "bing")
        if engine not in SEARCH_ENGINES:
            engine = "bing"

        self._backend = SearchBackend(engine)
        self._tool = WebSearchTool(backend=self._backend)
        context.add_llm_tools(self._tool)
        logger.info(f"[WebSearch] v3.1.2 已就绪 | 引擎={engine}")

    async def terminate(self):
        self.context.provider_manager.llm_tools.remove_func(self._tool.name)
        logger.info("[WebSearch] 已卸载")

    @filter.command("websearch", alias={"搜索", "websearch"})
    async def on_command(self, event: AstrMessageEvent, query: GreedyStr):
        query = query.strip()

        if query.lower() in ("changelog", "更新日志", "版本"):
            yield event.plain_result(CHANGELOG)
            return

        if not query:
            yield event.plain_result(
                "🔍 **多引擎搜索插件 v3.1.2**\n"
                "用法：/websearch 关键词\n"
                f"当前引擎：{self._backend.engine}\n"
                "输入 /websearch changelog 查看更新日志"
            )
            return

        result = await asyncio.to_thread(self._backend.search_for_user, query)
        yield event.plain_result(result)
