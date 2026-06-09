"""React onClick gotcha workaround.

Per persistent memory `feedback_playwright_react_onclick.md`: Playwright's
.click() on a `<div onClick={fn}>` does NOT fire the React handler in some
React 18+ builds — the synthetic event system doesn't reliably attach to
the native click. This is a Playwright behavior, NOT a product bug.

Workaround: reach into the React fiber via the
`__reactProps$<hash>` attribute and invoke onClick directly.

This file is part of the CloudWarriors overlay on the upstream
anthropics/skills/webapp-testing skill. See `~/.claude/skills/playwright/`
for the CLI-driven counterpart skill.
"""

from __future__ import annotations

from playwright.sync_api import sync_playwright


def react_click(page, selector: str) -> bool:
    """Click a React onClick-bound element via the fiber, not the synthetic event.

    Returns True on success, False if the element had no React props or
    no onClick attached.
    """
    return page.evaluate(
        """
        (sel) => {
            const el = document.querySelector(sel);
            if (!el) return false;
            const propsKey = Object.keys(el).find(k => k.startsWith('__reactProps$'));
            if (!propsKey) return false;
            const props = el[propsKey];
            if (typeof props.onClick !== 'function') return false;
            props.onClick({ preventDefault: () => {}, stopPropagation: () => {} });
            return true;
        }
        """,
        selector,
    )


if __name__ == "__main__":
    # Example wiring — adapt to your actual server URL and selectors.
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("http://localhost:5173")
        page.wait_for_load_state("networkidle")

        # Try the native click first. If the React handler did not fire
        # (no state mutation visible after the click), fall back to react_click.
        page.click("[data-testid=my-button]")

        # If state did not update, fall back to fiber invocation:
        if not react_click(page, "[data-testid=my-button]"):
            raise RuntimeError("react_click failed — element missing or no onClick")

        browser.close()
