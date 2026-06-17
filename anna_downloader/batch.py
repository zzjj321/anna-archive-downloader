"""Batch download scheduler: book list parsing, filtering, sorting, retry."""

import logging
import re
import time
import random
from pathlib import Path

from .downloader import search_books_detailed, download_book, BASE_URL

log = logging.getLogger("anna_downloader")

FORMAT_PRIORITY = {"pdf": 4, "epub": 3, "djvu": 2, "mobi": 1, "azw3": 1}
VALID_EXTS = {".pdf", ".djvu", ".epub", ".mobi", ".zip", ".rar"}


def parse_book_list(filepath):
    """Parse book list file. Format: title | author per line (or tab/comma separated).

    Returns list of dicts: [{title, author, query}]
    """
    books = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for sep in ["|", "\t", ","]:
                if sep in line:
                    parts = [p.strip() for p in line.split(sep, 1)]
                    if len(parts) == 2:
                        title, author = parts
                        books.append({
                            "title": title,
                            "author": author,
                            "query": f"{title} {author}",
                        })
                    break
    return books


def filter_results(results, author_query, book_title):
    """Filter: remove solutions manuals, match author and title."""
    filtered = []
    title_words = [w.lower() for w in book_title.split() if len(w) > 2]
    author_words = [w.lower() for w in author_query.split() if len(w) > 1]

    for r in results:
        combined = (r["title"] + " " + r["author"]).lower()

        bad_terms = ["solutions manual", "solution manual", "solution manial",
                     "instructor", "answer book", "solution guide",
                     "(solutions)", "study pack", "mastering", "supplement"]
        if any(bad in combined for bad in bad_terms):
            continue
        if re.search(r'\bsolutions?\b', r["title"].lower()):
            continue

        author_match = sum(1 for w in author_words if w in combined)
        if author_match < 1:
            continue

        title_match = sum(1 for w in title_words if w in combined)
        if title_words and title_match / len(title_words) < 0.4:
            continue

        r["author_match"] = author_match
        r["title_match"] = title_match
        filtered.append(r)

    if not filtered:
        log.warning(f"  no results after filter, using all")
        return results

    log.info(f"  filtered: {len(results)} -> {len(filtered)}")
    return filtered


def pick_best(results):
    """Sort by: author match > title match > PDF > downloads > size."""
    results.sort(key=lambda x: (
        x.get("author_match", 0),
        x.get("title_match", 0),
        FORMAT_PRIORITY.get(x.get("fmt", ""), 0),
        x["downloads"],
        x["size_mb"],
    ), reverse=True)
    best = results[0]
    log.info(f"  best: '{best['title'][:60]}' | {best.get('fmt','?').upper()} | "
             f"{best['size_mb']:.1f}MB | dl={best['downloads']}")
    if len(results) > 1:
        r1 = results[1]
        log.info(f"  2nd: '{r1['title'][:60]}' | {r1.get('fmt','?').upper()} | "
                 f"{r1['size_mb']:.1f}MB | dl={r1['downloads']}")
    return best


def is_already_downloaded(download_dir, book_title, author, prefer_fmt=None):
    """Check if file matching both title and author exists.
    If prefer_fmt is set (e.g. 'pdf'), skip only if preferred format exists.
    """
    download_dir = Path(download_dir)
    author_first = author.split()[0].lower()
    title_words = [w.lower() for w in book_title.split() if len(w) > 3]

    for f in download_dir.iterdir():
        if f.suffix.lower() not in VALID_EXTS:
            continue
        name_lower = f.stem.lower()
        if author_first not in name_lower:
            continue
        title_hits = sum(1 for w in title_words if w in name_lower)
        if title_hits >= max(1, len(title_words) * 0.4):
            if prefer_fmt and f.suffix.lower() != f".{prefer_fmt}":
                log.info(f"  existing {f.name} is {f.suffix}, prefer .{prefer_fmt} -> re-download")
                continue
            return f
    return None


def rename_to_title(download_dir, md5, book_title, author):
    """Rename md5.ext to 'Book Title - Author.ext'."""
    download_dir = Path(download_dir)
    for ext in VALID_EXTS:
        src = download_dir / f"{md5}{ext}"
        if src.exists():
            safe_name = re.sub(r'[<>:"/\\|?*]', "_", f"{book_title} - {author}")
            dst = download_dir / f"{safe_name}{ext}"
            if dst.exists() and dst != src:
                dst = download_dir / f"{safe_name}_{md5[:8]}{ext}"
            if src != dst:
                src.rename(dst)
                log.info(f"  renamed: {src.name} -> {dst.name}")
            return dst
    return None


def cleanup_partial_downloads(download_dir):
    """Remove orphaned md5-named files smaller than 1MB (interrupted downloads)."""
    download_dir = Path(download_dir)
    removed = 0
    for f in download_dir.iterdir():
        if f.stem and len(f.stem) == 32 and all(c in "0123456789abcdef" for c in f.stem):
            size = f.stat().st_size
            if size < 1_000_000:
                log.info(f"  cleanup partial: {f.name} ({size/1024:.0f} KB)")
                f.unlink()
                removed += 1
    if removed:
        log.info(f"  cleaned {removed} partial file(s)")


class BatchDownloader:
    """Batch download manager with retry queue and progress tracking."""

    def __init__(self, page, context, download_dir, prefer_fmt=None):
        self.page = page
        self.context = context
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.prefer_fmt = prefer_fmt
        self.report = []
        self.fail_queue = []

    def process_book(self, book):
        """Process a single book. Returns 'skip', 'ok', or 'fail'."""
        title = book["title"]
        author = book["author"]
        query = book["query"]
        label = f"{author} - {title}"

        existing = is_already_downloaded(self.download_dir, title, author, self.prefer_fmt)
        if existing:
            log.info(f"  EXISTS: {existing.name}, skip")
            self.report.append(f"SKIP: {label} ({existing.name})")
            return "skip"

        try:
            results = search_books_detailed(self.page, query, max_results=30)
        except Exception as e:
            log.error(f"  search failed: {e}")
            self.report.append(f"FAIL: {label} (search: {e})")
            self.fail_queue.append(book)
            return "fail"

        if not results:
            log.warning(f"  no results found")
            self.report.append(f"NOT FOUND: {label}")
            self.fail_queue.append(book)
            return "fail"

        filtered = filter_results(results, author, title)
        best = pick_best(filtered)
        log.info(f"  MD5: {best['md5']}")

        success = download_book(self.page, self.context, best, self.download_dir)
        if success:
            rename_to_title(self.download_dir, best["md5"], title, author)
            self.report.append(f"OK: {label} ({best.get('fmt','?')}, {best['size_mb']:.1f}MB, dl={best['downloads']})")
            return "ok"
        else:
            self.report.append(f"FAIL: {label} (download failed, md5={best['md5']})")
            self.fail_queue.append(book)
            return "fail"

    def run(self, books, max_retries=2):
        """Download all books with retry passes."""
        log.info(f"\nBatch download: {len(books)} books")
        log.info(f"Prefer: {self.prefer_fmt or 'any'}")
        log.info(f"Download dir: {self.download_dir}")

        cleanup_partial_downloads(self.download_dir)

        for i, book in enumerate(books, 1):
            label = f"{book['author']} - {book['title']}"
            log.info(f"\n{'#' * 60}")
            log.info(f"[{i}/{len(books)}] {label}")
            result = self.process_book(book)
            if result != "skip" and i < len(books):
                delay = random.uniform(45, 90)
                log.info(f"  wait {delay:.0f}s...")
                time.sleep(delay)

        retry_num = 0
        while self.fail_queue and retry_num < max_retries:
            retry_num += 1
            retry_list = list(self.fail_queue)
            self.fail_queue.clear()
            log.info(f"\n{'=' * 60}")
            log.info(f"Retry pass {retry_num}: {len(retry_list)} failed book(s)")

            for i, book in enumerate(retry_list, 1):
                label = f"{book['author']} - {book['title']}"
                log.info(f"\n[retry {i}/{len(retry_list)}] {label}")
                self.process_book(book)
                if i < len(retry_list):
                    delay = random.uniform(60, 120)
                    log.info(f"  wait {delay:.0f}s...")
                    time.sleep(delay)

        log.info(f"\n{'=' * 60}")
        log.info("DONE! Summary:")
        for line in self.report:
            log.info(f"  {line}")
        if self.fail_queue:
            log.info(f"Still failed ({len(self.fail_queue)}):")
            for b in self.fail_queue:
                log.info(f"  {b['author']} - {b['title']}")

        return self.report, self.fail_queue

    def save_report(self, output_path=None):
        """Save report to file."""
        if output_path is None:
            output_path = self.download_dir / "download_report.txt"
        output_path = Path(output_path)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("Download Report\n")
            f.write(f"{'=' * 60}\n")
            for line in self.report:
                f.write(f"{line}\n")
            if self.fail_queue:
                f.write(f"\nFailed ({len(self.fail_queue)}):\n")
                for b in self.fail_queue:
                    f.write(f"  {b['author']} - {b['title']}\n")
        log.info(f"Report: {output_path}")
        return output_path
