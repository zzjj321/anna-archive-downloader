# Anna's Archive 书籍下载器

自动化下载 [Anna's Archive](https://annas-archive.pk) 慢通道书籍。自动绕开 DDoS-Guard 人机验证、倒计时等待，支持断点续传和批量下载。

## 功能特性

- **DDoS-Guard 自动绕过** — 自动完成验证流程
- **慢通道自动化** — 注入 JS 绕过倒计时，直接拿到下载链接
- **智能筛选** — 排除 solutions manual，按作者 / 书名匹配度排序
- **格式优先级** — PDF > EPUB > DjVu > MOBI
- **断点续传** — 网络中断后用 HTTP Range 继续
- **Magic-byte 扩展名校验** — 自动修正错标的文件后缀
- **批量下载** — 从书单文件一次下载多本，失败自动重入队列
- **去重** — 已存在的文件自动跳过
- **自动重命名** — `md5.ext` → `书名 - 作者.ext`

## 安装

```bash
pip install -r requirements.txt
playwright install chromium
```

或者以包形式安装（会注册 `anna-download` 命令）：

```bash
pip install -e .
```

## 快速开始

### 1. 启动带 CDP 调试端口的 Chrome

```bash
anna-download --launch
```

或手动启动：

```bash
chrome --remote-debugging-port=9223 --user-data-dir=~/.anna-downloader/chrome-profile
```

> 首次启动时若遇到 DDoS-Guard，请在弹出的浏览器窗口中手动完成一次验证，cookie 会被保存到 `chrome-profile`，后续请求即可免验证。

### 2. 下载单本书

```bash
anna-download -q "Classical Mechanics Taylor"
```

### 3. 批量下载

创建书单文件 `booklist.txt`（分隔符支持 `|`、Tab、逗号）：

```
Classical Mechanics | J.R. Taylor
Mechanics | Landau Lifshitz
Nonlinear Dynamics and Chaos | Strogatz
```

执行批量下载：

```bash
anna-download --batch booklist.txt --prefer pdf --dir ./my_books
```

## 命令行参数

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--launch` | 启动带 CDP 的 Chrome | — |
| `--port PORT` | CDP 端口 | `9223` |
| `-q, --query QUERY` | 单本书搜索词（建议"书名 + 作者"） | — |
| `--batch BATCH` | 书单文件路径 | — |
| `--dir DIR` | 下载目录 | `./downloads` |
| `--prefer {pdf,epub,djvu,mobi}` | 首选格式 | — |
| `-n, --count N` | 下载数量（配合 `-q`） | `1` |

## Python API

### 单本下载

```python
from anna_downloader import connect_cdp, get_page, find_best_book, download_book
from pathlib import Path

pw, browser = connect_cdp()
context = browser.contexts[0] if browser.contexts else browser.new_context()
page = get_page(context)

# find_best_book 做严格过滤（作者词 + 标题匹配），返回 list
# 空 list 表示搜索无结果或无相关结果（即 Anna's Archive 未收录）
books = find_best_book(page, "Classical Mechanics Taylor", n=1)
if books:
    download_book(page, context, books[0], download_dir=Path("./downloads"))
else:
    print("Anna's Archive 未收录此书")

browser.close()
pw.stop()
```

### 批量下载

```python
from anna_downloader import connect_cdp, get_page, BatchDownloader, parse_book_list
from pathlib import Path

pw, browser = connect_cdp()
context = browser.contexts[0] if browser.contexts else browser.new_context()
page = get_page(context)

books = parse_book_list("booklist.txt")
batch = BatchDownloader(page, context, download_dir="./downloads", prefer_fmt="pdf")
report, failed = batch.run(books)
batch.save_report()

browser.close()
pw.stop()
```

## 工作流程

1. **搜索** — 用 `书名 + 作者` 在 Anna's Archive 查询
2. **过滤** — 剔除 solutions manual，要求作者词命中
3. **排序** — 按 `author_match > title_match > format > downloads > size` 选最优
4. **DDoS-Guard** — 导航到 `slow_download` 页面，等待（或手动完成）验证
5. **JS 注入** — 注入脚本绕过倒计时，提取直链
6. **下载** — 带重试 / 断点续传，用 magic bytes 校验扩展名
7. **重命名** — `md5.ext` → `书名 - 作者.ext`

## 项目结构

```
anna-archive-downloader/
├── anna_downloader/
│   ├── __init__.py         # 包导出
│   ├── chrome.py           # Chrome CDP 启动 / 连接
│   ├── downloader.py       # 核心下载引擎
│   ├── batch.py            # 批量调度器
│   ├── cli.py              # CLI 入口
│   └── inspect.js          # 搜索结果结构化提取脚本
├── examples/
│   └── classical_mechanics.txt
├── requirements.txt
├── pyproject.toml
└── README.md
```

## 常见问题

**Q: 总是弹出 DDoS-Guard 验证？**
A: 用 `--launch` 启动 Chrome 后，在弹出的浏览器窗口中手动完成一次验证，cookie 会保存到 `~/.anna-downloader/chrome-profile`，后续请求可免验证。不要每次都用新的 profile。

**Q: 下载速度很慢？**
A: 慢通道本身限速，属正常现象。脚本默认带断点续传，中断后再跑一次会继续下载。

**Q: 搜索结果里出现 solutions manual？**
A: 已内置过滤，但如果关键词本身包含 "solution"，可能误伤。建议关键词只写 "书名 + 作者"。

## 许可证

MIT
