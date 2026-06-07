# OpenClaw Web Search Plugin for AstrBot

## 功能
- **/websearch [关键词]** — 手动触发联网搜索
- **web_search** — LLM函数工具，AI在对话中可**主动调用**搜索

## 安装

1. 复制插件文件到 AstrBot 插件目录：
   ```bash
   cp astrbot_plugin_openclaw_search.py <AstrBot数据目录>/data/plugin/
   ```

2. 重启 AstrBot

## 配置（可选）

创建配置文件 `<AstrBot数据目录>/data/plugin_config/openclaw_web_search.json`：

```json
{
  "enabled": true,
  "max_results": 5,
  "search_lang": "zh",
  "freshness": ""
}
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| enabled | true | 是否启用 |
| max_results | 5 | 最大结果数 |
| search_lang | zh | 搜索语言：zh 或 en |
| freshness | (空) | 时间过滤：day/week/month/year |

## LLM 函数工具说明

`web_search` 工具会让 AI 在对话中自动感知并可能在需要时主动调用：

- **query** (string, 必填) — 搜索内容
- **max_results** (number, 可选) — 结果数量
- **language** (string, 可选) — 搜索语言
- **freshness** (string, 可选) — 时间范围：day/week/month/year

## 依赖

- Python 3.8+
- 网络连通性（Google搜索）
- 无需额外 pip 包
