"""
crawler.py

Discovery and extraction layer for the SEO + AEO Website Intelligence Agent.

Pipeline:
    Website URL
        -> robots.txt parsing          (RobotsReport + declared sitemaps)
        -> sitemap discovery           (SitemapReport + per-URL lastmod)
        -> concurrent crawling         (raw HTML, status code, headers)
        -> FeatureEngine.extract_features()
        -> CrawlResult(pages, robots_report, sitemap_report)

Responsibilities:
    The crawler only discovers pages and fetches raw HTML. All extraction of
    on-page signals (title, meta description, H1-H6 hierarchy, canonical, robots
    meta, links, images and ALT text, word count, JSON-LD, Schema.org types, FAQ
    schema, entities, published date, freshness metadata) is performed by
    ``FeatureEngine``. No SEO scoring, no AEO scoring, no LLM calls.

Freshness handling:
    ``FeatureEngine.extract_features`` receives one ``sitemap_last_modified``
    value per page. The sitemap ``<lastmod>`` is used when present; otherwise the
    HTTP ``Last-Modified`` response header is passed instead, so both freshness
    signals reach the engine through the single argument. The published date is
    read from the HTML by the engine.

Assumed contracts:
    * ``feature_engine.FeatureEngine`` can be constructed without arguments and
      exposes ``extract_features(url, html, status_code, sitemap_last_modified)``
      returning ``PageData``. It must be safe to call from multiple threads.
    * ``models.PageData`` is the structured per-page result.

Public API:
    Crawler.crawl(website_url, fallback_urls=None, progress_callback=None)
        -> CrawlResult
    Crawler.crawl_pages(urls, sitemap_last_modified=None, progress_callback=None)
        -> list[PageData]
    crawl_website(website_url, ...) -> CrawlResult
    SAMPLE_SITE_URLS -> demo URL list for use as ``fallback_urls``
"""

from __future__ import annotations

import concurrent.futures as cf
import gzip
import logging
from dataclasses import dataclass, field
from typing import Callable, Final, Sequence
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from feature_engine import FeatureEngine
from models import PageData

logger: Final[logging.Logger] = logging.getLogger(__name__)

USER_AGENT: Final[str] = (
    "Mozilla/5.0 (compatible; SEO-AEO-IntelligenceAgent/1.0; "
    "+https://example.com/bot)"
)
REQUEST_TIMEOUT: Final[int] = 10
HEADERS: Final[dict[str, str]] = {"User-Agent": USER_AGENT}

DEFAULT_MAX_PAGES: Final[int] = 20
DEFAULT_MAX_WORKERS: Final[int] = 8
DEFAULT_MAX_SITEMAP_URLS: Final[int] = 500
MAX_NESTED_SITEMAPS: Final[int] = 5
MAX_FALLBACK_LINKS: Final[int] = 100

ProgressCallback = Callable[[int, int], None]

# Used when no sitemap can be found and the caller wants to see the app work.
SAMPLE_SITE_URLS: Final[list[str]] = [
    "https://www.python.org/",
    "https://www.python.org/about/",
    "https://www.python.org/downloads/",
    "https://www.python.org/doc/",
    "https://www.python.org/community/",
    "https://www.python.org/success-stories/",
    "https://www.python.org/news/",
    "https://www.python.org/events/python-events/",
    "https://www.python.org/psf/",
    "https://www.python.org/jobs/",
]


# ----------------------------------------------------------------------
# Report dataclasses
# ----------------------------------------------------------------------
@dataclass
class RobotsReport:
    """Facts extracted from ``/robots.txt``, consumed later by the executive PPT.

    Attributes:
        exists: True if ``/robots.txt`` was served with HTTP 200.
        sitemap_found: True if robots.txt declares at least one ``Sitemap:`` line.
        disallowed_paths: Unique non-empty ``Disallow`` paths across all groups.
        allowed_bots: User agents declared in robots.txt that are not fully
            blocked (includes ``*`` when declared).
        blocked_bots: User agents declared in robots.txt that are blocked from
            the entire site (``Disallow: /`` with no ``Allow: /``).
    """

    exists: bool = False
    sitemap_found: bool = False
    disallowed_paths: list[str] = field(default_factory=list)
    allowed_bots: list[str] = field(default_factory=list)
    blocked_bots: list[str] = field(default_factory=list)


@dataclass
class SitemapReport:
    """Facts about the discovered sitemap, consumed later by the executive PPT.

    Attributes:
        exists: True if a sitemap containing at least one URL was found.
        total_urls: Unique URLs found in the sitemap(s) before any cap.
        discovered_urls: URLs kept for crawling, capped at the discovery limit.
        last_modified_available: True if any sitemap entry carries ``<lastmod>``.
    """

    exists: bool = False
    total_urls: int = 0
    discovered_urls: list[str] = field(default_factory=list)
    last_modified_available: bool = False


@dataclass
class CrawlResult:
    """Complete output of a crawl.

    Attributes:
        pages: One ``PageData`` per crawled page, in discovery order.
        robots_report: Report built from robots.txt.
        sitemap_report: Report built from sitemap discovery.
    """

    pages: list[PageData]
    robots_report: RobotsReport
    sitemap_report: SitemapReport


@dataclass
class _AgentRules:
    """Allow/Disallow rules collected for one robots.txt user agent."""

    disallow: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)

    @property
    def blocks_entire_site(self) -> bool:
        """True if the agent is disallowed from ``/`` and not re-allowed."""
        return "/" in self.disallow and "/" not in self.allow


# ----------------------------------------------------------------------
# Crawler
# ----------------------------------------------------------------------
class Crawler:
    """Discovers pages, crawls them concurrently, and delegates extraction.

    Args:
        feature_engine: Engine used to turn HTML into ``PageData``. A default
            ``FeatureEngine`` is created when omitted.
        max_pages: Maximum number of pages to crawl.
        max_workers: Number of concurrent fetch threads.
        timeout: Per-request timeout in seconds.
        max_sitemap_urls: Maximum number of sitemap URLs to keep.
    """

    def __init__(
        self,
        feature_engine: FeatureEngine | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_workers: int = DEFAULT_MAX_WORKERS,
        timeout: int = REQUEST_TIMEOUT,
        max_sitemap_urls: int = DEFAULT_MAX_SITEMAP_URLS,
    ) -> None:
        if max_pages < 1:
            raise ValueError("max_pages must be at least 1.")
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1.")
        self._feature_engine = feature_engine or FeatureEngine()
        self._max_pages = max_pages
        self._max_workers = max_workers
        self._timeout = timeout
        self._max_sitemap_urls = max_sitemap_urls

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def crawl(
        self,
        website_url: str,
        fallback_urls: Sequence[str] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> CrawlResult:
        """Run the full discovery and crawl pipeline for a website.

        Args:
            website_url: Site to audit (scheme optional).
            fallback_urls: URLs to crawl when no sitemap is available. When
                omitted, the homepage and its internal links are used.
            progress_callback: Optional ``callback(done, total)`` invoked after
                each page completes.

        Returns:
            A ``CrawlResult`` with ``PageData`` objects and both reports.
        """
        base = self._normalize_base(website_url)

        robots_report, robots_sitemaps = self._fetch_robots(base)
        sitemap_report, last_modified = self._discover_sitemap(base, robots_sitemaps)

        if sitemap_report.exists:
            urls = sitemap_report.discovered_urls
        elif fallback_urls:
            logger.info("No sitemap found for %s; using supplied fallback URLs", base)
            urls = list(fallback_urls)
        else:
            logger.info("No sitemap found for %s; falling back to homepage links", base)
            urls = self._homepage_fallback_urls(base)

        pages = self.crawl_pages(urls, last_modified, progress_callback)
        return CrawlResult(
            pages=pages,
            robots_report=robots_report,
            sitemap_report=sitemap_report,
        )

    def crawl_pages(
        self,
        urls: Sequence[str],
        sitemap_last_modified: dict[str, str | None] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> list[PageData]:
        """Crawl up to ``max_pages`` URLs concurrently and extract features.

        Args:
            urls: URLs to crawl. Duplicates are removed, order is preserved.
            sitemap_last_modified: Optional mapping of URL to sitemap ``lastmod``.
            progress_callback: Optional ``callback(done, total)``.

        Returns:
            ``PageData`` objects in the same order as the (de-duplicated) URLs.
        """
        targets = list(dict.fromkeys(urls))[: self._max_pages]
        total = len(targets)
        lastmods = sitemap_last_modified or {}
        results: list[PageData | None] = [None] * total

        with cf.ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            future_to_index = {
                executor.submit(self._crawl_one, url, lastmods.get(url)): index
                for index, url in enumerate(targets)
            }
            for done, future in enumerate(cf.as_completed(future_to_index), start=1):
                results[future_to_index[future]] = future.result()
                if progress_callback is not None:
                    progress_callback(done, total)

        return [page for page in results if page is not None]

    # ------------------------------------------------------------------
    # robots.txt
    # ------------------------------------------------------------------
    def _fetch_robots(self, base: str) -> tuple[RobotsReport, list[str]]:
        """Fetch and parse robots.txt; return the report and declared sitemaps."""
        try:
            response = _fetch(urljoin(base, "/robots.txt"), self._timeout)
        except requests.RequestException as exc:
            logger.warning("Could not fetch robots.txt for %s: %s", base, exc)
            return RobotsReport(), []

        if response.status_code != 200:
            return RobotsReport(), []

        sitemaps, rules = self._parse_robots(response.text)
        report = RobotsReport(
            exists=True,
            sitemap_found=bool(sitemaps),
            disallowed_paths=self._unique(
                path for agent_rules in rules.values() for path in agent_rules.disallow
            ),
            allowed_bots=[
                agent for agent, agent_rules in rules.items() if not agent_rules.blocks_entire_site
            ],
            blocked_bots=[
                agent for agent, agent_rules in rules.items() if agent_rules.blocks_entire_site
            ],
        )
        return report, sitemaps

    @staticmethod
    def _parse_robots(text: str) -> tuple[list[str], dict[str, _AgentRules]]:
        """Parse robots.txt text into declared sitemaps and per-agent rules.

        Consecutive ``User-agent`` lines form one group; the ``Allow`` and
        ``Disallow`` lines that follow apply to every agent in that group.
        """
        sitemaps: list[str] = []
        rules: dict[str, _AgentRules] = {}
        current_group: list[str] = []
        previous_was_agent = False

        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            directive, value = (part.strip() for part in line.split(":", 1))
            directive = directive.lower()

            if directive == "user-agent":
                if not previous_was_agent:
                    current_group = []
                if value:
                    current_group.append(value)
                    rules.setdefault(value, _AgentRules())
                previous_was_agent = True
            elif directive == "sitemap":
                if value:
                    sitemaps.append(value)
                previous_was_agent = False
            elif directive in ("disallow", "allow"):
                previous_was_agent = False
                if not value:
                    continue
                for agent in current_group:
                    target = rules[agent].disallow if directive == "disallow" else rules[agent].allow
                    target.append(value)
            else:
                previous_was_agent = False

        return sitemaps, rules

    # ------------------------------------------------------------------
    # Sitemap discovery
    # ------------------------------------------------------------------
    def _discover_sitemap(
        self, base: str, robots_sitemaps: Sequence[str]
    ) -> tuple[SitemapReport, dict[str, str | None]]:
        """Locate and parse a sitemap (including sitemap indexes).

        Candidates are ``/sitemap.xml`` followed by any sitemap declared in
        robots.txt. The first candidate that yields URLs is used.

        Returns:
            The ``SitemapReport`` and a mapping of URL to sitemap ``lastmod``.
        """
        candidates = self._unique([urljoin(base, "/sitemap.xml"), *robots_sitemaps])

        for sitemap_url in candidates:
            entries = self._load_sitemap_entries(sitemap_url)
            if not entries:
                continue

            unique_entries = dict(entries)  # later duplicates overwrite earlier ones
            urls = list(unique_entries)
            kept = urls[: self._max_sitemap_urls]
            report = SitemapReport(
                exists=True,
                total_urls=len(urls),
                discovered_urls=kept,
                last_modified_available=any(unique_entries.values()),
            )
            return report, {url: unique_entries[url] for url in kept}

        return SitemapReport(), {}

    def _load_sitemap_entries(self, sitemap_url: str) -> list[tuple[str, str | None]]:
        """Fetch one sitemap and return ``(url, lastmod)`` entries.

        For a sitemap index, the first ``MAX_NESTED_SITEMAPS`` child sitemaps
        are fetched and their entries combined.
        """
        xml_text = self._fetch_sitemap_text(sitemap_url)
        if xml_text is None:
            return []

        entries, nested = self._parse_sitemap_xml(xml_text)
        if entries or not nested:
            return entries

        combined: list[tuple[str, str | None]] = []
        for nested_url in nested[:MAX_NESTED_SITEMAPS]:
            nested_text = self._fetch_sitemap_text(nested_url)
            if nested_text is None:
                continue
            nested_entries, _ = self._parse_sitemap_xml(nested_text)
            combined.extend(nested_entries)
        return combined

    def _fetch_sitemap_text(self, sitemap_url: str) -> str | None:
        """Download a sitemap (plain or gzip) and return its XML text."""
        try:
            response = _fetch(sitemap_url, self._timeout)
        except requests.RequestException as exc:
            logger.debug("Sitemap fetch failed for %s: %s", sitemap_url, exc)
            return None
        if response.status_code != 200 or not response.content:
            return None

        content = response.content
        if sitemap_url.lower().endswith(".gz") or content[:2] == b"\x1f\x8b":
            try:
                content = gzip.decompress(content)
            except OSError:
                logger.debug("Sitemap %s is not valid gzip", sitemap_url)
                return None
            return content.decode("utf-8", errors="replace")
        return response.text

    @staticmethod
    def _parse_sitemap_xml(xml_text: str) -> tuple[list[tuple[str, str | None]], list[str]]:
        """Return ``(page_entries, nested_sitemap_urls)`` from a sitemap document."""
        soup = BeautifulSoup(xml_text, "lxml-xml")

        entries: list[tuple[str, str | None]] = []
        for node in soup.find_all("url"):
            loc = node.find("loc", recursive=False)
            if loc is None or not loc.get_text(strip=True):
                continue
            lastmod = node.find("lastmod", recursive=False)
            lastmod_text = lastmod.get_text(strip=True) if lastmod is not None else ""
            entries.append((loc.get_text(strip=True), lastmod_text or None))

        nested: list[str] = []
        for node in soup.find_all("sitemap"):
            loc = node.find("loc", recursive=False)
            if loc is not None and loc.get_text(strip=True):
                nested.append(loc.get_text(strip=True))
        return entries, nested

    # ------------------------------------------------------------------
    # Fallback discovery
    # ------------------------------------------------------------------
    def _homepage_fallback_urls(self, base: str) -> list[str]:
        """Return the homepage plus internal links found on it.

        Used only when no sitemap exists and no explicit fallback URLs were given.
        """
        homepage = base + "/"
        try:
            response = _fetch(homepage, self._timeout)
        except requests.RequestException as exc:
            logger.warning("Homepage fallback fetch failed for %s: %s", homepage, exc)
            return [homepage]

        if response.status_code != 200 or not self._is_html(response):
            return [homepage]

        domain = urlparse(homepage).netloc
        soup = BeautifulSoup(response.text, "lxml")
        links: list[str] = [homepage]
        for anchor in soup.find_all("a", href=True):
            absolute, _ = urldefrag(urljoin(homepage, str(anchor["href"]).strip()))
            parsed = urlparse(absolute)
            if parsed.scheme not in ("http", "https") or parsed.netloc != domain:
                continue
            links.append(absolute)
            if len(links) >= MAX_FALLBACK_LINKS:
                break
        return self._unique(links)

    # ------------------------------------------------------------------
    # Page crawling and feature extraction
    # ------------------------------------------------------------------
    def _crawl_one(self, url: str, sitemap_last_modified: str | None) -> PageData:
        """Fetch one page and return its ``PageData``.

        Network failures are represented as a page with status code ``0`` and
        empty HTML so every requested URL yields exactly one ``PageData``.
        """
        try:
            response = _fetch(url, self._timeout)
        except requests.RequestException as exc:
            logger.warning("Fetch failed for %s: %s", url, exc)
            return self._extract(url, "", 0, sitemap_last_modified)

        html = response.text if self._is_html(response) else ""
        last_modified = sitemap_last_modified or response.headers.get("Last-Modified")
        return self._extract(url, html, response.status_code, last_modified)

    def _extract(
        self,
        url: str,
        html: str,
        status_code: int,
        last_modified: str | None,
    ) -> PageData:
        """Delegate to ``FeatureEngine``; retry with empty HTML if it fails.

        A single malformed page must not abort the crawl, so an extraction error
        is logged and the page is re-submitted with no HTML.
        """
        try:
            return self._feature_engine.extract_features(
                url=url,
                html=html,
                status_code=status_code,
                sitemap_last_modified=last_modified,
            )
        except Exception:
            logger.exception("Feature extraction failed for %s", url)
            return self._feature_engine.extract_features(
                url=url,
                html="",
                status_code=status_code,
                sitemap_last_modified=last_modified,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_base(url: str) -> str:
        """Return ``scheme://host`` for a URL, defaulting the scheme to https."""
        cleaned = url.strip()
        if not cleaned.startswith(("http://", "https://")):
            cleaned = "https://" + cleaned
        parsed = urlparse(cleaned)
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _is_html(response: requests.Response) -> bool:
        """True if the response declares an HTML content type."""
        return "html" in response.headers.get("Content-Type", "").lower()

    @staticmethod
    def _unique(values: Sequence[str] | object) -> list[str]:
        """Return the values de-duplicated with first-seen order preserved."""
        return list(dict.fromkeys(values))  # type: ignore[arg-type]


def _fetch(url: str, timeout: int = REQUEST_TIMEOUT) -> requests.Response:
    """Perform a GET request with the crawler's User-Agent header."""
    return requests.get(url, headers=HEADERS, timeout=timeout)


def crawl_website(
    website_url: str,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_workers: int = DEFAULT_MAX_WORKERS,
    fallback_urls: Sequence[str] | None = None,
    progress_callback: ProgressCallback | None = None,
    feature_engine: FeatureEngine | None = None,
) -> CrawlResult:
    """Convenience wrapper that builds a ``Crawler`` and runs ``crawl``.

    Args:
        website_url: Site to audit.
        max_pages: Maximum number of pages to crawl.
        max_workers: Number of concurrent fetch threads.
        fallback_urls: URLs to crawl when no sitemap is available.
        progress_callback: Optional ``callback(done, total)``.
        feature_engine: Optional pre-built ``FeatureEngine``.

    Returns:
        The ``CrawlResult`` for the website.
    """
    crawler = Crawler(
        feature_engine=feature_engine,
        max_pages=max_pages,
        max_workers=max_workers,
    )
    return crawler.crawl(
        website_url,
        fallback_urls=fallback_urls,
        progress_callback=progress_callback,
    )
# ----------------------------------------------------------------------
# Compatibility wrappers for app.py
# ----------------------------------------------------------------------

def discover_sitemap_urls(website_url: str):
    """
    Returns:
        (sitemap_url, list_of_urls)
    """
    crawler = Crawler()
    base = crawler._normalize_base(website_url)

    robots_report, robot_sitemaps = crawler._fetch_robots(base)
    sitemap_report, _ = crawler._discover_sitemap(base, robot_sitemaps)

    if sitemap_report.exists:
        sitemap_url = (
            robot_sitemaps[0]
            if robot_sitemaps
            else f"{base}/sitemap.xml"
        )
        return sitemap_url, sitemap_report.discovered_urls

    return None, []


# ----------------------------------------------------------------------
# Compatibility wrappers for app.py
# ----------------------------------------------------------------------

def discover_sitemap_urls(website_url: str):
    """
    Returns:
        (sitemap_url, list_of_urls)
    """
    crawler = Crawler()
    base = crawler._normalize_base(website_url)

    robots_report, robot_sitemaps = crawler._fetch_robots(base)
    sitemap_report, _ = crawler._discover_sitemap(base, robot_sitemaps)

    if sitemap_report.exists:
        sitemap_url = (
            robot_sitemaps[0]
            if robot_sitemaps
            else f"{base}/sitemap.xml"
        )
        return sitemap_url, sitemap_report.discovered_urls

    return None, []


# ----------------------------------------------------------------------
# Compatibility wrappers for app.py
# ----------------------------------------------------------------------

def discover_sitemap_urls(website_url: str):
    """
    Returns:
        (sitemap_url, list_of_urls)
    """
    crawler = Crawler()
    base = crawler._normalize_base(website_url)

    robots_report, robot_sitemaps = crawler._fetch_robots(base)
    sitemap_report, _ = crawler._discover_sitemap(base, robot_sitemaps)

    if sitemap_report.exists:
        sitemap_url = (
            robot_sitemaps[0]
            if robot_sitemaps
            else f"{base}/sitemap.xml"
        )
        return sitemap_url, sitemap_report.discovered_urls

    return None, []


def crawl_pages(urls, max_pages=20, progress_callback=None):
    """
    Wrapper used by app.py
    """
    crawler = Crawler(max_pages=max_pages)
    return crawler.crawl_pages(
        urls,
        progress_callback=progress_callback,
    )