"""
seo.py

Rule-based SEO scoring engine for the SEO + AEO Website Intelligence Agent.

Input:
    ``CrawlResult`` (pages as ``PageData`` plus the robots and sitemap reports).

Output:
    ``(dict[url, SEOResult], site_seo_score)``

The engine is 100% deterministic. It uses no LLM, no Streamlit, no AEO logic
and no recommendation logic. Every deducted point is backed by an evidence entry.

Scoring model (100 marks, 10 per factor):
    Title (10)             exists 4 + length 50-60 chars 3 + unique across site 3
    Meta Description (10)  exists 5 + length 120-160 chars 5
    H1 Hierarchy (10)      exactly one H1 (5) + logical H2 structure (5)
    Internal Linking (10)  minimum 3 internal links, proportional below that
    Canonical (10)         canonical link present and non-empty
    Shareable Images (10)  page contains at least one meaningful image (ALT ignored)
    ALT Text (10)          deduction proportional to the share of images missing ALT
    Sitemap (10)           full marks when ``sitemap_report.exists``
    Robots.txt (10)        exists 4 + sitemap declared 3 + no search-engine bot blocked 3
    Content Quality (10)   word count 4 + heading structure 3 + paragraph richness 3

Sitemap and Robots.txt are site-level factors: they are evaluated once and apply
to every page, so each page is scored out of the full 100 marks. Their evidence
is attached to every page result with that page's URL as the affected URL.

Pages that do not return HTTP 200 cannot be evaluated on-page. They receive 0 of
the 80 on-page marks with a single evidence entry, and still receive the
site-level Sitemap and Robots.txt marks.

Grades (page scores): 85-100 A, 70-84 B, 55-69 C, below 55 D. The specification
lists "D" for both the 40-54 and below-40 bands, so D is the lowest grade.

Assumed contracts (read defensively; attributes and dict keys are both supported
and the first name found wins):
    * ``crawler.CrawlResult`` exposes ``pages``, ``robots_report`` and
      ``sitemap_report``.
    * ``models.PageData`` exposes: ``url``, ``status_code``, ``title``,
      ``meta_description``, headings (``headings`` / ``heading_structure`` mapping
      of ``h1``..``h6`` to a count or list, or ``h1_count``), ``internal_links``
      (count or list), ``canonical``, ``images`` (items with ``src`` / ``alt`` /
      optional ``width`` / ``height``, or the counts ``images_total`` and
      ``images_missing_alt``), ``word_count`` and ``paragraph_count``
      (or ``paragraphs``). When no paragraph data exists, paragraph richness
      is not measurable and is not penalised.
    * ``models.SEOResult`` is constructible as
      ``SEOResult(url, score, grade, issues, evidence)`` where ``issues`` is a
      ``list[str]`` of issue titles and ``evidence`` is a ``list[dict]`` whose
      entries carry ``factor``, ``issue``, ``evidence``, ``affected_urls`` and
      ``points_deducted``.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from crawler import CrawlResult, RobotsReport, SitemapReport
from models import PageData, SEOResult

logger: Final[logging.Logger] = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Scoring configuration
# ----------------------------------------------------------------------
FACTOR_MAX_POINTS: Final[float] = 10.0
ON_PAGE_FACTOR_COUNT: Final[int] = 8

TITLE_MIN_LENGTH: Final[int] = 50
TITLE_MAX_LENGTH: Final[int] = 60
TITLE_EXISTS_POINTS: Final[float] = 4.0
TITLE_LENGTH_POINTS: Final[float] = 3.0
TITLE_UNIQUE_POINTS: Final[float] = 3.0

META_MIN_LENGTH: Final[int] = 120
META_MAX_LENGTH: Final[int] = 160
META_EXISTS_POINTS: Final[float] = 5.0
META_LENGTH_POINTS: Final[float] = 5.0

H1_POINTS: Final[float] = 5.0
H2_STRUCTURE_POINTS: Final[float] = 5.0

MIN_INTERNAL_LINKS: Final[int] = 3

MIN_MEANINGFUL_DIMENSION: Final[int] = 50
MAX_ALT_EXAMPLES: Final[int] = 3

CONTENT_MIN_WORDS: Final[int] = 300
CONTENT_WORD_POINTS: Final[float] = 4.0
CONTENT_HEADING_POINTS: Final[float] = 3.0
CONTENT_PARAGRAPH_POINTS: Final[float] = 3.0
MIN_SUBHEADINGS_LONG_PAGE: Final[int] = 2
MIN_SUBHEADINGS_SHORT_PAGE: Final[int] = 1
MIN_PARAGRAPHS: Final[int] = 3
MIN_AVG_PARAGRAPH_WORDS: Final[int] = 20

ROBOTS_EXISTS_POINTS: Final[float] = 4.0
ROBOTS_SITEMAP_POINTS: Final[float] = 3.0
ROBOTS_BOTS_POINTS: Final[float] = 3.0

GRADE_THRESHOLDS: Final[tuple[tuple[float, str], ...]] = (
    (85.0, "A"),
    (70.0, "B"),
    (50.0, "C"),
)
LOWEST_GRADE: Final[str] = "D"

# General-purpose search engine crawlers whose blocking is considered unnecessary.
_SEARCH_ENGINE_BOTS: Final[frozenset[str]] = frozenset(
    {
        "*",
        "googlebot",
        "bingbot",
        "slurp",
        "duckduckbot",
        "baiduspider",
        "yandexbot",
        "yandex",
    }
)
_NON_MEANINGFUL_IMAGE_MARKERS: Final[tuple[str, ...]] = (
    "pixel",
    "spacer",
    "sprite",
    "1x1",
    "tracking",
    "beacon",
    "blank.gif",
)

# ----------------------------------------------------------------------
# PageData attribute lookups (data access only)
# ----------------------------------------------------------------------
_URL_ATTRS: Final[tuple[str, ...]] = ("url",)
_STATUS_ATTRS: Final[tuple[str, ...]] = ("status_code", "status")
_TITLE_ATTRS: Final[tuple[str, ...]] = ("title",)
_META_ATTRS: Final[tuple[str, ...]] = ("meta_description",)
_HEADING_ATTRS: Final[tuple[str, ...]] = ("headings", "heading_structure", "heading_hierarchy")
_H1_COUNT_ATTRS: Final[tuple[str, ...]] = ("h1_count",)
_INTERNAL_LINK_ATTRS: Final[tuple[str, ...]] = ("internal_links", "internal_link_count")
_CANONICAL_ATTRS: Final[tuple[str, ...]] = ("canonical", "canonical_url")
_IMAGES_ATTRS: Final[tuple[str, ...]] = ("images",)
_IMAGE_TOTAL_ATTRS: Final[tuple[str, ...]] = ("images_total", "image_count", "total_images")
_IMAGE_MISSING_ALT_ATTRS: Final[tuple[str, ...]] = (
    "images_missing_alt",
    "missing_alt_count",
    "images_without_alt",
)
_WORD_COUNT_ATTRS: Final[tuple[str, ...]] = ("word_count",)
_PARAGRAPH_ATTRS: Final[tuple[str, ...]] = ("paragraph_count", "paragraphs")
_IMAGE_SRC_ATTRS: Final[tuple[str, ...]] = ("src", "url", "source")
_IMAGE_ALT_ATTRS: Final[tuple[str, ...]] = ("alt", "alt_text")
_IMAGE_WIDTH_ATTRS: Final[tuple[str, ...]] = ("width",)
_IMAGE_HEIGHT_ATTRS: Final[tuple[str, ...]] = ("height",)
_HEADING_LEVEL_ATTRS: Final[tuple[str, ...]] = ("level", "tag", "name")

_HEADING_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^h?([1-6])$", re.IGNORECASE)
_WHITESPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+")


class SEOFactor(StrEnum):
    """The ten scored SEO factors (10 marks each)."""

    TITLE = "Title"
    META_DESCRIPTION = "Meta Description"
    H1_HIERARCHY = "H1 Hierarchy"
    INTERNAL_LINKING = "Internal Linking"
    CANONICAL = "Canonical"
    SHAREABLE_IMAGES = "Shareable Images"
    ALT_TEXT = "ALT Text"
    SITEMAP = "Sitemap"
    ROBOTS = "Robots.txt"
    CONTENT_QUALITY = "Content Quality"


@dataclass(frozen=True, slots=True)
class _Finding:
    """One deduction together with the evidence that justifies it."""

    factor: str
    issue: str
    evidence: str
    points_deducted: float


@dataclass(frozen=True, slots=True)
class _FactorOutcome:
    """Marks awarded for one factor and the findings behind any deduction."""

    awarded: float
    findings: tuple[_Finding, ...]


@dataclass(frozen=True, slots=True)
class _ImageStats:
    """Image counts extracted from a page."""

    total: int
    missing_alt: int
    meaningful: int
    missing_alt_examples: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _SiteContext:
    """Site-wide data shared by every page evaluation."""

    title_counts: Counter[str]
    site_outcomes: tuple[_FactorOutcome, ...]


class SEOEngine:
    """Deterministic, rule-based SEO scoring engine.

    The engine scores every crawled page out of 100 using ten equally weighted
    factors, records evidence for every deduction, and averages the page scores
    into a site-level SEO score.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def audit_site(self, crawl_result: CrawlResult) -> tuple[dict[str, SEOResult], float]:
        """Score every page of a crawl and compute the site-level SEO score.

        Args:
            crawl_result: Output of the crawler (pages, robots report, sitemap
                report).

        Returns:
            A tuple of ``(results, site_score)``. ``results`` maps each page URL
            to its ``SEOResult``. ``site_score`` is the average of all page
            scores, rounded to one decimal (``0.0`` when there are no pages).
        """
        pages = list(crawl_result.pages)
        context = self._build_context(pages, crawl_result)

        results: dict[str, SEOResult] = {}
        scores: dict[str, float] = {}
        for page in pages:
            url = self._page_url(page)
            score, findings = self._evaluate_page(page, context)
            results[url] = self._build_result(url, score, findings)
            scores[url] = score

        site_score = self._average(list(scores.values()))
        logger.info("SEO audit complete: %d page(s), site score %.1f", len(results), site_score)
        return results, site_score

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------
    def _build_context(self, pages: Sequence[PageData], crawl_result: CrawlResult) -> _SiteContext:
        """Compute site-wide data: title frequencies and site-level factor outcomes."""
        title_counts: Counter[str] = Counter()
        for page in pages:
            if self._status_code(page) != 200:
                continue
            title = self._text(self._first(page, _TITLE_ATTRS))
            if title:
                title_counts[self._normalise_title(title)] += 1

        site_outcomes = (
            self._score_sitemap(crawl_result.sitemap_report),
            self._score_robots(crawl_result.robots_report),
        )
        return _SiteContext(title_counts=title_counts, site_outcomes=site_outcomes)

    def _evaluate_page(
        self, page: PageData, context: _SiteContext
    ) -> tuple[float, list[_Finding]]:
        """Score one page; return its 0-100 score and all deduction findings."""
        status = self._status_code(page)
        if status == 200:
            outcomes = self._score_on_page_factors(page, context)
        else:
            outcomes = [self._unreachable_outcome(status)]
        outcomes.extend(context.site_outcomes)

        raw_score = sum(outcome.awarded for outcome in outcomes)
        score = round(max(0.0, min(100.0, raw_score)), 1)
        findings = [finding for outcome in outcomes for finding in outcome.findings]
        return score, findings

    def _score_on_page_factors(self, page: PageData, context: _SiteContext) -> list[_FactorOutcome]:
        """Evaluate the eight page-level factors for a reachable page."""
        heading_counts = self._heading_counts(page)
        images = self._image_stats(page)
        return [
            self._score_title(page, context),
            self._score_meta_description(page),
            self._score_h1_hierarchy(heading_counts),
            self._score_internal_linking(page),
            self._score_canonical(page),
            self._score_shareable_images(images),
            self._score_alt_text(images),
            self._score_content_quality(page, heading_counts),
        ]

    def _build_result(self, url: str, score: float, findings: Sequence[_Finding]) -> SEOResult:
        """Assemble the SEOResult with issues and evidence for a page."""
        issues = list(dict.fromkeys(finding.issue for finding in findings))
        evidence: list[dict[str, Any]] = [
            {
                "factor": finding.factor,
                        "issue": finding.issue,
                        "evidence": finding.evidence,
                        "affected_urls": [url],
                        "points_deducted": finding.points_deducted,
                    }
                    for finding in findings
        ]    
        # Calculate individual factor scores
        factor_scores = {
            "Title": 10.0,
            "Meta Description": 10.0,
            "H1 Hierarchy": 10.0,
            "Internal Linking": 10.0,
            "Canonical": 10.0,
            "ALT Text": 10.0,
            "Sitemap": 10.0,
            "Robots.txt": 10.0,
            "Content Quality": 10.0,
            "Shareable Images": 10.0,
        }
        for f in findings:
            if f.factor in factor_scores:
                factor_scores[f.factor] = max(
                    0.0,
                    factor_scores[f.factor] - f.points_deducted,
                      )
        
        return SEOResult(
            score=score,
            grade=self._grade(score),
            title_score=factor_scores["Title"],
            meta_score=factor_scores["Meta Description"],
            h1_score=factor_scores["H1 Hierarchy"],
            internal_link_score=factor_scores["Internal Linking"],
            canonical_score=factor_scores["Canonical"],
            alt_score=factor_scores["ALT Text"],
            sitemap_score=factor_scores["Sitemap"],
            robots_score=factor_scores["Robots.txt"],
            content_quality_score=factor_scores["Content Quality"],
            shareable_images_score=factor_scores["Shareable Images"],
            issues=issues,
            evidence=evidence,
        )
                        
                
        
        
    



    # ------------------------------------------------------------------
    # Factor 1: Title
    # ------------------------------------------------------------------
    def _score_title(self, page: PageData, context: _SiteContext) -> _FactorOutcome:
        """Title: exists (4), 50-60 characters (3), unique across the site (3)."""
        factor = SEOFactor.TITLE.value
        title = self._text(self._first(page, _TITLE_ATTRS))
        if not title:
            return self._outcome(
                [_Finding(factor, "Missing title", "Title tag not found or empty", FACTOR_MAX_POINTS)]
            )

        findings: list[_Finding] = []
        length = len(title)
        if length < TITLE_MIN_LENGTH:
            findings.append(
                _Finding(
                    factor,
                    "Title too short",
                    f"Title is {length} characters (required {TITLE_MIN_LENGTH}-{TITLE_MAX_LENGTH}): {title}",
                    TITLE_LENGTH_POINTS,
                )
            )
        elif length > TITLE_MAX_LENGTH:
            findings.append(
                _Finding(
                    factor,
                    "Title too long",
                    f"Title is {length} characters (required {TITLE_MIN_LENGTH}-{TITLE_MAX_LENGTH}): {title}",
                    TITLE_LENGTH_POINTS,
                )
            )

        occurrences = context.title_counts[self._normalise_title(title)]
        if occurrences > 1:
            findings.append(
                _Finding(
                    factor,
                    "Duplicate title",
                    f"The title \"{title}\" is used on {occurrences} pages",
                    TITLE_UNIQUE_POINTS,
                )
            )
        return self._outcome(findings)

    # ------------------------------------------------------------------
    # Factor 2: Meta description
    # ------------------------------------------------------------------
    def _score_meta_description(self, page: PageData) -> _FactorOutcome:
        """Meta description: exists (5), 120-160 characters (5)."""
        factor = SEOFactor.META_DESCRIPTION.value
        description = self._text(self._first(page, _META_ATTRS))
        if not description:
            return self._outcome(
                [
                    _Finding(
                        factor,
                        "Missing meta description",
                        "Meta description tag not found or empty",
                        FACTOR_MAX_POINTS,
                    )
                ]
            )

        length = len(description)
        if length < META_MIN_LENGTH:
            issue = "Meta description too short"
        elif length > META_MAX_LENGTH:
            issue = "Meta description too long"
        else:
            return self._outcome([])
        return self._outcome(
            [
                _Finding(
                    factor,
                    issue,
                    f"Meta description is {length} characters (required {META_MIN_LENGTH}-{META_MAX_LENGTH})",
                    META_LENGTH_POINTS,
                )
            ]
        )

    # ------------------------------------------------------------------
    # Factor 3: H1 hierarchy
    # ------------------------------------------------------------------
    def _score_h1_hierarchy(self, counts: Mapping[int, int]) -> _FactorOutcome:
        """H1 hierarchy: exactly one H1 (5), logical H2 structure (5).

        The H2 structure is logical when at least one H2 exists and no heading
        level is skipped (for example, an H4 used without any H3).
        """
        factor = SEOFactor.H1_HIERARCHY.value
        findings: list[_Finding] = []

        h1_count = counts[1]
        if h1_count == 0:
            findings.append(
                _Finding(factor, "Missing H1", "No H1 heading found on the page", H1_POINTS)
            )
        elif h1_count > 1:
            findings.append(
                _Finding(
                    factor,
                    "Multiple H1 headings",
                    f"{h1_count} H1 headings found; exactly one is required",
                    H1_POINTS,
                )
            )

        problems: list[str] = []
        if counts[2] == 0:
            problems.append("no H2 headings found")
        for level in range(3, 7):
            if counts[level] and not counts[level - 1]:
                problems.append(f"H{level} used without H{level - 1}")
        if problems:
            summary = "; ".join(problems)
            findings.append(
                _Finding(
                    factor,
                    "Illogical H2 heading structure",
                    f"{summary[0].upper()}{summary[1:]}",
                    H2_STRUCTURE_POINTS,
                )
            )
        return self._outcome(findings)

    # ------------------------------------------------------------------
    # Factor 4: Internal linking
    # ------------------------------------------------------------------
    def _score_internal_linking(self, page: PageData) -> _FactorOutcome:
        """Internal linking: minimum three links, marks proportional below that."""
        count = self._count(page, _INTERNAL_LINK_ATTRS)
        if count >= MIN_INTERNAL_LINKS:
            return self._outcome([])
        shortfall_ratio = 1.0 - count / MIN_INTERNAL_LINKS
        return self._outcome(
            [
                _Finding(
                    SEOFactor.INTERNAL_LINKING.value,
                    "Insufficient internal linking",
                    f"{count} internal link(s) found (minimum {MIN_INTERNAL_LINKS})",
                    round(FACTOR_MAX_POINTS * shortfall_ratio, 2),
                )
            ]
        )

    # ------------------------------------------------------------------
    # Factor 5: Canonical
    # ------------------------------------------------------------------
    def _score_canonical(self, page: PageData) -> _FactorOutcome:
        """Canonical: the tag must exist and its href must be non-empty."""
        canonical = self._text(self._first(page, _CANONICAL_ATTRS))
        if canonical:
            return self._outcome([])
        return self._outcome(
            [
                _Finding(
                    SEOFactor.CANONICAL.value,
                    "Missing canonical",
                    "Canonical tag not found or its href is empty",
                    FACTOR_MAX_POINTS,
                )
            ]
        )

    # ------------------------------------------------------------------
    # Factor 6: Shareable images
    # ------------------------------------------------------------------
    def _score_shareable_images(self, images: _ImageStats) -> _FactorOutcome:
        """Shareable images: the page must contain at least one meaningful image.

        Meaningful images have a real source and are not inline data URIs,
        tracking pixels, spacers, sprites, or smaller than 50px in a known
        dimension. ALT text is deliberately not considered here.
        """
        if images.meaningful > 0:
            return self._outcome([])
        if images.total == 0:
            evidence = "No images found on the page"
        else:
            evidence = (
                f"{images.total} image(s) found, but none are meaningful "
                "(tracking pixels, sprites, inline placeholders, or tiny icons)"
            )
        return self._outcome(
            [
                _Finding(
                    SEOFactor.SHAREABLE_IMAGES.value,
                    "No meaningful images",
                    evidence,
                    FACTOR_MAX_POINTS,
                )
            ]
        )

    # ------------------------------------------------------------------
    # Factor 7: ALT text
    # ------------------------------------------------------------------
    def _score_alt_text(self, images: _ImageStats) -> _FactorOutcome:
        """ALT text: deduction is proportional to the share of images missing ALT."""
        if images.total == 0 or images.missing_alt == 0:
            return self._outcome([])

        ratio = images.missing_alt / images.total
        evidence = (
            f"{images.missing_alt} of {images.total} images are missing ALT text "
            f"({ratio:.0%})"
        )
        if images.missing_alt_examples:
            examples = ", ".join(images.missing_alt_examples[:MAX_ALT_EXAMPLES])
            evidence = f"{evidence}. Examples: {examples}"
        return self._outcome(
            [
                _Finding(
                    SEOFactor.ALT_TEXT.value,
                    "Images missing ALT text",
                    evidence,
                    round(FACTOR_MAX_POINTS * ratio, 2),
                )
            ]
        )

    # ------------------------------------------------------------------
    # Factor 8: Sitemap (site-level)
    # ------------------------------------------------------------------
    def _score_sitemap(self, report: SitemapReport) -> _FactorOutcome:
        """Sitemap: full marks when a sitemap exists."""
        if report.exists:
            return self._outcome([])
        return self._outcome(
            [
                _Finding(
                    SEOFactor.SITEMAP.value,
                    "Missing sitemap",
                    "No sitemap containing URLs was found at /sitemap.xml or via robots.txt",
                    FACTOR_MAX_POINTS,
                )
            ]
        )

    # ------------------------------------------------------------------
    # Factor 9: Robots.txt (site-level)
    # ------------------------------------------------------------------
    def _score_robots(self, report: RobotsReport) -> _FactorOutcome:
        """Robots.txt: exists (4), sitemap declared (3), no search bot blocked (3)."""
        factor = SEOFactor.ROBOTS.value
        findings: list[_Finding] = []

        if not report.exists:
            findings.append(
                _Finding(
                    factor,
                    "Missing robots.txt",
                    "robots.txt was not served with HTTP 200 at /robots.txt",
                    ROBOTS_EXISTS_POINTS,
                )
            )
        if not report.sitemap_found:
            reason = (
                "robots.txt contains no Sitemap directive"
                if report.exists
                else "robots.txt is unavailable, so no Sitemap directive is declared"
            )
            findings.append(
                _Finding(factor, "Sitemap not declared in robots.txt", reason, ROBOTS_SITEMAP_POINTS)
            )

        blocked = [
            bot for bot in report.blocked_bots if bot.strip().lower() in _SEARCH_ENGINE_BOTS
        ]
        if blocked:
            findings.append(
                _Finding(
                    factor,
                    "Search engine bots blocked",
                    "robots.txt blocks the entire site for: " + ", ".join(blocked),
                    ROBOTS_BOTS_POINTS,
                )
            )
        return self._outcome(findings)

    # ------------------------------------------------------------------
    # Factor 10: Content quality
    # ------------------------------------------------------------------
    def _score_content_quality(
        self, page: PageData, counts: Mapping[int, int]
    ) -> _FactorOutcome:
        """Content quality: word count (4), heading structure (3), paragraph richness (3)."""
        factor = SEOFactor.CONTENT_QUALITY.value
        findings: list[_Finding] = []
        word_count = self._count(page, _WORD_COUNT_ATTRS)

        if word_count < CONTENT_MIN_WORDS:
            ratio = word_count / CONTENT_MIN_WORDS
            findings.append(
                _Finding(
                    factor,
                    "Thin content",
                    f"{word_count} words found (minimum {CONTENT_MIN_WORDS})",
                    round(CONTENT_WORD_POINTS * (1.0 - ratio), 2),
                )
            )

        subheadings = sum(counts[level] for level in range(2, 7))
        required = (
            MIN_SUBHEADINGS_LONG_PAGE
            if word_count >= CONTENT_MIN_WORDS
            else MIN_SUBHEADINGS_SHORT_PAGE
        )
        if subheadings < required:
            findings.append(
                _Finding(
                    factor,
                    "Weak content heading structure",
                    f"{subheadings} sub-heading(s) (H2-H6) found; at least {required} expected "
                    f"for a page of {word_count} words",
                    round(CONTENT_HEADING_POINTS * (1.0 - subheadings / required), 2),
                )
            )

        paragraph_finding = self._paragraph_finding(page, word_count)
        if paragraph_finding is not None:
            findings.append(paragraph_finding)
        return self._outcome(findings)

    def _paragraph_finding(self, page: PageData, word_count: int) -> _Finding | None:
        """Return a paragraph-richness finding, or None when rich or not measurable."""
        paragraphs = self._count_of(self._first(page, _PARAGRAPH_ATTRS))
        if paragraphs is None:
            return None

        average = word_count / paragraphs if paragraphs else 0.0
        count_ratio = min(1.0, paragraphs / MIN_PARAGRAPHS)
        length_ratio = min(1.0, average / MIN_AVG_PARAGRAPH_WORDS)
        richness = (count_ratio + length_ratio) / 2.0
        deduction = round(CONTENT_PARAGRAPH_POINTS * (1.0 - richness), 2)
        if deduction <= 0:
            return None
        return _Finding(
            SEOFactor.CONTENT_QUALITY.value,
            "Low paragraph richness",
            f"{paragraphs} paragraph(s) averaging {average:.1f} words "
            f"(expected at least {MIN_PARAGRAPHS} paragraphs averaging "
            f"{MIN_AVG_PARAGRAPH_WORDS} words)",
            deduction,
        )

    # ------------------------------------------------------------------
    # Unreachable pages
    # ------------------------------------------------------------------
    @staticmethod
    def _unreachable_outcome(status: int) -> _FactorOutcome:
        """Outcome for a page that did not return HTTP 200: all on-page marks lost."""
        detail = f"HTTP status {status}" if status else "Request failed (no HTTP response)"
        finding = _Finding(
            factor="On-page factors",
            issue="Page not reachable",
            evidence=(
                f"{detail}; Title, Meta Description, H1 Hierarchy, Internal Linking, "
                "Canonical, Shareable Images, ALT Text and Content Quality could not be evaluated"
            ),
            points_deducted=FACTOR_MAX_POINTS * ON_PAGE_FACTOR_COUNT,
        )
        return _FactorOutcome(awarded=0.0, findings=(finding,))

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------
    def _heading_counts(self, page: PageData) -> dict[int, int]:
        """Return the number of headings for each level H1-H6."""
        counts = {level: 0 for level in range(1, 7)}
        raw = self._first(page, _HEADING_ATTRS)

        if isinstance(raw, Mapping):
            for key, value in raw.items():
                level = self._heading_level(key)
                if level is not None:
                    counts[level] = self._count_of(value) or 0
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            for item in raw:
                level = self._heading_level(self._first(item, _HEADING_LEVEL_ATTRS) or item)
                if level is not None:
                    counts[level] += 1

        if counts[1] == 0:
            counts[1] = self._count(page, _H1_COUNT_ATTRS)
        return counts

    @staticmethod
    def _heading_level(key: Any) -> int | None:
        """Convert ``1``, ``"h1"`` or ``"H1"`` to a heading level (1-6)."""
        if isinstance(key, bool):
            return None
        if isinstance(key, int):
            return key if 1 <= key <= 6 else None
        if isinstance(key, str):
            match = _HEADING_KEY_PATTERN.match(key.strip())
            return int(match.group(1)) if match else None
        return None

    def _image_stats(self, page: PageData) -> _ImageStats:
        """Extract image totals, missing-ALT counts, and meaningful-image counts."""
        raw = self._first(page, _IMAGES_ATTRS)
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            return self._stats_from_items(raw)

        total = self._count(page, _IMAGE_TOTAL_ATTRS)
        missing = min(total, self._count(page, _IMAGE_MISSING_ALT_ATTRS))
        return _ImageStats(
            total=total, missing_alt=missing, meaningful=total, missing_alt_examples=()
        )

    def _stats_from_items(self, items: Sequence[Any]) -> _ImageStats:
        """Compute image statistics from per-image records."""
        total = 0
        missing = 0
        meaningful = 0
        examples: list[str] = []

        for item in items:
            total += 1
            if isinstance(item, str):
                source = item.strip()
                alt_known = False
                has_alt = False
                width: int | None = None
                height: int | None = None
            else:
                source = self._text(self._first(item, _IMAGE_SRC_ATTRS))
                alt_known = True
                has_alt = bool(self._text(self._first(item, _IMAGE_ALT_ATTRS)))
                width = self._to_int(self._first(item, _IMAGE_WIDTH_ATTRS))
                height = self._to_int(self._first(item, _IMAGE_HEIGHT_ATTRS))

            if alt_known and not has_alt:
                missing += 1
                examples.append(source or "(image without src)")
            if self._is_meaningful_image(source, width, height):
                meaningful += 1

        return _ImageStats(
            total=total,
            missing_alt=missing,
            meaningful=meaningful,
            missing_alt_examples=tuple(examples[:MAX_ALT_EXAMPLES]),
        )

    @staticmethod
    def _is_meaningful_image(source: str, width: int | None, height: int | None) -> bool:
        """True if an image is a real content image rather than a pixel or placeholder."""
        if not source:
            return False
        lowered = source.lower()
        if lowered.startswith("data:"):
            return False
        if any(marker in lowered for marker in _NON_MEANINGFUL_IMAGE_MARKERS):
            return False
        return not any(
            dimension is not None and 0 < dimension < MIN_MEANINGFUL_DIMENSION
            for dimension in (width, height)
        )

    # ------------------------------------------------------------------
    # Generic helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _outcome(findings: Sequence[_Finding]) -> _FactorOutcome:
        """Build a factor outcome; marks awarded are 10 minus the deductions."""
        deducted = sum(finding.points_deducted for finding in findings)
        awarded = max(0.0, FACTOR_MAX_POINTS - deducted)
        return _FactorOutcome(awarded=round(awarded, 2), findings=tuple(findings))

    @staticmethod
    def _grade(score: float) -> str:
        """Map a 0-100 page score to its letter grade."""
        for threshold, grade in GRADE_THRESHOLDS:
            if score >= threshold:
                return grade
        return LOWEST_GRADE

    @staticmethod
    def _average(scores: Sequence[float]) -> float:
        """Average of the scores rounded to one decimal; 0.0 when empty."""
        if not scores:
            return 0.0
        return round(sum(scores) / len(scores), 1)

    @staticmethod
    def _normalise_title(title: str) -> str:
        """Case-fold a title and collapse whitespace for duplicate detection."""
        return _WHITESPACE_PATTERN.sub(" ", title).strip().casefold()

    def _page_url(self, page: PageData) -> str:
        """Return the page URL as text."""
        return self._text(self._first(page, _URL_ATTRS))

    def _status_code(self, page: PageData) -> int:
        """Return the HTTP status code, or 0 when unavailable."""
        return self._to_int(self._first(page, _STATUS_ATTRS)) or 0

    @staticmethod
    def _get(source: Any, name: str) -> Any:
        """Read ``name`` from a mapping key or object attribute; None if absent."""
        if source is None:
            return None
        if isinstance(source, Mapping):
            return source.get(name)
        return getattr(source, name, None)

    def _first(self, source: Any, names: Sequence[str]) -> Any:
        """Return the first non-None value among ``names`` on ``source``."""
        for name in names:
            value = self._get(source, name)
            if value is not None:
                return value
        return None

    @staticmethod
    def _text(value: Any) -> str:
        """Render a value as stripped text (empty string for None)."""
        if value is None:
            return ""
        return value.strip() if isinstance(value, str) else str(value).strip()

    @staticmethod
    def _to_int(value: Any) -> int | None:
        """Convert a numeric value or numeric string to int; None if not numeric."""
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            try:
                return int(float(value.strip()))
            except ValueError:
                return None
        return None

    def _count_of(self, value: Any) -> int | None:
        """Interpret a value as a count: a number, or the length of a collection."""
        if value is None:
            return None
        number = self._to_int(value)
        if number is not None:
            return max(0, number)
        if isinstance(value, (Mapping, Sequence, set, frozenset)) and not isinstance(
            value, (str, bytes)
        ):
            return len(value)
        return None

    def _count(self, source: Any, names: Sequence[str]) -> int:
        """Return the first available count among ``names`` (0 when absent)."""
        return self._count_of(self._first(source, names)) or 0

# Compatibility wrapper for app.py

def audit_site(crawled_pages):
    """
    Returns the structure expected by app.py
    """
    engine = SEOEngine()
    results, overall_score = engine.audit_site(crawled_pages)

    page_audits = []
    all_issues = []

    for url, result in results.items():
        page_audits.append({
            "url": url,
            "score": result.score,
            "grade": result.grade,
            "issue_count": len(result.issues),
            "issues": result.issues,
            "evidence": result.evidence,
        })

        for e in result.evidence:
            all_issues.append({
                "url": url,
                "severity": "High",
                "issue": e["issue"],
                "detail": e["evidence"],
            })

    return {
        "overall_score": overall_score,
        "page_audits": page_audits,
        "all_issues": all_issues,
        "top_problem_pages": sorted(
            page_audits,
            key=lambda x: x["score"]
        )[:10],
        "indexed_pages": len(page_audits),
        "total_pages": len(page_audits),
        "critical_issues": sum(
            1 for i in all_issues if i["severity"] == "Critical"
        ),
    }