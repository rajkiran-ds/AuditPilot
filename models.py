"""
models.py

Centralized data model layer for the SEO + AEO Website Intelligence Agent (v2).
These dataclasses are the shared contract between the crawler, feature engine,
SEO engine, AEO engine, recommendation engine, PPT agent, email agent, and
chat agent. This module contains no business logic — only typed data
containers.
"""

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class PageData:
    """Represents one crawled webpage and its extracted raw signals."""

    url: str
    status_code: int
    title: str
    meta_description: str
    h1: str
    h2_count: int
    headings: list[str] = field(default_factory=list)
    word_count: int = 0
    content: str = ""
    canonical: str = ""
    robots_meta: str = ""
    internal_links: int = 0
    external_links: int = 0
    total_images: int = 0
    missing_alt_images: int = 0
    schema_types: list[str] = field(default_factory=list)
    faq_count: int = 0
    last_modified: Optional[str] = None
    published_date: Optional[str] = None


@dataclass
class SEOResult:
    """Traditional SEO audit result for a single page."""

    score: float
    grade: str
    title_score: float
    meta_score: float
    h1_score: float
    internal_link_score: float
    canonical_score: float
    alt_score: float
    sitemap_score: float
    robots_score: float
    content_quality_score: float
    shareable_images_score: float
    issues: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


@dataclass
class AEOResult:
    score: float
    grade: str

    # Individual factor scores
    structured_schema_score: float
    entity_clarity_score: float
    faq_score: float
    trustworthiness_score: float
    freshness_score: float
    keyword_modifier_score: float
    citation_readiness_score: float

    # Extra fields used by app.py
    factor_scores: dict[str, float] = field(default_factory=dict)
    factor_details: dict[str, str] = field(default_factory=dict)
    recommended_keywords: list[str] = field(default_factory=list)

    issues: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    

@dataclass
class RecommendationItem:
    """A single actionable recommendation derived from the audit findings."""

    title: str
    description: str
    priority: str
    impact: str
    effort: str
    rationale: str
    severity: int = 5
    suggested_keywords: list[str] = field(default_factory=list)
    affected_urls: list[str] = field(default_factory=list)


@dataclass
class RecommendationResult:
    """Recommendations grouped into a short/medium/long term roadmap."""

    short_term: list[RecommendationItem] = field(default_factory=list)
    medium_term: list[RecommendationItem] = field(default_factory=list)
    long_term: list[RecommendationItem] = field(default_factory=list)

@dataclass
class ReferenceItem:
    """Reference URL and evidence used in the executive PPT."""

    url: str
    title: str
    reason: str

@dataclass
class EmbeddedAsset:
    """Supporting files that can be embedded or referenced in the PPT."""

    name: str
    file_path: str
    asset_type: str


@dataclass
class AuditResult:
    """Complete site-wide audit shared across all modules."""

    website_url: str = ""

    pages: list[PageData] = field(default_factory=list)

    seo_results: dict[str, SEOResult] = field(default_factory=dict)
    aeo_results: dict[str, AEOResult] = field(default_factory=dict)

    recommendations: Optional[RecommendationResult] = None

    references: list[ReferenceItem] = field(default_factory=list)
    embedded_assets: list[EmbeddedAsset] = field(default_factory=list)

    overall_seo_score: float = 0.0
    overall_aeo_score: float = 0.0

    overall_seo_grade: str = ""
    overall_aeo_grade: str = ""

    overall_grade: str = ""