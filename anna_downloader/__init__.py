"""Anna's Archive Book Downloader - Automated slow-channel download tool."""

__version__ = "0.1.0"

from .chrome import launch_chrome, connect_cdp, get_page
from .downloader import (
    download_book, search_books, search_books_detailed,
    pass_ddos_guard, _fix_extension, BASE_URL,
)
from .batch import BatchDownloader, parse_book_list

__all__ = [
    "launch_chrome",
    "connect_cdp",
    "get_page",
    "download_book",
    "search_books",
    "search_books_detailed",
    "pass_ddos_guard",
    "BatchDownloader",
    "parse_book_list",
]
