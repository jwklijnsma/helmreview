"""Screenshots of the ansreview / helmreview web UI, driven like a user would.

usage: shoot_review.py URL REPO OTHER_REPO[,OTHER...] OUTDIR
"""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

url, repo, others, out = sys.argv[1], sys.argv[2], sys.argv[3].split(","), Path(sys.argv[4])
out.mkdir(parents=True, exist_ok=True)
BUSY = "/Scanning|Fixing|Previewing|Queued|Running|Waiting/"


def settle(pg, timeout=300_000):
    pg.wait_for_function(f"() => !document.body.innerText.match({BUSY})", timeout=timeout)
    pg.wait_for_timeout(1200)


def no_toasts(pg):
    for x in pg.locator(".toast-x").all():
        try:
            x.click(timeout=1000)
        except Exception:
            pass
    pg.wait_for_timeout(400)


def shot(pg, name, **kw):
    pg.wait_for_timeout(900)
    no_toasts(pg)
    pg.add_style_tag(content=".toasts{display:none!important}")
    pg.screenshot(path=out / f"{name}.png", **kw)
    (out / f"{name}.txt").write_text(pg.inner_text("body"))


with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome")
    pg = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
    pg.goto(url)
    pg.wait_for_timeout(1500)
    pg.get_by_role("button", name=repo).first.click()
    pg.wait_for_timeout(800)

    # 1. scan -> findings
    pg.get_by_role("button", name="Scan", exact=True).click()
    pg.wait_for_timeout(1500)
    settle(pg)
    pg.locator("button:has-text('Findings')").first.click()
    pg.wait_for_timeout(800)
    shot(pg, "findings")

    # 2. model findings only, with the code around one
    pg.get_by_role("button", name="Model", exact=True).click()
    pg.wait_for_timeout(600)
    rows = pg.locator("text=/never fires|Service selects/").all()
    if rows:
        rows[0].click()
        pg.wait_for_timeout(800)
    shot(pg, "finding-detail")
    pg.get_by_role("button", name="All", exact=True).click()

    # 3. preview fixes -> fix report and diff
    pg.get_by_role("button", name="Preview fixes", exact=True).click()
    pg.wait_for_timeout(1500)
    settle(pg)
    pg.locator("button:has-text('Fixes')").last.click()
    pg.wait_for_timeout(800)
    shot(pg, "fixes")
    diff = pg.locator("text=/^Diff ·/").first
    diff.evaluate("e => e.scrollIntoView({block: 'start'})")
    pg.evaluate("window.scrollBy(0, -20)")
    pg.wait_for_timeout(500)
    shot(pg, "fix-diff")

    # 4. jobs panel with work in the queue
    for o in others:
        pg.get_by_role("button", name=o).first.click()
        pg.wait_for_timeout(500)
        pg.get_by_role("button", name="Scan", exact=True).click()
        pg.wait_for_timeout(300)
    pg.get_by_role("button", name="Jobs").first.click()
    pg.wait_for_timeout(700)
    shot(pg, "jobs")
    b.close()
