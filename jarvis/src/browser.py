"""Playwright-backed browser tools for the Jarvis voice agent.

The BrowserToolset gives the agent full control over a single Chromium
browser instance: navigation, inspection, clicking, typing, tabs, and
screenshots. It is a livekit.agents Toolset, so AgentSession calls
setup() when the session starts and aclose() when it ends.
"""

import asyncio
import base64
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from livekit.agents import RunContext, ToolError, function_tool
from livekit.agents.llm import Toolset
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page, async_playwright
from typing_extensions import Self

logger = logging.getLogger("browser")

# Every interactive element the agent can target.
_ELEMENT_SELECTOR = (
    "a[href], button, input, textarea, select, label, "
    '[role], [onclick], [contenteditable="true"]'
)

# Elements the agent can type into.
_FIELD_SELECTOR = (
    "input, textarea, [contenteditable='true'], [role='textbox'], "
    "[role='searchbox'], [role='combobox']"
)

# JavaScript run in the page to enumerate interactive elements as compact
# ref records the LLM can reason about (e.g. "e4 button 'Search'").
# Each element is tagged with a data-jarvis-ref attribute so later tool calls
# address that exact element, instead of relying on list positions that shift
# when hidden elements exist.
_INSPECT_ELEMENTS_JS = """
([selector, fieldSelector]) => {
    document
        .querySelectorAll("[data-jarvis-ref]")
        .forEach((el) => el.removeAttribute("data-jarvis-ref"));

    const elements = Array.from(document.querySelectorAll(selector));
    const out = [];
    let index = 0;
    for (const el of elements) {
        const style = window.getComputedStyle(el);
        if (style.display === "none" || style.visibility === "hidden") {
            continue;
        }
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) {
            continue;
        }
        const ref = "e" + index;
        el.setAttribute("data-jarvis-ref", ref);

        const tag = el.tagName.toLowerCase();
        const role = el.getAttribute("role") || tag;
        let name = (
            el.getAttribute("aria-label") ||
            el.innerText ||
            el.getAttribute("placeholder") ||
            el.getAttribute("alt") ||
            el.getAttribute("name") ||
            el.getAttribute("value") ||
            ""
        ).trim().replace(/\\s+/g, " ").slice(0, 80);
        if (!name && tag === "input") {
            name = "unlabeled " + (el.getAttribute("type") || "input");
        }
        out.push({
            ref,
            role,
            name,
            tag,
            editable: el.matches(fieldSelector),
            value: tag === "input" || tag === "textarea" ? el.value || "" : "",
        });
        index += 1;
    }
    return out;
}
"""

_READABLE_TEXT_JS = """
() => {
    const text = document.body ? document.body.innerText : "";
    return text.split("\\n").map((l) => l.trim()).filter(Boolean).join("\\n");
}
"""

_SEARCH_RESULTS_JS = """
(selector) => {
    const results = [];
    for (const a of document.querySelectorAll(selector)) {
        const container = a.closest(".result, .web-result, body");
        const snippetEl = container ? container.querySelector(".result__snippet") : null;
        results.push({
            title: (a.innerText || "").trim(),
            href: a.getAttribute("href") || "",
            snippet: snippetEl ? (snippetEl.innerText || "").trim() : "",
        });
        if (results.length >= 8) break;
    }
    return results;
}
"""

# DuckDuckGo's main site is a JS app with data-testid attributes.
_DDG_SITE_JS = """
() => Array.from(document.querySelectorAll("article[data-testid='result']"))
    .slice(0, 8)
    .map((article) => {
        const link = article.querySelector("a[data-testid='result-title-a']");
        const snippet = article.querySelector("[data-result='snippet']");
        return {
            title: link ? (link.innerText || "").trim() : "",
            href: link ? link.href : "",
            snippet: snippet ? (snippet.innerText || "").trim() : "",
        };
    })
    .filter((r) => r.title || r.href);
"""

# Fallback engine used when DuckDuckGo blocks the browser.
_BING_JS = """
() => Array.from(document.querySelectorAll("#b_results li.b_algo"))
    .slice(0, 8)
    .map((item) => {
        const link = item.querySelector("h2 a");
        const snippet = item.querySelector(".b_caption p, .b_algoSlug");
        return {
            title: link ? (link.innerText || "").trim() : "",
            href: link ? link.href : "",
            snippet: snippet ? (snippet.innerText || "").trim() : "",
        };
    })
    .filter((r) => r.title || r.href);
"""


def _clip(text: str, limit: int) -> str:
    """Trim text to a character limit, noting truncation."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + " [truncated]"


class _SharedPlaywright:
    """One Playwright driver for the whole process.

    Playwright expects a single driver Node process. Starting one per browser
    crashes its pipe when many sessions run at once (`lk agent simulate` runs
    several jobs concurrently). This shares the driver and stops it when the
    last user releases it. Each toolset still gets its own browser context.
    """

    _lock = asyncio.Lock()
    _pw = None
    _refcount = 0

    @classmethod
    async def acquire(cls):
        async with cls._lock:
            if cls._pw is None:
                cls._pw = await async_playwright().start()
            cls._refcount += 1
            return cls._pw

    @classmethod
    async def release(cls) -> None:
        async with cls._lock:
            if cls._refcount == 0:
                return
            cls._refcount -= 1
            if cls._refcount == 0 and cls._pw is not None:
                try:
                    await cls._pw.stop()
                except Exception:
                    logger.debug("error while stopping playwright", exc_info=True)
                cls._pw = None


class BrowserToolset(Toolset):
    """Full-control Playwright browser tools for a voice agent.

    Uses one persistent Chromium context per session so cookies and logins
    survive across calls. All page actions are serialized with a lock since
    a single page cannot safely run two actions at once.
    """

    def __init__(self) -> None:
        super().__init__(id="browser")

        self._headless = os.environ.get("JARVIS_HEADLESS", "").lower() in (
            "1",
            "true",
            "yes",
        )
        # A Chromium user data dir can only be used by one browser instance at a
        # time. Concurrent agent sessions (for example `lk agent simulate`, which
        # runs several jobs in one process) would otherwise collide with
        # "Opening in existing browser session" and crash on startup. Give each
        # toolset instance its own profile, and let JARVIS_BROWSER_PROFILE
        # override it to keep a persistent login across restarts.
        profile_override = os.environ.get("JARVIS_BROWSER_PROFILE")
        if profile_override:
            self._profile_dir = Path(profile_override)
            self._owns_profile_dir = False
        else:
            self._profile_dir = (
                Path(".browser-profile") / f"session-{uuid.uuid4().hex[:8]}"
            )
            self._owns_profile_dir = True
        self._screenshots_dir = Path("screenshots")

        self._pw = None
        self._context = None
        self._active: Page | None = None
        self._acquired = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def setup(self) -> Self:
        """Prepare the toolset without launching a browser.

        The browser starts lazily, the first time a browser tool is actually
        called. That keeps a window from opening on every call, before the user
        has asked for anything.
        """
        await super().setup()
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._screenshots_dir.mkdir(exist_ok=True)
        return self

    async def _ensure_browser(self) -> Page:
        """Start the browser on first use and return the active page.

        Call this while holding `self._lock`. A startup failure is turned into a
        ToolError the LLM can relay, so a browser problem never takes down the
        voice session.
        """
        if self._context is None:
            self._pw = await _SharedPlaywright.acquire()
            self._acquired = True
            try:
                self._context = await self._pw.chromium.launch_persistent_context(
                    user_data_dir=str(self._profile_dir),
                    headless=self._headless,
                    viewport={"width": 1280, "height": 800},
                )
            except PlaywrightError as exc:
                await self._shutdown_playwright()
                logger.error("could not start the browser: %s", exc)
                raise ToolError(
                    "The browser could not be started, so that is not possible "
                    "right now. Tell the user plainly and offer to help another way."
                ) from exc

            if self._context.pages:
                self._active = self._context.pages[0]
            else:
                self._active = await self._context.new_page()

            logger.info(
                f"browser started (headless={self._headless}, "
                f"profile={self._profile_dir.resolve()})"
            )

        return self._current_page()

    async def _shutdown_playwright(self) -> None:
        """Release the shared driver, leaving the toolset inert."""
        if self._acquired:
            await _SharedPlaywright.release()
            self._acquired = False
        self._pw = None
        self._context = None
        self._active = None

    async def aclose(self) -> None:
        await super().aclose()
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                logger.exception("error while closing the browser")
        self._context = None
        self._active = None
        await self._shutdown_playwright()

        # Remove the throwaway profile we created for this session, so repeated
        # runs do not accumulate directories. A user-supplied profile is kept.
        # Chromium releases its lock files just after the process exits, so retry
        # briefly before giving up.
        if self._owns_profile_dir:
            for attempt in range(5):
                shutil.rmtree(self._profile_dir, ignore_errors=True)
                if not self._profile_dir.exists():
                    break
                await asyncio.sleep(0.2 * (attempt + 1))
            if self._profile_dir.exists():
                logger.debug("could not fully remove profile %s", self._profile_dir)

        logger.info("browser closed")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _current_page(self) -> Page:
        """Return the active page, or raise a ToolError the LLM can act on."""
        if self._context is None:
            raise ToolError("The browser is not running yet, try again in a moment.")

        if self._active is not None and not self._active.is_closed():
            return self._active

        open_pages = [p for p in self._context.pages if not p.is_closed()]
        if not open_pages:
            raise ToolError("No page is open. Use open_url to open one first.")

        self._active = open_pages[-1]
        return self._active

    async def _inspect_elements(self, page: Page) -> list[dict]:
        elements = await page.evaluate(
            _INSPECT_ELEMENTS_JS, [_ELEMENT_SELECTOR, _FIELD_SELECTOR]
        )
        return list(elements)

    async def _resolve_ref(self, page: Page, ref: str) -> Locator:
        """Resolve an inspect_page ref to the exact element it pointed at."""
        ref = ref.strip().lower()
        if not ref.startswith("e") or not ref[1:].isdigit():
            raise ToolError(
                f"'{ref}' is not a valid element ref. Use the refs returned by "
                'inspect_page, such as "e4".'
            )
        locator = page.locator(f'[data-jarvis-ref="{ref}"]')
        if await locator.count() == 0:
            raise ToolError(
                f"Ref {ref} is no longer on the page, it may have changed. Use "
                "inspect_page to refresh the refs before you act on it."
            )
        return locator.first

    @staticmethod
    async def _describe_element(locator: Locator) -> str:
        info = await locator.evaluate(
            """(el) => ({
                role: el.getAttribute("role") || el.tagName.toLowerCase(),
                name: (el.getAttribute("aria-label") || el.innerText || "")
                    .trim().replace(/\\s+/g, " ").slice(0, 60),
            })"""
        )
        return f"{info['role']} '{info['name']}'"

    async def _describe_page(self, page: Page) -> str:
        title = await page.title() if not page.is_closed() else "(closed)"
        return f'"{_clip(title, 80)}" ({page.url})'

    @staticmethod
    def _normalize_url(url: str) -> str:
        url = url.strip()
        if url.startswith(("http://", "https://")):
            return url
        if " " not in url and "." in url:
            return f"https://{url}"
        raise ToolError(
            f"'{url}' does not look like a web address. Provide a full domain "
            "such as example.com, or use search_web to search instead."
        )

    async def _summarize_navigation(self, page: Page, verb: str) -> str:
        """Return a compact post-navigation summary for the LLM."""
        elements = await self._inspect_elements(page)
        description = await self._describe_page(page)
        lines = [f"{verb} {description}"]
        lines.append(f"{len(elements)} interactive elements found.")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    @function_tool()
    async def open_url(self, context: RunContext, url: str) -> str:
        """Open a web address in the browser.

        Args:
            url: The web address to open, such as "https://example.com" or
                "wikipedia.org". The https:// scheme is optional.
        """
        target = self._normalize_url(url)
        async with self._lock:
            page = await self._ensure_browser()
            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=30000)
            except PlaywrightError as e:
                raise ToolError(f"Could not open {target}: {e}") from e
            return await self._summarize_navigation(page, "Opened")

    @function_tool()
    async def inspect_page(self, context: RunContext) -> str:
        """Inspect the current page: its title, URL, interactive elements, and
        readable text.

        Returns element refs like "e0", "e4". Always call this before click or
        type_text to discover the current refs, unless you are acting on refs
        returned by a very recent inspection.
        """
        async with self._lock:
            page = await self._ensure_browser()
            try:
                elements = await self._inspect_elements(page)
                text = await page.evaluate(_READABLE_TEXT_JS)
            except PlaywrightError as e:
                raise ToolError(f"Could not inspect the page: {e}") from e

            lines = [f"Page: {await self._describe_page(page)}", ""]
            lines.append("Interactive elements (use these refs with click/type_text):")
            for el in elements[:120]:
                detail = ""
                if el.get("value"):
                    detail = f' = "{_clip(el["value"], 40)}"'
                marker = " [text field]" if el.get("editable") else ""
                lines.append(
                    f'  {el["ref"]} {el["role"]} "{el["name"]}"{detail}{marker}'
                )
            if len(elements) > 120:
                lines.append(f"  [plus {len(elements) - 120} more elements]")
            lines.append("")
            lines.append("Readable text:")
            lines.append(_clip(text, 4000))
            return "\n".join(lines)

    @function_tool()
    async def click(self, context: RunContext, ref: str) -> str:
        """Click the element with the given ref on the current page.

        Args:
            ref: An element ref such as "e4", from inspect_page. Refs change
                when the page navigates, so inspect again if a click fails.
        """
        async with self._lock:
            page = await self._ensure_browser()
            locator = await self._resolve_ref(page, ref)
            try:
                await locator.click(timeout=10000)
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
            except PlaywrightError as e:
                description = await self._describe_element(locator)
                raise ToolError(f"Could not click {ref} ({description}): {e}") from e
            return await self._summarize_navigation(page, "Clicked")

    @function_tool()
    async def type_text(
        self,
        context: RunContext,
        ref: str,
        text: str,
        press_enter: bool = False,
    ) -> str:
        """Type text into a text field on the current page.

        Args:
            ref: The ref of a text field from inspect_page, shown there as
                "[text field]", such as "e6".
            text: The text to type into the field.
            press_enter: Set to true to press Enter after typing, which submits
                most search and login forms.
        """
        async with self._lock:
            page = await self._ensure_browser()
            locator = await self._resolve_ref(page, ref)
            try:
                is_field = await locator.evaluate(
                    "(el, sel) => el.matches(sel)", _FIELD_SELECTOR
                )
                field = locator
                if not is_field:
                    nested = locator.locator(_FIELD_SELECTOR)
                    if await nested.count() == 0:
                        description = await self._describe_element(locator)
                        raise ToolError(
                            f"Ref {ref} ({description}) is not a text field. Use "
                            "inspect_page and pick a ref marked [text field]."
                        )
                    field = nested.first

                await field.fill(text, timeout=10000)
                if press_enter:
                    await field.press("Enter", timeout=10000)
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
            except PlaywrightError as e:
                description = await self._describe_element(locator)
                raise ToolError(
                    f"Could not type into {ref} ({description}): {e}"
                ) from e

            action = "Typed into and submitted" if press_enter else "Typed into"
            return await self._summarize_navigation(page, action)

    @function_tool()
    async def scroll(
        self,
        context: RunContext,
        direction: str = "down",
        amount: int = 600,
    ) -> str:
        """Scroll the current page down or up to see more content.

        Args:
            direction: Either "down" or "up".
            amount: How many pixels to scroll, between 100 and 3000.
        """
        if direction not in ("down", "up"):
            raise ToolError("direction must be 'down' or 'up'")
        amount = max(100, min(amount, 3000))
        delta = amount if direction == "down" else -amount
        async with self._lock:
            page = await self._ensure_browser()
            try:
                await page.mouse.wheel(0, delta)
                await page.wait_for_timeout(400)
            except PlaywrightError as e:
                raise ToolError(f"Could not scroll: {e}") from e
            return (
                f"Scrolled {direction}. Now viewing {await self._describe_page(page)}."
            )

    @function_tool()
    async def go_back(self, context: RunContext) -> str:
        """Go back to the previously viewed page in this tab's history."""
        async with self._lock:
            page = await self._ensure_browser()
            try:
                await page.go_back(wait_until="domcontentloaded", timeout=15000)
            except PlaywrightError as e:
                raise ToolError(f"Could not go back: {e}") from e
            return await self._summarize_navigation(page, "Went back to")

    @function_tool()
    async def wait_for(self, context: RunContext, seconds: int = 3) -> str:
        """Wait for a few seconds, for example to let a page finish loading
        or an animation complete.

        Args:
            seconds: How long to wait, between 1 and 10 seconds.
        """
        seconds = max(1, min(seconds, 10))
        async with self._lock:
            page = await self._ensure_browser()
            await page.wait_for_timeout(seconds * 1000)
            return (
                f"Waited {seconds} seconds. Currently on "
                f"{await self._describe_page(page)}."
            )

    @function_tool()
    async def switch_tab(self, context: RunContext, index: int = -1) -> str:
        """List open tabs, or switch to one of them.

        Args:
            index: The tab number shown by a previous call to this tool. Omit
                it (or pass -1) to only list the open tabs without switching.
        """
        async with self._lock:
            await self._ensure_browser()

            tabs = [p for p in self._context.pages if not p.is_closed()]
            if index < 0:
                lines = ["Open tabs:"]
                for i, page in enumerate(tabs):
                    marker = " (current)" if page is self._active else ""
                    label = await page.title() or page.url
                    lines.append(f"  tab {i}: {_clip(label, 80)}{marker}")
                return "\n".join(lines) if tabs else "No tabs are open."

            if index >= len(tabs):
                raise ToolError(
                    f"No tab with number {index}. There are {len(tabs)} open tab(s)."
                )

            self._active = tabs[index]
            await self._active.bring_to_front()
            return f"Switched to {await self._describe_page(self._active)}."

    @function_tool()
    async def close_tab(self, context: RunContext, index: int = -1) -> str:
        """Close a browser tab.

        Args:
            index: The tab number to close, from switch_tab. Omit it (or pass
                -1) to close the current tab.
        """
        async with self._lock:
            await self._ensure_browser()

            tabs = [p for p in self._context.pages if not p.is_closed()]
            target = self._active if index < 0 else None
            if target is None:
                if index >= len(tabs):
                    raise ToolError(
                        f"No tab with number {index}. There are {len(tabs)} open tab(s)."
                    )
                target = tabs[index]

            if len(tabs) == 1:
                # Closing the last page shuts down the whole persistent
                # context, so navigate to a blank page instead.
                await target.goto("about:blank")
                self._active = target
                return "Closed the page. The browser is now showing a blank page."

            await target.close()
            if self._active is target:
                remaining = [p for p in self._context.pages if not p.is_closed()]
                self._active = remaining[-1] if remaining else None
            return f"Closed the tab. Now on {await self._describe_page(self._current_page())}."

    @function_tool()
    async def screenshot(
        self, context: RunContext, include_full_page: bool = False
    ) -> str:
        """Take a screenshot of the current page and save it to the
        screenshots folder. Use this when the user asks to capture or save
        what is on screen.

        Args:
            include_full_page: Set to true to capture the entire scrollable
                page, not just the visible area.
        """
        async with self._lock:
            page = await self._ensure_browser()
            try:
                data = await page.screenshot(full_page=include_full_page)
            except PlaywrightError as e:
                raise ToolError(f"Could not take a screenshot: {e}") from e

            path = self._screenshots_dir / f"screenshot-{int(time.time())}.png"
            path.write_bytes(data)
            return f"Saved a screenshot of {await self._describe_page(page)} to {path}."

    @function_tool()
    async def search_web(self, context: RunContext, query: str) -> str:
        """Search the web with DuckDuckGo in the agent-controlled browser.

        Only use this when no specific website is given and a general internet
        lookup is needed. For anything on a named website, open that website
        directly with open_url instead.

        Args:
            query: The search query. For weather requests, include the
                location and the words "current weather".
        """
        async with self._lock:
            page = await self._ensure_browser()

            results, source = await self._search_ddg_html(page, query)
            if not results:
                results, source = await self._search_ddg_site(page, query)
            if not results:
                results, source = await self._search_bing(page, query)

            if not results:
                return (
                    f"No search results were found for '{query}'. Every search "
                    "engine refused the request. The search page is open; use "
                    "inspect_page to read it, or tell the user the search failed."
                )

            lines = [f"{source} results for '{query}':"]
            for i, result in enumerate(results, start=1):
                lines.append(f"{i}. {_clip(result.get('title', ''), 100)}")
                url = self._clean_result_url(result.get("href", ""))
                lines.append(f"   URL: {_clip(url, 150)}")
                snippet = _clip(result.get("snippet", ""), 250)
                if snippet:
                    lines.append(f"   {snippet}")
            lines.append("The results page remains open for further inspection.")
            return "\n".join(lines)

    # ------------------------------------------------------------------
    # Search engines
    # ------------------------------------------------------------------

    async def _search_ddg_html(self, page: Page, query: str) -> tuple[list[dict], str]:
        """Search DuckDuckGo's lightweight HTML endpoint."""
        url = f"https://html.duckduckgo.com/html/?q={query.strip().replace(' ', '+')}"
        try:
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=30000
            )
            if response is not None and response.status >= 400:
                return [], ""
            results = await page.evaluate(_SEARCH_RESULTS_JS, "a.result__a")
        except PlaywrightError as e:
            logger.info(f"DuckDuckGo HTML search failed: {e}")
            return [], ""
        return list(results), "DuckDuckGo"

    async def _search_ddg_site(self, page: Page, query: str) -> tuple[list[dict], str]:
        """Search the DuckDuckGo site the way a person would."""
        try:
            await page.goto(
                "https://duckduckgo.com/", wait_until="domcontentloaded", timeout=30000
            )
            await page.fill("textarea[name=q], input[name=q]", query, timeout=10000)
            await page.keyboard.press("Enter")
            try:
                await page.wait_for_selector(
                    "article[data-testid='result']", timeout=8000
                )
            except PlaywrightError:
                # DuckDuckGo serves a challenge page instead of results when it
                # detects an automated browser. Fall through to the next engine.
                return [], ""
            results = await page.evaluate(_DDG_SITE_JS)
        except PlaywrightError as e:
            logger.info(f"DuckDuckGo site search failed: {e}")
            return [], ""
        return list(results), "DuckDuckGo"

    async def _search_bing(self, page: Page, query: str) -> tuple[list[dict], str]:
        """Fall back to Bing when DuckDuckGo blocks the browser."""
        url = f"https://www.bing.com/search?q={query.strip().replace(' ', '+')}"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_selector("#b_results li.b_algo", timeout=20000)
            results = await page.evaluate(_BING_JS)
        except PlaywrightError as e:
            logger.info(f"Bing search failed: {e}")
            return [], ""
        return list(results), "Bing"

    @staticmethod
    def _clean_result_url(href: str) -> str:
        """Unwrap search-engine redirect links to the real destination."""
        if not href:
            return ""
        parsed = urlparse(href)

        # DuckDuckGo HTML results wrap the target in a ?uddg= parameter.
        redirect = parse_qs(parsed.query).get("uddg", [""])[0]
        if redirect:
            return unquote(redirect)

        # Bing wraps results in /ck/a links with a base64 ?u= parameter.
        if "bing.com" in parsed.netloc and parsed.path.startswith("/ck/a"):
            encoded = parse_qs(parsed.query).get("u", [""])[0]
            if encoded.startswith("a1"):
                padded = encoded[2:] + "=" * (-len(encoded[2:]) % 4)
                try:
                    return base64.urlsafe_b64decode(padded).decode("utf-8")
                except Exception:
                    logger.debug("could not decode Bing redirect url")
        return href
