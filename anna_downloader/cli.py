#!/usr/bin/env python3
"""CLI entry point for anna-archive-downloader."""

import argparse
import logging
import sys
from pathlib import Path

from .chrome import launch_chrome, connect_cdp, get_page
from .downloader import search_books, download_book
from .batch import BatchDownloader, parse_book_list

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("anna_downloader")


def main():
    parser = argparse.ArgumentParser(
        description="Anna's Archive Book Downloader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  anna-download --launch                    # Launch Chrome with CDP
  anna-download -q "Classical Mechanics"    # Search and download one book
  anna-download --batch booklist.txt        # Batch download from book list
  anna-download --batch booklist.txt --prefer pdf --dir ./my_books
        """,
    )
    parser.add_argument("--launch", action="store_true",
                        help="Launch Chrome with CDP debugging (port 9223)")
    parser.add_argument("--port", type=int, default=9223,
                        help="Chrome CDP port (default: 9223)")
    parser.add_argument("-q", "--query", default=None,
                        help="Search query for single book download")
    parser.add_argument("--batch", default=None,
                        help="Path to book list file for batch download")
    parser.add_argument("--dir", default="./downloads",
                        help="Download directory (default: ./downloads)")
    parser.add_argument("--prefer", choices=["pdf", "epub", "djvu", "mobi"],
                        default=None, help="Preferred format")
    parser.add_argument("-n", "--count", type=int, default=1,
                        help="Number of books to download (with -q, default: 1)")
    args = parser.parse_args()

    if args.launch:
        log.info(f"Launching Chrome on port {args.port}...")
        port = launch_chrome(port=args.port)
        log.info(f"Chrome launched on port {port}")
        log.info("Now run the script again without --launch to start downloading.")
        return

    if not args.query and not args.batch:
        parser.print_help()
        return

    try:
        pw, browser = connect_cdp(port=args.port)
    except RuntimeError as e:
        log.error(str(e))
        log.error("Run: anna-download --launch")
        sys.exit(1)

    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = get_page(context)

    try:
        download_dir = Path(args.dir)

        if args.query:
            books = search_books(page, args.query, max_results=10)
            if not books:
                log.error("No results found")
                return
            log.info(f"Found {len(books)} results")
            for i, b in enumerate(books[:args.count], 1):
                log.info(f"\n[{i}/{min(args.count, len(books))}] {b['title'][:70]}")
                download_book(page, context, b, download_dir=download_dir)

        elif args.batch:
            booklist_path = Path(args.batch)
            if not booklist_path.exists():
                log.error(f"Book list not found: {booklist_path}")
                sys.exit(1)

            books = parse_book_list(booklist_path)
            if not books:
                log.error("No books parsed from file")
                sys.exit(1)

            batch = BatchDownloader(page, context, download_dir, prefer_fmt=args.prefer)
            batch.run(books)
            batch.save_report()

    finally:
        browser.close()
        pw.stop()


if __name__ == "__main__":
    main()
