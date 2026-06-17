"""Chrome CDP launch and connection utilities."""

import os
import subprocess
import sys
import urllib.request


DEFAULT_PORT = 9223
DEFAULT_USER_DATA_DIR = os.path.expanduser("~/.anna-downloader/chrome-profile")


def find_chrome():
    """Try to locate Chrome executable."""
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        "/usr/bin/google-chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return "google-chrome"


def launch_chrome(port=DEFAULT_PORT, user_data_dir=None):
    """Launch Chrome with remote debugging enabled.

    Args:
        port: CDP debugging port (default 9223)
        user_data_dir: Chrome profile directory (default ~/.anna-downloader/chrome-profile)
    """
    if user_data_dir is None:
        user_data_dir = DEFAULT_USER_DATA_DIR
    os.makedirs(user_data_dir, exist_ok=True)

    chrome = find_chrome()
    cmd = [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    subprocess.Popen(cmd, shell=(os.name == "nt"))
    return port


def connect_cdp(port=DEFAULT_PORT):
    """Connect to running Chrome CDP instance.

    Returns:
        (playwright_instance, browser)
    """
    from playwright.sync_api import sync_playwright

    cdp_url = f"http://127.0.0.1:{port}"
    try:
        urllib.request.urlopen(f"{cdp_url}/json/version", timeout=5)
    except Exception:
        raise RuntimeError(
            f"Chrome CDP not running on port {port}. "
            f"Run `launch_chrome({port})` or start Chrome manually with "
            f"--remote-debugging-port={port}"
        )

    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(cdp_url)
    return pw, browser


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
