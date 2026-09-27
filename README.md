# 本地 LLM Wiki

一个完全在本机运行的 Markdown Wiki。页面、全文搜索索引与历史版本均保存在 `D:\LocalLLMWiki\data\wiki.db`，问答默认连接本机 Ollama，不上传知识库内容。

## 启动

1. 双击或在 PowerShell 运行 `start-wiki.ps1`。
2. 浏览器打开 <http://127.0.0.1:3210>。

本项目使用 Python 3.10+ 标准库，无需安装 pip 依赖。

## 启用本地问答

安装并启动 Ollama 后，下载一个中文效果较好的模型：

```powershell
ollama pull qwen3:4b
```

刷新 Wiki，右下角显示“本地模型已连接”后即可提问。

## 配置

```powershell
$env:WIKI_DATA_DIR = 'D:\LocalLLMWiki\data'
$env:OLLAMA_URL = 'http://127.0.0.1:11434'
$env:PORT = '3210'
python server.py
```

备份时只需复制 `D:\LocalLLMWiki\data` 整个目录。
