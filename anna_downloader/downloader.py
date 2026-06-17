"""Core download engine: search, DDoS-Guard bypass, slow-channel download."""

import logging
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus

import requests
import urllib3

urllib3.disable_warnings()

log = logging.getLogger("anna_downloader")

BASE_URL = "https://annas-archive.pk"
VALID_EXTS = {".pdf", ".djvu", ".epub", ".mobi", ".zip", ".rar"}

# JS injection for slow-channel countdown bypass
INJECT_JS = r"""
(function() {
  'use strict';
  let isCountdownRunning = false;
  const pendingRequests = [];
  function getScidbDirectUrl(url) {
    try {
      const parsed = new URL(url);
      if (parsed.pathname === '/scidb') {
        const doi = parsed.searchParams.get('doi');
        if (doi) return `${parsed.origin}/scidb/${doi}/`;
      }
    } catch(e) {}
    return null;
  }
  function fetchStatus(url, container, canLeadCountdown) {
    if (container.getAttribute('data-status') === 'done') return;
    const scidbUrl = getScidbDirectUrl(url);
    if (scidbUrl) {
      container.innerHTML = ` [<a href="${scidbUrl}" target="_blank" style="color:#28a745;font-weight:bold">Get</a>]`;
      container.setAttribute('data-status', 'done');
      return;
    }
    container.innerHTML = ' [Fetching...]';
    fetch(url).then(r => r.text()).then(html => {
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const span = doc.querySelector('span.bg-gray-200');
      if (span) {
        const u = span.textContent.trim();
        container.innerHTML = ` [<a href="${u}" target="_blank" style="color:#28a745;font-weight:bold">Get</a>]`;
        container.setAttribute('data-status', 'done');
        return;
      }
      const cd = doc.querySelector('.js-partner-countdown');
      if (cd) {
        let s = parseInt(cd.innerText);
        if (!isCountdownRunning && canLeadCountdown) {
          isCountdownRunning = true;
          container.setAttribute('data-status', 'waiting');
          const iv = setInterval(() => {
            if (s <= 0) {
              clearInterval(iv);
              container.innerHTML = ' [Preparing...]';
              fetchStatus(url, container, true);
              setTimeout(() => { isCountdownRunning = false; while(pendingRequests.length){ const r=pendingRequests.shift(); fetchStatus(r.url,r.container,true); } }, 1500);
              return;
            }
            container.innerHTML = ` [Wait: ${s}s]`;
            s--;
          }, 1000);
        } else {
          container.innerHTML = ' [Queued]';
          pendingRequests.push({url, container});
        }
      } else {
        container.innerHTML = ' [Need manual verify]';
      }
    }).catch(() => { container.innerHTML = ' [Error]'; });
  }
  function init() {
    const headers = Array.from(document.querySelectorAll('h3'));
    const slowHeader = headers.find(h => h.textContent.includes('Slow'));
    if (!slowHeader) return;
    const slowSection = slowHeader.closest('div');
    if (!slowSection) return;
    const slowLinks = slowSection.querySelectorAll('a.js-download-link');
    slowLinks.forEach((link, index) => {
      let container = link.parentNode.querySelector('.direct-link-container');
      if (container && container.getAttribute('data-status') === 'done') return;
      if (!container) {
        container = document.createElement('span');
        container.className = 'direct-link-container';
        container.style.marginLeft = '10px';
        container.innerHTML = ' [Checking...]';
        link.parentNode.insertBefore(container, link.nextSibling);
      }
      setTimeout(() => fetchStatus(link.href, container, index === 0), index * 200);
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
"""

# Load inspect.js from package directory
_INSPECT_JS_PATH = Path(__file__).parent / "inspect.js"
_INSPECT_JS_FALLBACK = r"""() => {
    const rows = document.querySelectorAll('div[class*="flex"][class*="pt-3"][class*="pb-3"][class*="border-b"]');
    return Array.from(rows).map(row => {
        const link = row.querySelector('a[href*="/md5/"]');
        if (!link) return null;
        const href = link.getAttribute('href') || '';
        const md5m = href.match(/[a-f0-9]{32}/);
        if (!md5m) return null;
        const titleEl = row.querySelector('a[class*="line-clamp-[3]"]');
        const title = titleEl ? titleEl.innerText.trim() : '';
        const authorEls = row.querySelectorAll('a[class*="line-clamp-[2]"]');
        let author = '';
        for (const a of authorEls) {
            const t = a.innerText.trim();
            if (t.length > 0 && !t.startsWith('/') && !t.includes('\\')) { author = t; break; }
        }
        const metaDiv = row.querySelector('div[class*="text-gray-800"][class*="dark:text-slate-400"]');
        const metaText = metaDiv ? metaDiv.innerText.trim() : '';
        return { md5: md5m[0], title, author, metaText };
    }).filter(x => x !== null && x.md5);
}"""


def load_inspect_js():
    """Load inspect.js from package, with inline fallback."""
    try:
        if _INSPECT_JS_PATH.exists():
            return _INSPECT_JS_PATH.read_text(encoding="utf-8")
    except Exception:
        pass
    return _INSPECT_JS_FALLBACK


INSPECT_JS = load_inspect_js()


# ---------- Browser helpers ----------

def get_page(context):
    """Get or create a page in the browser context."""
    pages = context.pages
    if pages:
        for p in pages[1:]:
            try:
                p.close()
            except Exception:
                pass
        return pages[0]
    return context.new_page()


# ---------- Search ----------

def search_books(page, keyword, max_results=30):
    """Search Anna's Archive, return [{title, md5}] ranked by keyword relevance.

    Delegates to search_books_detailed for structured extraction, then strips
    extra fields. Avoids the original naive a[href*="/md5/"] scrape which
    pulled sidebar/recommendation links with bogus titles.
    """
    detailed = search_books_detailed(page, keyword, max_results=max_results)
    return [{"title": b.get("title", ""), "md5": b["md5"]} for b in detailed]


def search_books_detailed(page, query, max_results=30):
    """Search with structured extraction: title, author, size, downloads, format."""
    search_url = f"{BASE_URL}/search?q={quote_plus(query)}"
    log.info(f"search: {search_url}")

    page.goto(search_url, timeout=60000, wait_until="domcontentloaded")
    time.sleep(random.uniform(3, 5))

    results = page.evaluate(INSPECT_JS)

    books = []
    seen_md5 = set()
    for r in results:
        md5 = r["md5"]
        if md5 in seen_md5:
            continue
        seen_md5.add(md5)

        meta = r.get("metaText", "")
        size_mb = 0.0
        downloads = 0
        fmt = ""

        m = re.search(r'(\d+(?:\.\d+)?)\s*MB', meta)
        if m:
            size_mb = float(m.group(1))

        for fmt_name in ["pdf", "epub", "djvu", "mobi", "azw3"]:
            if fmt_name in meta.lower():
                fmt = fmt_name
                break

        save_match = re.search(r'Save\s*.\s*([\d,]+)', meta)
        if save_match:
            downloads = int(save_match.group(1).replace(",", ""))

        books.append({
            "md5": md5,
            "title": r.get("title", ""),
            "author": r.get("author", ""),
            "size_mb": size_mb,
            "downloads": downloads,
            "fmt": fmt,
        })
        if len(books) >= max_results:
            break

    log.info(f"found {len(books)} results")
    return _keyword_rank_detailed(books, query)


def _keyword_rank_detailed(books, query):
    """Rank detailed books by keyword overlap with title+author."""
    stop = {"the", "a", "an", "of", "to", "in", "for", "and", "with", "by"}
    keywords = [k.lower() for k in query.split() if len(k) > 2 and k.lower() not in stop]
    if not keywords:
        return books

    def score(book):
        title = (book.get("title") or "").lower()
        author = (book.get("author") or "").lower()
        s = sum(10 for kw in keywords if kw in title)
        s += sum(20 for kw in keywords if kw in author)
        if "solution" in title:
            s -= 1000
        fmt = (book.get("fmt") or "").lower()
        s += {"pdf": 4, "epub": 3, "djvu": 2, "mobi": 1}.get(fmt, 0)
        return s

    return sorted(books, key=score, reverse=True)


def find_best_book(page, query, max_results=30):
    """Search and return the single best matching book.

    Uses detailed search + batch filter/pick logic:
      - excludes solutions manuals / instructor editions
      - requires author-word match + partial title match
      - sorts by: author_match > title_match > format > downloads > size

    Returns book dict (md5, title, author, fmt, ...) or None.
    """
    from .batch import filter_results, pick_best

    results = search_books_detailed(page, query, max_results=max_results)
    if not results:
        return None

    filtered = filter_results(results, author_query=query, book_title=query)
    pool = filtered if filtered else results
    return pick_best(pool)


# ---------- DDoS-Guard ----------

def pass_ddos_guard(page, md5, server_index=0, timeout=120):
    """Navigate to slow_download page, wait for DDoS-Guard verification."""
    slow_url = f"{BASE_URL}/slow_download/{md5}/0/{server_index}"
    log.info(f"  DDoS-Guard: {slow_url}")

    page.goto(slow_url, timeout=60000, wait_until="domcontentloaded")
    time.sleep(3)

    title = page.title()
    body_text = page.inner_text("body")[:200] if title != "Page Not Found" else ""

    if "DDoS" in title or "Checking" in body_text:
        log.info("  DDoS-Guard detected, waiting for verification...")
        for i in range(timeout // 2):
            time.sleep(2)
            title = page.title()
            body_text = page.inner_text("body")[:100]
            if "DDoS" not in title and "Checking" not in body_text:
                log.info(f"  DDoS-Guard passed! ({(i+1)*2}s)")
                return True
        log.error("  DDoS-Guard verification timeout")
        return False
    else:
        log.info("  No DDoS-Guard")
        return True


# ---------- Download ----------

def download_book(page, context, book, download_dir=None):
    """Full download flow: DDoS-Guard -> detail page -> JS inject -> get link -> download.

    Args:
        page: Playwright page
        context: Playwright browser context
        book: dict with 'md5' and 'title'
        download_dir: Path to save files (default: ./downloads)
    """
    if download_dir is None:
        download_dir = Path("./downloads")
    download_dir = Path(download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    md5 = book["md5"]
    title = book.get("title", "")[:80]

    log.info(f"\n{'='*60}")
    log.info(f"download: {title}")
    log.info(f"md5: {md5}")

    if not pass_ddos_guard(page, md5, server_index=0):
        return False

    detail_url = f"{BASE_URL}/md5/{md5}"
    log.info("  returning to detail page...")
    page.goto(detail_url, timeout=60000, wait_until="domcontentloaded")
    time.sleep(random.uniform(2, 4))

    log.info("  injecting JS...")
    try:
        page.evaluate(INJECT_JS)
    except Exception as e:
        log.error(f"  inject failed: {e}")
        return False

    log.info("  waiting for slow channel...")
    direct_url = None

    for i in range(40):
        time.sleep(2)
        elapsed = (i + 1) * 2

        try:
            get_urls = page.evaluate("""() => {
                const links = document.querySelectorAll('span.direct-link-container a[style*="color:#28a745"]');
                return Array.from(links).map(a => a.getAttribute('href')).filter(h => h && h.startsWith('http'));
            }""")
            if get_urls:
                direct_url = get_urls[0]
                log.info(f"  got link! ({elapsed}s)")
                break
        except Exception:
            pass

        if elapsed % 10 == 0:
            try:
                statuses = page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('span.direct-link-container')).map(c => c.innerText.trim());
                }""")
                log.info(f"  [{elapsed}s] {statuses[:4]}")
            except Exception:
                pass

        if elapsed >= 60:
            break

    if not direct_url:
        log.error("  no download link obtained")
        return False

    author = book.get("author", "")
    return _do_download(context, direct_url, md5, download_dir, title, author)


def _do_download(context, direct_url, md5, download_dir, book_title="", author=""):
    """Download file with retry and resume support."""
    log.info(f"  downloading: {direct_url[:100]}...")

    session = requests.Session()
    session.verify = False
    session.trust_env = False

    for c in context.cookies():
        session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))

    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/131.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Referer": f"{BASE_URL}/",
    })

    MAX_RETRIES = 5
    filename = f"{md5}.pdf"
    file_path = download_dir / filename

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            existing_size = file_path.stat().st_size if file_path.exists() else 0
            dl_headers = dict(session.headers)
            if existing_size > 0:
                dl_headers["Range"] = f"bytes={existing_size}-"
                log.info(f"    resume from {existing_size/(1024*1024):.1f} MB (attempt {attempt})")

            resp = session.get(direct_url, timeout=(30, 900), stream=True, headers=dl_headers)
            resp.raise_for_status()

            cd_h = resp.headers.get("content-disposition", "")
            if "filename" in cd_h:
                for p in [r"filename\*?=(?:UTF-8''|)([^;\s\"]+)", r'filename="?([^";\s]+)"?']:
                    m = re.search(p, cd_h)
                    if m:
                        filename = m.group(1).strip('" ')
                        break
            if not filename or filename == f"{md5}.pdf":
                ct = resp.headers.get("content-type", "")
                ext = ".pdf"
                if "djvu" in ct:
                    ext = ".djvu"
                elif "epub" in ct:
                    ext = ".epub"
                filename = f"{md5}{ext}"

            filename = re.sub(r'[<>:"/\\|?*]', "_", filename)
            file_path = download_dir / filename

            mode = "ab" if existing_size > 0 and resp.status_code == 206 else "wb"
            if mode == "wb":
                existing_size = 0

            total = existing_size
            content_length_header = int(resp.headers.get("content-length", 0))
            total_expected = (existing_size + content_length_header
                              if resp.status_code == 206 else content_length_header)
            start_time = time.time()
            last_report_time = start_time
            downloaded_this_attempt = 0

            with open(file_path, mode) as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        total += len(chunk)
                        downloaded_this_attempt += len(chunk)
                        now = time.time()
                        if now - last_report_time >= 30:
                            elapsed = now - start_time
                            speed = downloaded_this_attempt / elapsed / 1024 if elapsed > 0 else 0
                            if total_expected:
                                pct = total / total_expected * 100
                                log.info(f"    ... {total/(1024*1024):.1f}/{total_expected/(1024*1024):.1f} MB "
                                         f"({pct:.0f}%) @ {speed:.1f} KB/s")
                            else:
                                log.info(f"    ... {total/(1024*1024):.1f} MB @ {speed:.1f} KB/s")
                            last_report_time = now

            size_mb = total / (1024*1024)
            if size_mb < 0.01:
                log.warning(f"  file too small ({size_mb:.3f} MB)")
                file_path.unlink(missing_ok=True)
                return False

            file_path = _fix_extension(file_path)

            if book_title:
                renamed_path = rename_to_title(download_dir, md5, book_title, author)
                if renamed_path:
                    file_path = renamed_path

            log.info(f"  [OK] {file_path.name} ({size_mb:.1f} MB)")
            return True

        except Exception as e:
            wait = 15 * attempt
            log.error(f"    connection lost ({e}), retry {attempt}/{MAX_RETRIES} in {wait}s...")
            if attempt >= MAX_RETRIES:
                log.error(f"  [FAIL] download failed")
                return False
            time.sleep(wait)

    return False


def _fix_extension(file_path):
    """Detect actual file type via magic bytes and rename if extension is wrong."""
    try:
        with open(file_path, "rb") as f:
            header = f.read(1024)
    except Exception:
        return file_path

    if len(header) < 8:
        return file_path

    detected_ext = None

    if header.startswith(b"%PDF"):
        detected_ext = ".pdf"
    elif header.startswith(b"DjV") or header.startswith(b"AT&TFORM"):
        detected_ext = ".djvu"
    elif header.startswith(b"PK\x03\x04"):
        if b"mimetype" in header[:100] and b"application/epub+zip" in header[:512]:
            detected_ext = ".epub"
        elif len(header) > 67 and header[60:68] == b"BOOKMOBI":
            detected_ext = ".mobi"
        else:
            detected_ext = ".zip"

    if not detected_ext:
        return file_path

    current_ext = file_path.suffix.lower()
    if current_ext == detected_ext:
        return file_path

    new_path = file_path.with_suffix(detected_ext)
    log.info(f"  format fix: {file_path.suffix} -> {detected_ext}")
    file_path.rename(new_path)
    return new_path


def rename_to_title(download_dir, md5, book_title, author):
    """Rename md5.ext to 'Book Title - Author.ext'."""
    download_dir = Path(download_dir)
    for ext in VALID_EXTS:
        src = download_dir / f"{md5}{ext}"
        if src.exists():
            display = f"{book_title} - {author}".strip(" -") if author else book_title
            safe_name = re.sub(r'[<>:"/\\|?*]', "_", display)
            dst = download_dir / f"{safe_name}{ext}"
            if dst.exists() and dst != src:
                dst = download_dir / f"{safe_name}_{md5[:8]}{ext}"
            if src != dst:
                src.rename(dst)
                log.info(f"  renamed: {src.name} -> {dst.name}")
            return dst
    return None
