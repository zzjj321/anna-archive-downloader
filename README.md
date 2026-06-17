# Anna's Archive Book Downloader

Automated book downloader for [Anna's Archive](https://annas-archive.pk) via the slow channel. Bypasses DDoS-Guard, countdown timers, and handles resume/retry automatically.

## Features

- **DDoS-Guard bypass** - Automatic verification handling
- **Slow channel automation** - JS injection bypasses countdown timers
- **Smart filtering** - Excludes solutions manuals, matches author/title
- **Format priority** - PDF > EPUB > DjVu > MOBI
- **Resume download** - HTTP Range headers for interrupted downloads
- **Magic-byte detection** - Corrects wrong file extensions automatically
- **Batch download** - Process entire book lists with retry queue
- **Skip detection** - Checks existing files to avoid re-downloads

## Installation

```bash
pip install -r requirements.txt
playwright install chromium
```

Or install as a package:

```bash
pip install -e .
```

## Quick Start

### 1. Launch Chrome with CDP debugging

```bash
anna-download --launch
# Or manually:
# chrome --remote-debugging-port=9223 --user-data-dir=~/.anna-downloader/chrome-profile
```

### 2. Download a single book

```bash
anna-download -q "Classical Mechanics Taylor"
```

### 3. Batch download from book list

Create a book list file (`booklist.txt`):

```
Classical Mechanics | J.R. Taylor
Mechanics | Landau Lifshitz
Nonlinear Dynamics and Chaos | Strogatz
```

Run batch download:

```bash
anna-download --batch booklist.txt --prefer pdf --dir ./my_books
```

## Python API

```python
from anna_downloader import connect_cdp, get_page, search_books, download_book
from pathlib import Path

# Connect to Chrome
pw, browser = connect_cdp()
context = browser.contexts[0] if browser.contexts else browser.new_context()
page = get_page(context)

# Search and download
books = search_books(page, "Classical Mechanics Taylor", max_results=10)
if books:
    download_book(page, context, books[0], download_dir=Path("./downloads"))

browser.close()
pw.stop()
```

### Batch download API

```python
from anna_downloader import connect_cdp, get_page, BatchDownloader, parse_book_list
from pathlib import Path

pw, browser = connect_cdp()
context = browser.contexts[0] if browser.contexts else browser.new_context()
page = get_page(context)

# Parse book list
books = parse_book_list("booklist.txt")

# Run batch download
batch = BatchDownloader(page, context, download_dir="./downloads", prefer_fmt="pdf")
report, failed = batch.run(books)
batch.save_report()

browser.close()
pw.stop()
```

## Book List Format

One book per line. Supported separators: `|`, tab, or comma.

```
Title | Author
Another Title | Another Author
```

## Claude Code Skill

This package includes a Claude Code Skill definition at `.claude/skills/anna-download.md`. Once installed, Claude Code can use this skill to help you download books:

```
/anna-download booklist.txt --prefer pdf
```

## How It Works

1. **Search** - Query Anna's Archive with `title + author`
2. **Filter** - Remove solutions manuals, require author match
3. **Sort** - By author_match > title_match > format > downloads > size
4. **DDoS-Guard** - Navigate to slow_download, wait for verification
5. **JS injection** - Bypass countdown timer, extract direct download URL
6. **Download** - With retry/resume, fix extensions via magic bytes
7. **Rename** - `md5.ext` → `Title - Author.ext`

## Project Structure

```
anna-archive-downloader/
├── anna_downloader/
│   ├── __init__.py         # Package exports
│   ├── chrome.py           # Chrome CDP launch/connect
│   ├── downloader.py       # Core download engine
│   ├── batch.py            # Batch scheduler
│   ├── cli.py              # CLI entry point
│   └── inspect.js          # JS extraction script
├── examples/
│   └── classical_mechanics.txt
├── .claude/
│   └── skills/
│       └── anna-download.md
├── requirements.txt
├── pyproject.toml
└── README.md
```

## License

MIT
