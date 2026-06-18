"""Core download engine: search, DDoS-Guard bypass, slow-channel download."""

import logging
import os
import random
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote_plus

import requests
import urllib3

urllib3.disable_warnings()

log = logging.getLogger("anna_downloader")

BASE_URL = "https://annas-archive.pk"
VALID_EXTS = {".pdf", ".djvu", ".epub", ".mobi", ".zip", ".rar"}

# JS injection for slow-channel countdown bypass.
# All slow channels run their countdowns in parallel (no queueing) so we can
# collect multiple direct URLs and race them in Python.
INJECT_JS = r"""
(function() {
  'use strict';
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
  function fetchStatus(url, container) {
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
        container.setAttribute('data-status', 'waiting');
        const iv = setInterval(() => {
          if (s <= 0) {
            clearInterval(iv);
            container.innerHTML = ' [Preparing...]';
            fetchStatus(url, container);
            return;
          }
          container.innerHTML = ` [Wait: ${s}s]`;
          s--;
        }, 1000);
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
      setTimeout(() => fetchStatus(link.href, container), index * 200);
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


def find_best_book(page, query, max_results=30, n=1):
    """Search and return the top-N best matching books.

    Uses detailed search + batch filter/pick logic:
      - excludes solutions manuals / instructor editions
      - requires author-word match + partial title match
      - sorts by: author_match > title_match > format > downloads > size

    Returns list of book dicts (md5, title, author, fmt, ...), possibly empty.
    With n=1 (default), callers typically unwrap with `[0]` after checking length.
    """
    from .batch import filter_results, pick_best

    results = search_books_detailed(page, query, max_results=max_results)
    if not results:
        return []

    filtered = filter_results(results, author_query=query, book_title=query)
    if not filtered:
        return []

    pick_best(filtered)  # sorts in place
    return filtered[:n]


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
    direct_urls = []
    max_servers = 4
    poll_timeout = 60

    for i in range(poll_timeout // 2):
        time.sleep(2)
        elapsed = (i + 1) * 2

        try:
            get_urls = page.evaluate("""() => {
                const links = document.querySelectorAll('span.direct-link-container a[style*="color:#28a745"]');
                return Array.from(links).map(a => a.getAttribute('href')).filter(h => h && h.startsWith('http'));
            }""")
            # dedupe preserving order
            seen = set()
            direct_urls = []
            for u in get_urls:
                if u not in seen:
                    seen.add(u)
                    direct_urls.append(u)
            if len(direct_urls) >= max_servers:
                log.info(f"  got {len(direct_urls)} links ({elapsed}s)")
                break
            elif direct_urls and elapsed >= 20:
                # after 20s, accept whatever we have (most countdowns finish by then)
                log.info(f"  got {len(direct_urls)} link(s) ({elapsed}s)")
                break
        except Exception:
            pass

        if elapsed % 10 == 0:
            try:
                statuses = page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('span.direct-link-container')).map(c => c.innerText.trim());
                }""")
                log.info(f"  [{elapsed}s] {statuses[:max_servers]}")
            except Exception:
                pass

        if elapsed >= poll_timeout:
            break

    if not direct_urls:
        log.error("  no download link obtained")
        return False

    author = book.get("author", "")
    if len(direct_urls) == 1:
        return _do_download(context, direct_urls[0], md5, download_dir, title, author)
    return _do_download_parallel(context, direct_urls, md5, download_dir, title, author)


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


# ---------- Parallel / race download ----------

PROBE_SECONDS = 5

# Per-server health tracking (in-memory, process lifetime).
# Keyed by download URL host (e.g. "93.123.118.11:6060") so stats survive
# across different books even though the exact URLs differ.
_SERVER_STATS = {}  # host -> {"successes": int, "consecutive_failures": int, "bytes": int}
_SKIP_THRESHOLD = 3  # skip server after this many consecutive failures


def _host_of(url):
    """Extract 'host:port' from a URL for stable stats keying."""
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        return p.netloc or url
    except Exception:
        return url


def _record_server(url, bytes_downloaded, success):
    host = _host_of(url)
    entry = _SERVER_STATS.setdefault(host, {"successes": 0, "consecutive_failures": 0, "bytes": 0})
    if success and bytes_downloaded >= 10000:
        entry["successes"] += 1
        entry["consecutive_failures"] = 0
        entry["bytes"] += bytes_downloaded
    else:
        entry["consecutive_failures"] += 1


def _should_skip(url):
    host = _host_of(url)
    entry = _SERVER_STATS.get(host)
    if not entry:
        return False
    return entry["consecutive_failures"] >= _SKIP_THRESHOLD


def _format_stats():
    if not _SERVER_STATS:
        return ""
    parts = []
    for host, e in _SERVER_STATS.items():
        parts.append(f"{host}: {e['successes']}ok/{e['consecutive_failures']}fail "
                     f"({e['bytes'] // 1024}KB)")
    return " | ".join(parts)


def _do_download_parallel(context, urls, md5, download_dir, book_title="", author="",
                          probe_seconds=PROBE_SECONDS):
    """Race downloads across multiple servers; keep the fastest.

    Flow:
      1. Start one thread per URL, each writing to its own temp file.
      2. After probe_seconds, compare bytes downloaded.
      3. Cancel losers, let the winner finish.
      4. If the winner already completed during the probe, use its file directly;
         otherwise resume via _do_download.
    """
    log.info(f"  racing {len(urls)} servers ({probe_seconds}s probe)...")
    if _SERVER_STATS:
        log.info(f"  server health: {_format_stats()}")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/131.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Referer": f"{BASE_URL}/",
    }
    cookies = [(c["name"], c["value"], c.get("domain", "")) for c in context.cookies()]

    tmp_paths = [download_dir / f"{md5}.part{i}" for i in range(len(urls))]
    cancel_events = [threading.Event() for _ in urls]
    results = [{} for _ in urls]

    def _make_session():
        s = requests.Session()
        s.verify = False
        s.trust_env = False
        for name, value, domain in cookies:
            s.cookies.set(name, value, domain=domain)
        s.headers.update(headers)
        return s

    def worker(idx, url, tmp_path, cancel_event, result_holder):
        session = _make_session()
        size = 0
        try:
            resp = session.get(url, timeout=(30, 900), stream=True)
            resp.raise_for_status()
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(65536):
                    if cancel_event.is_set():
                        resp.close()
                        result_holder["status"] = "cancelled"
                        result_holder["size"] = size
                        return
                    if chunk:
                        f.write(chunk)
                        size += len(chunk)
            result_holder["status"] = "done"
            result_holder["size"] = size
        except Exception as e:
            result_holder["status"] = "error"
            result_holder["error"] = str(e)
            result_holder["size"] = size

    threads = []
    skipped = []
    active_indices = []
    for i, url in enumerate(urls):
        if _should_skip(url):
            skipped.append(i)
            results[i]["status"] = "skipped"
            results[i]["size"] = 0
            continue
        active_indices.append(i)
        t = threading.Thread(target=worker,
                             args=(i, url, tmp_paths[i], cancel_events[i], results[i]),
                             daemon=True)
        t.start()
        threads.append(t)

    if skipped:
        log.info(f"  skipping servers {skipped} (consecutive failures >= {_SKIP_THRESHOLD})")

    # If all were skipped, reset and try again (stats may be stale)
    if not active_indices:
        log.warning(f"  all servers skipped; resetting stats and retrying all")
        for url in urls:
            host = _host_of(url)
            if host in _SERVER_STATS:
                _SERVER_STATS[host]["consecutive_failures"] = 0
        for i, url in enumerate(urls):
            results[i] = {}
            t = threading.Thread(target=worker,
                                 args=(i, url, tmp_paths[i], cancel_events[i], results[i]),
                                 daemon=True)
            t.start()
            threads.append(t)
        active_indices = list(range(len(urls)))

    time.sleep(probe_seconds)

    sizes = []
    for path in tmp_paths:
        try:
            sizes.append(path.stat().st_size)
        except Exception:
            sizes.append(0)

    best_idx = max(range(len(sizes)), key=lambda i: sizes[i])
    best_size = sizes[best_idx]
    speed_kb = best_size / probe_seconds / 1024 if probe_seconds > 0 else 0

    size_report = ", ".join(f"#{i}={s // 1024}KB" for i, s in enumerate(sizes))
    log.info(f"  probe: [{size_report}] -> winner #{best_idx} @ {speed_kb:.1f} KB/s")

    # Record health stats for each server based on probe outcome.
    for i, url in enumerate(urls):
        if results[i].get("status") == "skipped":
            continue
        _record_server(url, sizes[i], success=(sizes[i] >= 10000))

    for i, ev in enumerate(cancel_events):
        if i != best_idx:
            ev.set()

    threads[best_idx].join()

    for i, path in enumerate(tmp_paths):
        if i != best_idx:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    winner_path = tmp_paths[best_idx]
    winner_status = results[best_idx].get("status", "error")
    winner_size = winner_path.stat().st_size if winner_path.exists() else 0

    if winner_status == "error" or winner_size < 10000:
        log.error(f"  race winner failed: {results[best_idx]}")
        winner_path.unlink(missing_ok=True)
        return False

    if winner_status == "done":
        file_path = _fix_extension(winner_path)
        if book_title:
            renamed = rename_to_title(download_dir, md5, book_title, author)
            if renamed:
                file_path = renamed
        size_mb = winner_size / (1024 * 1024)
        log.info(f"  [OK] {file_path.name} ({size_mb:.1f} MB, race)")
        return True

    # Winner was cancelled mid-download. Move partial to the canonical
    # md5.pdf path so _do_download can resume via Range header.
    canonical = download_dir / f"{md5}.pdf"
    if winner_path != canonical:
        if canonical.exists():
            canonical.unlink()
        winner_path.rename(canonical)

    log.info(f"  resuming from winner #{best_idx} ({winner_size / (1024*1024):.1f} MB)")
    return _do_download(context, urls[best_idx], md5, download_dir, book_title, author)


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


def _fix_mojibake(text):
    """Try to recover text that was decoded with the wrong encoding.

    Common case on Anna's Archive: Chinese titles encoded in GBK on the page,
    but interpreted by the browser as Latin-1/cp1252, producing garbage chars
    like `綯ѧ` or `¾­µç`. We try multiple (wrong, right) encoding pairs and
    pick the result with the best "text quality" score.
    """
    if not text:
        return text

    pairs = [
        ("cp1252", "gbk"),
        ("latin-1", "gbk"),
        ("cp1252", "gb2312"),
        ("latin-1", "gb2312"),
        ("cp1252", "utf-8"),
        ("latin-1", "utf-8"),
        ("cp1252", "big5"),
    ]

    best, best_score = text, _text_quality(text)
    for wrong, right in pairs:
        try:
            recovered = text.encode(wrong).decode(right)
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
        score = _text_quality(recovered)
        if score > best_score:
            best, best_score = recovered, score
    return best


def _text_quality(text):
    """Higher = better. Penalizes replacement chars and Latin-1 garbage,
    rewards ASCII and (if present) CJK."""
    if not text:
        return -1
    score = 0
    for c in text:
        cp = ord(c)
        if c == "�":
            score -= 10
        elif 0x80 <= cp <= 0xFF:
            score -= 3  # Latin-1 extended — usually garbage in titles
        elif cp < 0x80:
            score += 1  # ASCII
        elif 0x4E00 <= cp <= 0x9FFF:
            score += 2  # CJK Unified Ideographs
        elif 0x3000 <= cp <= 0x303F or 0xFF00 <= cp <= 0xFFEF:
            score += 1  # CJK punctuation / fullwidth
    return score


def _looks_garbled(text):
    """Heuristic: does the text look like mojibake / encoding garbage?

    Signals of garbling:
      - Replacement chars (U+FFFD)
      - Latin-1 extended chars (0x80-0xFF) - usually mojibake residue
      - Cyrillic chars in a title that's otherwise ASCII/CJK - mixing 3 scripts
        is almost always an encoding accident, not a real bilingual title
      - High ratio of "unusual" chars to total length
    """
    if not text:
        return True
    garbage = 0
    scripts_seen = set()
    for c in text:
        cp = ord(c)
        if cp == 0xFFFD:
            garbage += 2
        elif 0x80 <= cp <= 0xFF:
            garbage += 1
        elif cp < 0x80:
            scripts_seen.add("ascii")
        elif 0x0400 <= cp <= 0x04FF:
            garbage += 1
            scripts_seen.add("cyrillic")
        elif 0x4E00 <= cp <= 0x9FFF:
            scripts_seen.add("cjk")
        elif 0x3000 <= cp <= 0x303F or 0xFF00 <= cp <= 0xFFEF:
            scripts_seen.add("cjk_punct")
    # Multi-script mixing (ascii + cjk + cyrillic etc.) is a strong garble signal
    main_scripts = {s for s in scripts_seen if s not in ("cjk_punct",)}
    if len(main_scripts) >= 3:
        return True
    return garbage / len(text) > 0.05

def _clean_book_title(title):
    """Clean a book title; return cleaned string or None if unrecoverable."""
    if not title:
        return None
    recovered = _fix_mojibake(title)
    if not _looks_garbled(recovered):
        return recovered.strip()
    # Still garbled: fall back to ASCII-only skeleton, then normalize.
    ascii_only = "".join(c for c in title if ord(c) < 0x80)
    # Collapse whitespace, strip leading/trailing punctuation residue from mojibake
    ascii_only = re.sub(r"\s+", " ", ascii_only).strip(" \t\n\r=-_.,:;")
    # Drop empty parentheses like "()" or "(  )" left by dropped non-ASCII
    ascii_only = re.sub(r"\(\s*\)", "", ascii_only)
    # Strip whitespace before closing parens (leftover from dropped non-ASCII)
    ascii_only = re.sub(r"\s+\)", ")", ascii_only)
    # Strip again after removing empty parens
    ascii_only = ascii_only.strip(" \t\n\r=-_.,:;")
    if len(ascii_only) >= 5:
        return ascii_only
    return None


def rename_to_title(download_dir, md5, book_title, author):
    """Rename md5.ext to 'Book Title - Author.ext'.

    If the title is mojibake / unrecoverable, falls back to author-only
    or keeps the md5 name.
    """
    download_dir = Path(download_dir)
    for ext in VALID_EXTS:
        src = download_dir / f"{md5}{ext}"
        if src.exists():
            clean_title = _clean_book_title(book_title)
            clean_author = _clean_book_title(author) if author else None

            if clean_title and clean_author:
                display = f"{clean_title} - {clean_author}"
            elif clean_title:
                display = clean_title
            elif clean_author:
                display = clean_author
            else:
                log.warning(f"  title/author garbled, keeping md5 name: {src.name}")
                return src

            safe_name = re.sub(r'[<>:"/\\|?*]', "_", display).strip(" .")
            dst = download_dir / f"{safe_name}{ext}"
            if dst.exists() and dst != src:
                dst = download_dir / f"{safe_name}_{md5[:8]}{ext}"
            if src != dst:
                src.rename(dst)
                log.info(f"  renamed: {src.name} -> {dst.name}")
            return dst
    return None
