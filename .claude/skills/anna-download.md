---
name: anna-download
description: Download books from Anna's Archive via slow channel automation
triggers:
  - /anna-download
  - download book from anna
  - anna archive download
  - 下载 anna 的书
---

# Anna's Archive Book Downloader

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

2. Launch Chrome with CDP debugging:
   ```bash
   python -c "from anna_downloader import launch_chrome; launch_chrome()"
   ```
   Or manually:
   ```bash
   chrome --remote-debugging-port=9223 --user-data-dir=~/.anna-downloader/chrome-profile
   ```

3. In the Chrome window, log into Anna's Archive if needed (to avoid DDoS-Guard repeatedly).

## Usage

When user asks to download books from Anna's Archive:

### Quick single book
```python
from anna_downloader import connect_cdp, get_page, find_best_book, download_book
from pathlib import Path

pw, browser = connect_cdp()
context = browser.contexts[0] if browser.contexts else browser.new_context()
page = get_page(context)

book = find_best_book(page, "Classical Mechanics Taylor")
if book:
    download_book(page, context, book, download_dir=Path("./downloads"))
else:
    print("No matching book found")

browser.close()
pw.stop()
```

`find_best_book` searches with detailed extraction and applies filtering
(excludes solutions manuals, requires author+title word match) then ranks by
author_match > title_match > format > downloads > size. It returns the single
best match — safer than grabbing `search_books(...)[0]`.

### Batch download from book list
```python
from anna_downloader import connect_cdp, get_page, BatchDownloader, parse_book_list
from pathlib import Path

pw, browser = connect_cdp()
context = browser.contexts[0] if browser.contexts else browser.new_context()
page = get_page(context)

books = parse_book_list("booklist.txt")  # format: Title | Author per line
batch = BatchDownloader(page, context, download_dir="./downloads", prefer_fmt="pdf")
report, failed = batch.run(books)
batch.save_report()

browser.close()
pw.stop()
```

### Book list format
One book per line, `Title | Author` separated by `|`, tab, or comma:
```
Classical Mechanics | J.R. Taylor
Mechanics | Landau Lifshitz
Nonlinear Dynamics and Chaos | Strogatz
```

## Commands

- `/anna-download <booklist_path>` - Download from book list file
- `/anna-download --prefer pdf <booklist_path>` - Prefer PDF format
- `/anna-download --dir ./my_books <booklist_path>` - Custom download directory

## Features

- **DDoS-Guard bypass**: Navigates to slow_download page, waits for verification
- **Slow channel countdown**: JS injection bypasses countdown timer
- **Smart filtering**: Excludes solutions manuals, matches author/title
- **Format priority**: PDF > EPUB > DjVu > MOBI
- **Resume download**: HTTP Range headers for interrupted downloads
- **Magic-byte detection**: Corrects wrong file extensions (PDF/EPUB/MOBI/DjVu)
- **Partial cleanup**: Removes orphaned incomplete files on startup
- **Retry queue**: Auto-retries failed books (up to 2 passes)
- **Skip detection**: Checks existing files by title+author in filename

## Workflow

1. Search Anna's Archive with `title + author` query
2. Filter results: exclude solutions manuals, require author match
3. Sort by: author_match > title_match > format > downloads > size
4. Navigate to slow_download → pass DDoS-Guard → inject JS → wait for Get button
5. Download with retry/resume → fix extension via magic bytes → rename to title