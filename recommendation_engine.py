"""
recommendation_engine.py

Deterministic rule engine for the SEO + AEO Website Intelligence Agent (v2).
Replaces the legacy `insights.py`. Converts explainable SEO/AEO audit issues
into deduplicated, business-facing recommendations grouped into short-term,
medium-term, and long-term horizons.

No LLM calls, no scoring — pure rule matching over `SEOResult.issues` /
`AEOResult.issues` text.
"""

from dataclasses import dataclass

from models import AEOResult, RecommendationItem, RecommendationResult, SEOResult


@dataclass(frozen=True)
class _Rule:
    """A single deterministic mapping from issue-text patterns to a recommendation."""

    key: str
    title: str
    description_template: str
    rationale: str
    patterns: tuple[str, ...]


_SHORT_TERM_RULES: tuple[_Rule, ...] = (
    _Rule(
        key="missing_title",
        title="Add missing page titles",
        description_template="{count} page(s) are missing a <title> tag.",
        rationale="Title tags are one of the strongest on-page ranking and "
                   "click-through-rate signals; leaving them blank forfeits "
                   "guaranteed, low-effort SEO value.",
        patterns=("missing title",),
    ),
    _Rule(
        key="missing_meta_description",
        title="Write missing meta descriptions",
        description_template="{count} page(s) have no meta description.",
        rationale="Meta descriptions drive search-result click-through rate "
                   "and are one of the fastest wins available on a site.",
        patterns=("missing meta description",),
    ),
    _Rule(
        key="duplicate_titles",
        title="Resolve duplicate page titles",
        description_template="{count} page(s) share a duplicate title with another page.",
        rationale="Duplicate titles confuse search engines about which page "
                   "to rank and dilute ranking signal across pages.",
        patterns=("duplicate title",),
    ),
    _Rule(
        key="missing_h1",
        title="Add missing H1 headings",
        description_template="{count} page(s) have no H1 heading.",
        rationale="The H1 is the primary on-page relevance signal for both "
                   "search engines and answer engines; it is quick to add.",
        patterns=("missing h1",),
    ),
    _Rule(
        key="missing_alt_text",
        title="Add missing image ALT text",
        description_template="{count} page(s) have images missing ALT attributes.",
        rationale="Missing ALT text harms accessibility, image search "
                   "visibility, and is a near-zero-effort fix.",
        patterns=("missing alt", "alt text"),
    ),
    _Rule(
        key="missing_canonical",
        title="Add missing canonical tags",
        description_template="{count} page(s) are missing a canonical tag.",
        rationale="Without a canonical tag, search engines may index "
                   "duplicate or parameterized URLs instead of the "
                   "intended page.",
        patterns=("missing canonical",),
    ),
    _Rule(
        key="broken_internal_links",
        title="Fix broken internal links",
        description_template="{count} page(s) contain broken internal links.",
        rationale="Broken internal links waste crawl budget, break user "
                   "journeys, and dilute link equity across the site.",
        patterns=("broken internal link", "broken link"),
    ),
    _Rule(
        key="missing_robots_txt",
        title="Publish a robots.txt file",
        description_template="{count} page(s) are affected by a missing robots.txt file.",
        rationale="Without robots.txt, crawlers cannot be reliably guided "
                   "toward the sitemap or away from low-value paths.",
        patterns=("robots.txt",),
    ),
    _Rule(
        key="missing_sitemap",
        title="Publish an XML sitemap",
        description_template="{count} page(s) are affected by a missing or unreachable sitemap.",
        rationale="A sitemap is the fastest way to guarantee full, timely "
                   "crawl and indexation coverage.",
        patterns=("missing sitemap", "sitemap"),
    ),
)

_MEDIUM_TERM_RULES: tuple[_Rule, ...] = (
    _Rule(
        key="missing_faq_section",
        title="Add FAQ sections to key pages",
        description_template="{count} page(s) have no FAQ section.",
        rationale="FAQ content is directly extractable by answer engines "
                   "and search features, making it high-leverage for AEO.",
        patterns=("missing faq", "no faq", "faq section"),
    ),
    _Rule(
        key="missing_structured_schema",
        title="Implement structured data (schema.org)",
        description_template="{count} page(s) have no structured schema markup.",
        rationale="Structured data is the primary machine-readable signal "
                   "AI answer engines use to understand and cite a page.",
        patterns=("missing structured schema", "structured schema", "schema markup", "missing schema"),
    ),
    _Rule(
        key="weak_internal_linking",
        title="Strengthen internal linking",
        description_template="{count} page(s) show weak internal linking.",
        rationale="Stronger internal linking distributes authority and "
                   "helps both crawlers and users reach priority content.",
        patterns=("weak internal linking", "low internal linking", "internal linking"),
    ),
    _Rule(
        key="missing_keyword_modifiers",
        title="Add keyword modifiers to target more queries",
        description_template="{count} page(s) lack keyword modifiers (e.g. \"best\", \"vs\", \"how to\").",
        rationale="Modifier-rich phrasing captures a wider set of "
                   "long-tail and comparison-style queries.",
        patterns=("keyword modifier",),
    ),
    _Rule(
        key="missing_comparison_format",
        title="Add comparison / review / \"best of\" formats",
        description_template="{count} page(s) lack comparison, review, or \"best of\" style content.",
        rationale="Comparison and review formats consistently perform "
                   "well in both organic search and AI-generated answers.",
        patterns=("comparison", "review format", "best format", "best-of"),
    ),
    _Rule(
        key="weak_heading_hierarchy",
        title="Fix weak heading hierarchy",
        description_template="{count} page(s) have a weak or inconsistent heading hierarchy.",
        rationale="A clean H1-H6 hierarchy helps both search engines and "
                   "answer engines parse page structure and topic flow.",
        patterns=("weak heading", "heading hierarchy", "heading structure"),
    ),
)

_LONG_TERM_RULES: tuple[_Rule, ...] = (
    _Rule(
        key="weak_entity_clarity",
        title="Improve entity clarity",
        description_template="{count} page(s) show weak entity clarity.",
        rationale="Clear, unambiguous entities (brand, product, people, "
                   "places) improve how confidently AI systems can cite "
                   "and attribute the content.",
        patterns=("entity clarity", "weak entity"),
    ),
    _Rule(
        key="thin_topical_authority",
        title="Build topical authority",
        description_template="{count} page(s) show thin topical authority.",
        rationale="Depth and breadth of coverage on a topic compounds "
                   "over time and is difficult for competitors to replicate quickly.",
        patterns=("topical authority", "thin content", "topic depth"),
    ),
    _Rule(
        key="low_trustworthiness",
        title="Strengthen trust signals",
        description_template="{count} page(s) show low trustworthiness signals.",
        rationale="Trust signals (authorship, citations, credentials) are "
                   "increasingly weighted by both search and answer engines.",
        patterns=("trustworthiness", "low trust", "e-e-a-t", "eat score"),
    ),
    _Rule(
        key="poor_freshness",
        title="Establish a content refresh cadence",
        description_template="{count} page(s) show outdated content or poor freshness signals.",
        rationale="Regularly updated content is favored by both ranking "
                   "algorithms and answer engines that prioritize recency.",
        patterns=("outdated content", "freshness", "stale content"),
    ),
    _Rule(
        key="lack_of_shareable_visuals",
        title="Invest in shareable visual assets",
        description_template="{count} page(s) lack shareable visual content.",
        rationale="Original visuals (charts, diagrams, branded imagery) "
                   "earn backlinks and social shares that compound authority over time.",
        patterns=("shareable visual", "shareable image", "visual content"),
    ),
    _Rule(
        key="brand_consistency",
        title="Improve brand consistency",
        description_template="{count} page(s) show inconsistent brand presentation.",
        rationale="Consistent brand naming and voice improves entity "
                   "recognition by search engines and answer engines alike.",
        patterns=("brand consistency", "brand voice", "inconsistent brand"),
    ),
)


class RecommendationEngine:
    """Deterministic rule engine that turns audit issues into a 3-tier roadmap."""

    def generate_recommendations(
        self,
        seo_results: dict[str, SEOResult],
        aeo_results: dict[str, AEOResult],
    ) -> RecommendationResult:
        """Build a deduplicated short/medium/long-term recommendation set."""
        merged_issues = self._merge_issues(seo_results, aeo_results)
        total_pages = len(merged_issues)

        return RecommendationResult(
            short_term=self._build_short_term(merged_issues),
            medium_term=self._build_medium_term(merged_issues, total_pages),
            long_term=self._build_long_term(merged_issues),
        )

    # ------------------------------------------------------------------
    # Tier builders
    # ------------------------------------------------------------------
    def _build_short_term(
        self, merged_issues: dict[str, list[str]]
    ) -> list[RecommendationItem]:
        """High-impact, low-effort fixes: priority is always High, effort Low."""
        items: list[RecommendationItem] = []
        for rule in _SHORT_TERM_RULES:
            urls = self._collect_urls(merged_issues, rule.patterns)
            if not urls:
                continue
            items.append(
                RecommendationItem(
                    title=rule.title,
                    description=rule.description_template.format(count=len(urls)),
                    priority="High",
                    impact="High",
                    effort="Low",
                    rationale=rule.rationale,
                    affected_urls=urls,
                )
            )
        return items

    def _build_medium_term(
        self, merged_issues: dict[str, list[str]], total_pages: int
    ) -> list[RecommendationItem]:
        """Structural/content improvements: priority scales with how many pages are affected."""
        items: list[RecommendationItem] = []
        for rule in _MEDIUM_TERM_RULES:
            urls = self._collect_urls(merged_issues, rule.patterns)
            if not urls:
                continue
            priority = self._frequency_priority(len(urls), total_pages)
            items.append(
                RecommendationItem(
                    title=rule.title,
                    description=rule.description_template.format(count=len(urls)),
                    priority=priority,
                    impact="Medium",
                    effort="Medium",
                    rationale=rule.rationale,
                    affected_urls=urls,
                )
            )
        return items

    def _build_long_term(
        self, merged_issues: dict[str, list[str]]
    ) -> list[RecommendationItem]:
        """Strategic, compounding improvements: priority Medium, effort High."""
        items: list[RecommendationItem] = []
        for rule in _LONG_TERM_RULES:
            urls = self._collect_urls(merged_issues, rule.patterns)
            if not urls:
                continue
            items.append(
                RecommendationItem(
                    title=rule.title,
                    description=rule.description_template.format(count=len(urls)),
                    priority="Medium",
                    impact="Medium",
                    effort="High",
                    rationale=rule.rationale,
                    affected_urls=urls,
                )
            )
        return items

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------
    def _merge_issues(
        self,
        seo_results: dict[str, SEOResult],
        aeo_results: dict[str, AEOResult],
    ) -> dict[str, list[str]]:
        """Merge SEO + AEO issue lists per URL into one lookup table."""
        merged: dict[str, list[str]] = {}
        for url, result in seo_results.items():
            merged.setdefault(url, []).extend(result.issues)
        for url, result in aeo_results.items():
            merged.setdefault(url, []).extend(result.issues)
        return merged

    def _collect_urls(
        self, merged_issues: dict[str, list[str]], patterns: tuple[str, ...]
    ) -> list[str]:
        """Return the deduplicated, sorted list of URLs whose issues match any pattern."""
        matched: set[str] = set()
        for url, issues in merged_issues.items():
            haystack = " ".join(issue.lower() for issue in issues)
            if any(pattern in haystack for pattern in patterns):
                matched.add(url)
        return sorted(matched)

    def _frequency_priority(self, affected_count: int, total_pages: int) -> str:
        """Escalate medium-term priority to High once an issue affects most of the site."""
        if total_pages <= 0:
            return "Medium"
        ratio = affected_count / total_pages
        return "High" if ratio >= 0.5 else "Medium"