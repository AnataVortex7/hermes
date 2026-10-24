"""Low-RAM Playwright example for a 512MB Koyeb container.
Run:  python3 /app/examples/browser_example.py https://example.com
"""
import sys
from playwright.sync_api import sync_playwright

ARGS = ["--no-sandbox", "--disable-setuid-sandbox",
        "--disable-dev-shm-usage", "--disable-gpu"]
BLOCK = {"image", "font", "media"}

def fetch(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=ARGS)
        try:
            ctx = browser.new_context()
            ctx.route("**/*", lambda r: r.abort()
                      if r.request.resource_type in BLOCK else r.continue_())
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return page.content()
        finally:
            browser.close()          # always free RAM

if __name__ == "__main__":
    print(fetch(sys.argv[1] if len(sys.argv) > 1 else "https://example.com")[:500])
