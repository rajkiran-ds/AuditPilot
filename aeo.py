"""
aeo.py
Answer Engine Optimization (AEO) scoring engine.

Fully deterministic, rule-based auditor that evaluates how ready pages are to
be read, summarized, and cited by AI assistants / answer engines (e.g.
ChatGPT, Perplexity, Google AI Overviews).

Scoring model (fixed weights, do not change):
    Final AEO Score = (0.4 x SEO Score) + Semantic Score

    Semantic Score = 60 marks total, six factors at 10 marks each:
        1. Structured Schema      3+ families = 10, 2 = 9, 1 = 7, none = 0
        2. Entity Clarity         brand / product / technology / industry,
                                  2.5 marks per category present
        3. FAQ Presence           faq_count only: 0 = 0, 1-2 = 6, 3+ = 10
        4. Trust / Consensus      five signal families, 2 marks each, with a
                                  corroboration floor of 6 when statistics and
                                  outbound references are both present
        5. Content Freshness      <=30d = 10, <=180d = 8, <=365d = 6,
                                  older = 3, missing = 2
        6. Keywords & Modifiers   title + H1 + headings: 3+ = 10, 2 = 8,
                                  1 = 6, 0 = 4

Grades (final AEO score):
    A = 90-100, B = 80-89, C = 70-79, D = 60-69, F = below 60.

Public API:
    engine = AEOEngine()
    results, overall_score = engine.audit_site(crawl_result, seo_results)
        -> tuple[dict[str, AEOResult], float]

The engine consumes ``CrawlResult`` (from crawler.py) and
``dict[url, SEOResult]`` (from seo.py) via defensive, duck-typed lookups:
both dataclass-style objects and plain dictionaries are supported.

No LLMs. No network calls. No randomness. No I/O. No print statements.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

from models import AEOResult


# ---------------------------------------------------------------------------
# Constants (single source of truth, never mutate)
# ---------------------------------------------------------------------------

SEO_WEIGHT: float = 0.4
SEMANTIC_TOTAL: float = 60.0
FACTOR_MARKS: float = 10.0

GRADE_A_MIN: float = 85.0
GRADE_B_MIN: float = 70.0
GRADE_C_MIN: float = 60.0
GRADE_D_MIN: float = 50.0

MAX_RECOMMENDED_KEYWORDS: int = 5

# Structured schema families recognized by the auditor (all else ignored).
# Close schema.org subtypes count toward their family.
SCHEMA_ALIASES: Mapping[str, frozenset[str]] = {
    "organization": frozenset({
        "organization", "corporation", "localbusiness", "onlinebusiness",
        "ngo", "educationalorganization", "governmentorganization",
    }),
    "faqpage": frozenset({"faqpage"}),
    "article": frozenset({
        "article", "newsarticle", "blogposting", "techarticle",
        "scholarlyarticle",
    }),
    "product": frozenset({"product", "productgroup", "productmodel"}),
}
SCHEMA_FAMILIES: tuple[str, ...] = tuple(SCHEMA_ALIASES)
SCHEMA_FAMILY_DISPLAY: Mapping[str, str] = {
    "organization": "Organization",
    "faqpage": "FAQPage",
    "article": "Article",
    "product": "Product",
}
SCHEMA_TWO_FAMILY_MARKS: float = 9.0
SCHEMA_ONE_FAMILY_MARKS: float = 7.0

# Entity clarity: four categories at 2.5 marks each (presence-based).
ENTITY_CATEGORY_MARKS: float = 2.5
ENTITY_CATEGORIES: tuple[str, ...] = (
    "brand", "product", "technology", "industry",
)
MIN_BRAND_LABEL_LENGTH: int = 3
SECOND_LEVEL_TLDS: frozenset[str] = frozenset(
    {"co", "com", "org", "net", "gov", "ac", "edu"}
)

# FAQ presence tiers (faq_count only).
FAQ_MID_MARKS: float = 6.0
FAQ_MID_MIN: int = 1
FAQ_FULL_MIN: int = 3

# Trust / Consensus: five signal families at 2 marks each.
TRUST_FAMILY_MARKS: float = 2.0
TRUST_CORROBORATION_FLOOR: float = 6.0
TRUST_MIN_PERCENTAGES_AS_STATISTIC: int = 2
TRUST_CITATION_TERMS: tuple[str, ...] = (
    "study", "studies", "research", "survey", "according to",
    "peer-reviewed", "journal", "report", "analysis", "white paper",
    "whitepaper", "case study", "et al",
)
TRUST_STATISTIC_TERMS: tuple[str, ...] = (
    "statistics", "statistic", "data shows", "findings", "benchmark",
)
_STAT_PATTERN: re.Pattern[str] = re.compile(
    r"\b\d{1,3}(?:,\d{3})+\b"
    r"|\b\d+(?:\.\d+)?\+?\s?(?:million|billion|trillion|thousand|x|times|"
    r"users|customers|clients|companies|countries|employees|downloads|years)\b"
)
_PERCENT_PATTERN: re.Pattern[str] = re.compile(
    r"\d+(?:\.\d+)?\s?(?:%|percent\b)"
)
_QUOTE_PATTERN: re.Pattern[str] = re.compile(r"[\"“‘].{10,200}[\"”’]")
_ATTRIBUTION_PATTERN: re.Pattern[str] = re.compile(
    r"\bsaid\b|\bstated\b|\baccording to\b"
)

# Content freshness tiers.
FRESHNESS_FULL_DAYS: int = 30
FRESHNESS_RECENT_DAYS: int = 180
FRESHNESS_YEAR_DAYS: int = 365
FRESHNESS_RECENT_MARKS: float = 8.0
FRESHNESS_YEAR_MARKS: float = 6.0
FRESHNESS_OLD_MARKS: float = 3.0
FRESHNESS_MISSING_MARKS: float = 2.0

# Keywords & modifiers (title + H1 + headings).
MODIFIER_TERMS: tuple[str, ...] = (
    "best", "top", "review", "comparison", "guide", "how", "vs",
)
MODIFIER_PATTERNS: Mapping[str, re.Pattern[str]] = {
    "best": re.compile(r"\bbest\b"),
    "top": re.compile(r"\btop\b"),
    "review": re.compile(r"\breviews?\b"),
    "comparison": re.compile(r"\bcompar(?:e|es|ing|ison|isons)\b"),
    "guide": re.compile(r"\bguides?\b"),
    "how": re.compile(r"\bhow\b"),
    "vs": re.compile(r"\bvs\b|\bversus\b"),
}
MODIFIER_TIERS: tuple[tuple[int, float], ...] = (
    (3, FACTOR_MARKS),
    (2, 8.0),
    (1, 6.0),
    (0, 4.0),
)

# Defensive field-name aliases (never assume one schema).
TEXT_FIELDS: tuple[str, ...] = (
    "text", "content", "text_content", "body_text",
)
SCHEMA_FIELDS: tuple[str, ...] = ("schema_types", "schemas")
FAQ_FIELDS: tuple[str, ...] = ("faq_count", "faqs_count", "num_faqs")
ENTITY_FIELDS: tuple[str, ...] = ("entities", "named_entities")
EXTERNAL_LINK_FIELDS: tuple[str, ...] = (
    "external_links", "external_link_count", "outbound_links",
    "num_external_links",
)
DATE_FIELDS: tuple[str, ...] = (
    "published_date", "date_published", "date",
    "last_modified", "modified", "updated_at", "updated",
)
STATUS_FIELDS: tuple[str, ...] = (
    "status_code", "status", "http_status", "code",
)

# Entity category fallback vocabulary for text matching.
CATEGORY_VOCABULARY: Mapping[str, tuple[str, ...]] = {
    "brand": ("brand", "company", "about us", "©", "copyright", "inc",
              "ltd", "llc"),
    "product": ("product", "service", "platform", "solution", "software",
                "tool", "suite", "app"),
    "technology": ("ai", "ml", "api", "cloud", "saas", "automation",
                   "machine learning", "artificial intelligence",
                   "blockchain", "data"),
    "industry": ("industry", "market", "sector", "enterprise", "b2b",
                 "healthcare", "finance", "retail", "manufacturing",
                 "education", "real estate", "logistics"),
}

# Fields on an entity record that may carry its category or name.
ENTITY_TYPE_FIELDS: tuple[str, ...] = (
    "type", "category", "kind", "label", "entity_type",
)
ENTITY_NAME_FIELDS: tuple[str, ...] = ("name", "text", "value", "entity")

# Feature-engine entity labels mapped to the four scored categories.
ENTITY_LABEL_MAP: Mapping[str, str] = {
    "brand": "brand",
    "organization": "brand",
    "organisation": "brand",
    "org": "brand",
    "company": "brand",
    "corporation": "brand",
    "product": "product",
    "products": "product",
    "service": "product",
    "services": "product",
    "technology": "technology",
    "tech": "technology",
    "industry": "industry",
    "sector": "industry",
}


# ---------------------------------------------------------------------------
# Defensive lookup helpers (mirroring the style of seo.py)
# ---------------------------------------------------------------------------

def _get(obj: Any, *names: str, default: Any = None) -> Any:
    """Fetch the first present, non-None attribute/key among ``names``.

    Works against dataclass instances, plain objects, and mappings so the
    engine never assumes one schema from crawler.py / feature_engine.py.
    """
    if obj is None:
        return default
    for name in names:
        if isinstance(obj, Mapping) and name in obj:
            value = obj[name]
            if value is not None:
                return value
        elif not isinstance(obj, Mapping) and hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def _iter_pages(crawl_result: Any) -> Iterable[tuple[str, Any]]:
    """Yield ``(url, page)`` pairs from any supported CrawlResult shape.

    Supported shapes:
        * Mapping[url, page]
        * Object with ``pages`` / ``results`` / ``crawled_pages``
          (mapping or iterable of page objects)
        * Iterable of page objects (URL read from page fields)
    """
    if crawl_result is None:
        return

    if isinstance(crawl_result, Mapping):
        for url, page in crawl_result.items():
            yield str(url), page
        return

    container = _get(crawl_result, "pages", "results", "crawled_pages",
                     "crawl_results")
    if isinstance(container, Mapping):
        for url, page in container.items():
            yield str(url), page
        return
    if container is not None:
        for page in container:
            url = _get(page, "url", "page_url", "link", default="")
            if url:
                yield str(url), page
        return

    if isinstance(crawl_result, (list, tuple, set)):
        for page in crawl_result:
            url = _get(page, "url", "page_url", "link", default="")
            if url:
                yield str(url), page


def _extract_seo_score(seo_result: Any) -> float:
    """Pull a numeric SEO score out of any SEOResult-like object."""
    raw = _get(seo_result, "score", "overall_score", "total_score",
               "seo_score", "final_score", default=0.0)
    try:
        return max(0.0, min(100.0, float(raw)))
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Field normalizers
# ---------------------------------------------------------------------------

def _normalize_text(page: Any) -> str:
    """Return lowercase page text from any supported field name."""
    raw = _get(page, *TEXT_FIELDS, default="")
    if raw is None:
        return ""
    return str(raw).lower()


def _normalize_headings_text(page: Any) -> str:
    """Return all heading text (tags + texts) as one lowercase string."""
    parts: list[str] = []
    raw = _get(page, "headings", "heading_structure", "heading_texts",
               default=[])
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            parts.append(str(key).lower())
            if isinstance(value, (list, tuple, set)):
                parts.extend(str(v).lower() for v in value)
            elif isinstance(value, str):
                parts.append(value.lower())
            elif isinstance(value, int) and value > 0:
                parts.append(str(key).lower())
    elif isinstance(raw, (list, tuple, set)):
        for item in raw:
            parts.append(str(item).lower())
    return " ".join(parts)


def _normalize_schema_types(page: Any) -> list[str]:
    """Return a deduplicated, lowercased list of schema.org types."""
    raw = _get(page, *SCHEMA_FIELDS, default=[])
    items: list[str] = []
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, Iterable):
        items = [str(x) for x in raw]
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        cleaned = item.strip().lower()
        cleaned = cleaned.replace("https://schema.org/", "")
        cleaned = cleaned.replace("http://schema.org/", "")
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def _normalize_faq_count(page: Any) -> int:
    """Return the FAQ count as provided by the feature extractor only."""
    raw = _get(page, *FAQ_FIELDS, default=0)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _normalize_entities(page: Any) -> list[Any]:
    """Return the raw entity list from any supported shape.

    A mapping of ``label -> names`` (the feature engine's shape) is flattened
    to ``(name, label)`` pairs so entity categories are preserved.
    """
    raw = _get(page, *ENTITY_FIELDS, default=[])
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, Mapping):
        out: list[Any] = []
        for label, value in raw.items():
            if isinstance(value, (list, tuple, set)):
                out.extend((str(v), str(label)) for v in value)
            elif value is not None:
                out.append((str(value), str(label)))
        return out
    if isinstance(raw, Iterable):
        return list(raw)
    return []


def _normalize_external_links(page: Any) -> int:
    """Return the number of external links from any supported shape."""
    raw = _get(page, *EXTERNAL_LINK_FIELDS, default=0)
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return max(0, raw)
    if isinstance(raw, (list, tuple, set)):
        return len(raw)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _parse_date(value: Any) -> Optional[date]:
    """Parse a date/datetime, ISO-ish or RFC 2822 string; None on failure."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    original = str(value).strip()
    if not original:
        return None
    text = original.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%m/%d/%Y",
                "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        # HTTP Last-Modified headers use RFC 2822 dates.
        return parsedate_to_datetime(original).date()
    except (TypeError, ValueError, IndexError):
        pass
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)),
                        int(match.group(3)))
        except ValueError:
            return None
    return None


def _page_date(page: Any) -> Optional[date]:
    """Return the NEWEST of published_date / last_modified (any alias)."""
    parsed = [_parse_date(_get(page, name)) for name in DATE_FIELDS]
    parsed = [d for d in parsed if d is not None]
    return max(parsed) if parsed else None


def _http_status(page: Any) -> int:
    """Return the HTTP status code from any supported field."""
    raw = _get(page, *STATUS_FIELDS, default=200)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 200


def _title_and_h1(page: Any) -> tuple[str, str]:
    """Return (title, h1_text) as lowercase strings."""
    title = str(_get(page, "title", "page_title", "meta_title",
                     default="") or "").lower().strip()
    h1_raw = _get(page, "h1", "h1_text", "h1_texts", default="")
    if isinstance(h1_raw, (list, tuple, set)):
        h1 = " ".join(str(x) for x in h1_raw).lower().strip()
    else:
        h1 = str(h1_raw).lower().strip()
    return title, h1


# ---------------------------------------------------------------------------
# Evidence helper (fixed schema required by recommendation_engine.py)
# ---------------------------------------------------------------------------

def _evidence(factor: str, issue: str, evidence_text: str,
              affected_url: str, points: float) -> dict[str, Any]:
    """Build one evidence record in the fixed schema required downstream."""
    return {
        "factor": factor,
        "issue": issue,
        "evidence": evidence_text,
        "affected_urls": [affected_url],
        "points_deducted": round(float(points), 2),
    }


# ---------------------------------------------------------------------------
# Factor scorers, each returns (score_0_to_10, details, evidence_list)
# ---------------------------------------------------------------------------

def _score_structured_schema(url: str, schema_types: list[str]) -> \
        tuple[float, str, list[dict[str, Any]]]:
    """Score structured data using the four recognized families.

    Recognized families: Organization, FAQPage, Article, Product (close
    schema.org subtypes such as Corporation or NewsArticle count toward
    their family).
    Scoring: 3+ families -> 10, 2 -> 9, 1 -> 7, none -> 0.
    All other schema types are ignored.
    """
    short_types = {t.rsplit("/", 1)[-1].strip() for t in schema_types}
    families = [name for name, aliases in SCHEMA_ALIASES.items()
                if short_types & aliases]

    if len(families) >= 3:
        score = FACTOR_MARKS
    elif len(families) == 2:
        score = SCHEMA_TWO_FAMILY_MARKS
    elif len(families) == 1:
        score = SCHEMA_ONE_FAMILY_MARKS
    else:
        score = 0.0

    evidence: list[dict[str, Any]] = []
    if score < FACTOR_MARKS:
        if not families:
            evidence.append(_evidence(
                "Structured Schema",
                "No recognized schema family present",
                "None of Organization / FAQPage / Article / Product schema "
                "were detected on the page.",
                url, FACTOR_MARKS))
        else:
            found = ", ".join(SCHEMA_FAMILY_DISPLAY[f] for f in families)
            missing = ", ".join(SCHEMA_FAMILY_DISPLAY[f]
                                for f in SCHEMA_FAMILIES
                                if f not in families)
            issue = ("Only one schema family present"
                     if len(families) == 1
                     else "Only two schema families present")
            evidence.append(_evidence(
                "Structured Schema", issue,
                f"Detected families: {found}. Add another recognized "
                f"family for full marks (missing: {missing}).",
                url, round(FACTOR_MARKS - score, 2)))

    details = f"schema_families={families or 'none'}"
    return round(score, 2), details, evidence


def _entity_names_and_types(raw_entities: list[Any]) -> \
        tuple[list[str], dict[str, list[str]]]:
    """Split raw entities into plain names and per-category names.

    Entity records may be strings (uncategorized), ``(name, label)`` pairs,
    or mappings/objects with a category field (type/category/kind/label) and
    a name field (name/text/value). Labels such as ``organization`` or
    ``service`` are mapped onto the four scored categories.
    """
    names: list[str] = []
    categorized: dict[str, list[str]] = {c: [] for c in ENTITY_CATEGORIES}
    for item in raw_entities:
        if isinstance(item, Mapping):
            name = _get(item, *ENTITY_NAME_FIELDS, default="")
            category = str(_get(item, *ENTITY_TYPE_FIELDS,
                                default="") or "").lower().strip()
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            name, category = str(item[0]), str(item[1]).lower().strip()
        else:
            name, category = str(item), ""
        name = str(name).strip()
        if not name:
            continue
        names.append(name.lower())
        category = ENTITY_LABEL_MAP.get(category, category)
        if category in categorized:
            categorized[category].append(name.lower())
    return names, categorized


def _brand_label(url: str) -> str:
    """Derive the brand label from a URL's registrable domain."""
    host = urlparse(url if "://" in url else f"https://{url}").hostname or ""
    parts = [p for p in host.lower().split(".") if p]
    if parts and parts[0] == "www":
        parts = parts[1:]
    if len(parts) >= 3 and parts[-2] in SECOND_LEVEL_TLDS:
        return parts[-3]
    if len(parts) >= 2:
        return parts[-2]
    return parts[0] if parts else ""


def _contains_term(haystack: str, term: str) -> bool:
    """True if ``term`` occurs as a whole word (optional plural) in text."""
    return bool(re.search(
        rf"(?<!\w){re.escape(term.strip())}s?(?!\w)", haystack))


def _detect_entity_categories(names: list[str],
                              categorized: Mapping[str, list[str]],
                              text: str, title: str, h1: str,
                              brand: str = "") -> dict[str, int]:
    """Count distinct entities per category (feature engine + text fallback).

    Entities labelled by the feature engine count directly; the category
    vocabulary (and the brand name from the domain) is matched against the
    title, H1 and body text. Returns a mapping of category -> distinct count.
    """
    counts: dict[str, int] = {c: 0 for c in ENTITY_CATEGORIES}
    haystack = f"{title} {h1} {text}"

    for category in ENTITY_CATEGORIES:
        detected: set[str] = set(categorized.get(category, []))
        for term in CATEGORY_VOCABULARY.get(category, ()):
            if _contains_term(haystack, term):
                detected.add(term.strip())
        if (category == "brand" and len(brand) >= MIN_BRAND_LABEL_LENGTH
                and _contains_term(haystack, brand)):
            detected.add(brand)
        counts[category] = len(detected)
    return counts


def _score_entity_clarity(url: str, raw_entities: list[Any], text: str,
                          title: str, h1: str) -> \
        tuple[float, str, list[dict[str, Any]]]:
    """Score entity clarity by category coverage (2.5 marks each).

    Categories: Brand, Product/Service, Technology, Industry.
    A category earns its 2.5 marks when at least one entity is detected;
    several entities per category are not required.
    """
    names, categorized = _entity_names_and_types(raw_entities)
    counts = _detect_entity_categories(
        names, categorized, text, title, h1, _brand_label(url))

    score = 0.0
    evidence: list[dict[str, Any]] = []
    for category in ENTITY_CATEGORIES:
        if counts[category] >= 1:
            score += ENTITY_CATEGORY_MARKS
        else:
            evidence.append(_evidence(
                "Entity Clarity",
                f"No {category} entities",
                f"Category '{category}' has no detectable entities.",
                url, ENTITY_CATEGORY_MARKS))

    score = min(FACTOR_MARKS, score)
    details = ", ".join(f"{c}={counts[c]}" for c in ENTITY_CATEGORIES)
    return round(score, 2), details, evidence


def _score_faq_presence(url: str, faq_count: int) -> \
        tuple[float, str, list[dict[str, Any]]]:
    """Score FAQ presence using ONLY faq_count.

    Scoring: 0 -> 0, 1-2 -> 6, 3+ -> 10. No question-mark inference.
    """
    if faq_count >= FAQ_FULL_MIN:
        score = FACTOR_MARKS
    elif faq_count >= FAQ_MID_MIN:
        score = FAQ_MID_MARKS
    else:
        score = 0.0

    evidence: list[dict[str, Any]] = []
    if score < FACTOR_MARKS:
        if faq_count == 0:
            evidence.append(_evidence(
                "FAQ Presence", "No FAQ items on page",
                "faq_count is 0; no FAQ content was extracted.",
                url, FACTOR_MARKS))
        else:
            evidence.append(_evidence(
                "FAQ Presence", "Thin FAQ coverage",
                f"Only {faq_count} FAQ item(s). Add at least "
                f"{FAQ_FULL_MIN - faq_count} more to reach full marks.",
                url, round(FACTOR_MARKS - score, 2)))

    details = f"faq_count={faq_count}"
    return round(score, 2), details, evidence


def _score_trust_consensus(url: str, text: str,
                           external_links: int) -> \
        tuple[float, str, list[dict[str, Any]]]:
    """Score trust/consensus across five signal families (2 marks each).

    Families: statistics, percentages, research citations, quotes,
    external references. A page is not required to carry all five: when it
    has both statistics and outbound references, it is guaranteed at least
    ``TRUST_CORROBORATION_FLOOR`` marks. Deductions are spread across the
    missing families so the evidence always sums to the marks lost.
    """
    percent_hits = len(_PERCENT_PATTERN.findall(text))
    has_statistics = (
        any(t in text for t in TRUST_STATISTIC_TERMS)
        or bool(_STAT_PATTERN.search(text))
        or percent_hits >= TRUST_MIN_PERCENTAGES_AS_STATISTIC
    )
    has_percentages = percent_hits >= 1
    has_citations = any(t in text for t in TRUST_CITATION_TERMS)
    has_quotes = (bool(_QUOTE_PATTERN.search(text))
                  or bool(_ATTRIBUTION_PATTERN.search(text)))
    has_external_refs = external_links >= 1

    families: Mapping[str, bool] = {
        "statistics": has_statistics,
        "percentages": has_percentages,
        "research_citations": has_citations,
        "quotes": has_quotes,
        "external_references": has_external_refs,
    }

    score = sum(TRUST_FAMILY_MARKS for present in families.values()
                if present)
    if has_statistics and has_external_refs:
        score = max(score, TRUST_CORROBORATION_FLOOR)
    score = min(FACTOR_MARKS, score)

    missing = [family for family, present in families.items()
               if not present]
    per_family = (round((FACTOR_MARKS - score) / len(missing), 2)
                  if missing else 0.0)

    evidence: list[dict[str, Any]] = []
    for family in missing:
        label = family.replace("_", " ")
        evidence.append(_evidence(
            "Trust / Consensus",
            f"Missing {label} signals",
            f"No {label} signals were detected.",
            url, per_family))

    details = ", ".join(f"{k}={'yes' if v else 'no'}"
                        for k, v in families.items())
    return round(score, 2), details, evidence


def _score_freshness(url: str, page_date: Optional[date],
                     today: date) -> \
        tuple[float, str, list[dict[str, Any]]]:
    """Score content freshness against the current date (10 marks).

    Rule:
        newest of published_date / last_modified is
            <= 30 days old   -> 10
            <= 180 days old  -> 8
            <= 365 days old  -> 6
            older            -> 3
        no date              -> 2 (many enterprise pages omit dates)
    """
    if page_date is None:
        return FRESHNESS_MISSING_MARKS, "no date signals found", [_evidence(
            "Content Freshness", "No publication or modification date",
            "Neither published_date nor last_modified could be parsed.",
            url, FACTOR_MARKS - FRESHNESS_MISSING_MARKS)]

    age_days = max(0, (today - page_date).days)

    if age_days <= FRESHNESS_FULL_DAYS:
        marks = FACTOR_MARKS
    elif age_days <= FRESHNESS_RECENT_DAYS:
        marks = FRESHNESS_RECENT_MARKS
    elif age_days <= FRESHNESS_YEAR_DAYS:
        marks = FRESHNESS_YEAR_MARKS
    else:
        marks = FRESHNESS_OLD_MARKS

    evidence: list[dict[str, Any]] = []
    if marks < FACTOR_MARKS:
        evidence.append(_evidence(
            "Content Freshness", "Content not recent",
            f"Newest date signal is {age_days} days old (full marks "
            f"require <= {FRESHNESS_FULL_DAYS} days).",
            url, round(FACTOR_MARKS - marks, 2)))

    details = f"age_days={age_days}, page_date={page_date.isoformat()}"
    return round(marks, 2), details, evidence


def _detect_modifiers(haystack: str) -> list[str]:
    """Return the sorted list of modifiers present in title + H1 + headings."""
    return sorted(name for name, pattern in MODIFIER_PATTERNS.items()
                  if pattern.search(haystack))


def _score_keywords_modifiers(url: str, present: list[str]) -> \
        tuple[float, str, list[dict[str, Any]]]:
    """Score modifier coverage in title + H1 + headings (10 marks).

    Scoring: 3+ -> 10, 2 -> 8, 1 -> 6, 0 -> 4.
    """
    count = len(present)
    score = 0.0
    for minimum, marks in MODIFIER_TIERS:
        if count >= minimum:
            score = marks
            break

    evidence: list[dict[str, Any]] = []
    if score < FACTOR_MARKS:
        evidence.append(_evidence(
            "Keywords & Modifiers", "Few query modifiers present",
            f"Only {count} modifier(s) in title/H1/headings "
            f"({', '.join(present) or 'none'}); 3+ required for full marks.",
            url, round(FACTOR_MARKS - score, 2)))

    details = f"modifiers_present={count}"
    return round(score, 2), details, evidence


# ---------------------------------------------------------------------------
# Recommended keywords (missing modifiers only, rule-based, max 5)
# ---------------------------------------------------------------------------

def _build_recommended_keywords(title: str, h1: str, entities: list[str],
                                present: list[str]) -> list[str]:
    """Suggest keyword variants built only from MISSING modifiers.

    The topic seed is taken from the H1, falling back to the title, then
    to the first entity, then to a generic placeholder. Each suggestion is
    ``<Modifier> <Topic>`` for modifiers absent from title + headings.
    """
    seed = h1.strip() or title.strip()
    if not seed and entities:
        seed = entities[0].strip()
    if not seed:
        seed = "your topic"
    seed = re.sub(r"\s+", " ", seed).strip(" -–—|").title()

    suggestions: list[str] = []
    seen: set[str] = set()
    for modifier in MODIFIER_TERMS:
        if len(suggestions) >= MAX_RECOMMENDED_KEYWORDS:
            break
        if modifier in present:
            continue
        keyword = f"{modifier.title()} {seed}"
        if keyword.lower() not in seen:
            seen.add(keyword.lower())
            suggestions.append(keyword)
    return suggestions


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

def _grade(score: float) -> str:
    """Map a 0-100 final score to the fixed letter bands."""
    if score >= GRADE_A_MIN:
        return "A"
    if score >= GRADE_B_MIN:
        return "B"
    if score >= GRADE_C_MIN:
        return "C"
    return "D"


# ---------------------------------------------------------------------------
# Public engine
# ---------------------------------------------------------------------------

class AEOEngine:
    """Deterministic, rule-based AEO scoring engine.

    Usage:
        engine = AEOEngine()
        results, overall_score = engine.audit_site(crawl_result, seo_results)

    The engine performs no network access, no LLM calls, no I/O, and no
    printing. All scoring rules are fixed; the only time input is ``today``,
    which defaults to ``date.today()`` and may be injected for reproducible
    test runs.
    """

    FACTORS: tuple[str, ...] = (
        "structured_schema",
        "entity_clarity",
        "faq_presence",
        "trust_consensus",
        "content_freshness",
        "keywords_modifiers",
    )

    def __init__(self, today: Optional[date] = None) -> None:
        """Optionally inject the reference date for deterministic reruns."""
        self._today: date = today if today is not None else date.today()

    def audit_site(self, crawl_result: Any,
                   seo_results: Any) -> tuple[dict[str, AEOResult], float]:
        """Audit every crawled page and return per-page results + site score.

        Args:
            crawl_result: CrawlResult from crawler.py (mapping, object with
                ``pages``/``results``, or iterable of page dicts/objects).
            seo_results: Mapping of URL to SEOResult (or SEOResult-like
                objects/dicts carrying a numeric score).

        Returns:
            Tuple of:
                * ``dict`` mapping URL -> :class:`AEOResult`
                * Site-wide AEO score (mean of page final scores, 0-100).
        """
        results: dict[str, AEOResult] = {}

        for url, page in _iter_pages(crawl_result):
            seo_entry: Any = None
            if isinstance(seo_results, Mapping):
                seo_entry = seo_results.get(url)
            seo_score = _extract_seo_score(seo_entry)

            results[url] = self._audit_page(url, page, seo_score)

        site_score = round(
            sum(r.score for r in results.values()) / len(results), 1
        ) if results else 0.0
        return results, site_score

    # ------------------------------------------------------------------
    # Internal: single-page audit
    # ------------------------------------------------------------------

    def _audit_page(self, url: str, page: Any,
                    seo_score: float) -> AEOResult:
        """Audit one page, applying the fixed 0.4xSEO + 60-mark model."""
        status = _http_status(page)
        reachable = status == 200
        seo_contribution = round(SEO_WEIGHT * seo_score, 2)

        if not reachable:
            skipped = _evidence(
                "Semantic Evaluation", "Page unreachable",
                f"HTTP status {status}; semantic evaluation was skipped. "
                f"Only the SEO contribution ({seo_contribution:.1f} pts) "
                f"was applied.",
                url, SEMANTIC_TOTAL)
            return AEOResult(
                score=round(seo_contribution, 2),
                grade=_grade(seo_contribution),

                structured_schema_score=0.0,
                entity_clarity_score=0.0,
                faq_score=0.0,
                trustworthiness_score=0.0,
                freshness_score=0.0,
                keyword_modifier_score=0.0,
                citation_readiness_score=0.0,

                factor_scores={f: 0.0 for f in self.FACTORS},
                factor_details={f: "skipped" for f in self.FACTORS},
                recommended_keywords=[],

                issues=["Page unreachable"],
                evidence=[skipped],
            )

        text = _normalize_text(page)
        headings_text = _normalize_headings_text(page)
        title, h1 = _title_and_h1(page)
        schema_types = _normalize_schema_types(page)
        faq_count = _normalize_faq_count(page)
        raw_entities = _normalize_entities(page)
        external_links = _normalize_external_links(page)
        page_date = _page_date(page)

        modifier_haystack = f"{title} {h1} {headings_text}"
        present_modifiers = _detect_modifiers(modifier_haystack)

        factor_scores: dict[str, float] = {}
        factor_details: dict[str, str] = {}
        evidence: list[dict[str, Any]] = []

        scorers = {
            "structured_schema": lambda: _score_structured_schema(
                url, schema_types),
            "entity_clarity": lambda: _score_entity_clarity(
                url, raw_entities, text, title, h1),
            "faq_presence": lambda: _score_faq_presence(url, faq_count),
            "trust_consensus": lambda: _score_trust_consensus(
                url, text, external_links),
            "content_freshness": lambda: _score_freshness(
                url, page_date, self._today),
            "keywords_modifiers": lambda: _score_keywords_modifiers(
                url, present_modifiers),
        }

        for factor in self.FACTORS:
            score, details, factor_evidence = scorers[factor]()
            factor_scores[factor] = score
            factor_details[factor] = details
            evidence.extend(factor_evidence)

        semantic_score = round(sum(factor_scores.values()), 2)
        final_score = round(
            min(100.0, seo_contribution + semantic_score), 2)

        entity_names, _ = _entity_names_and_types(raw_entities)
        keywords = _build_recommended_keywords(
            title, h1, entity_names, present_modifiers)

        return AEOResult(
            score=final_score,
            grade=_grade(final_score),

            structured_schema_score=factor_scores["structured_schema"] * 10,
            entity_clarity_score=factor_scores["entity_clarity"] * 10,
            faq_score=factor_scores["faq_presence"] * 10,
            trustworthiness_score=factor_scores["trust_consensus"] * 10,
            freshness_score=factor_scores["content_freshness"] * 10,
            keyword_modifier_score=factor_scores["keywords_modifiers"] * 10,
            citation_readiness_score=(semantic_score / SEMANTIC_TOTAL) * 100,

            factor_scores=factor_scores,
            factor_details=factor_details,
            recommended_keywords=keywords,

            issues=[e["issue"] for e in evidence],
            evidence=evidence,
        )


CRITERIA_LABELS = {
    "structured_schema": "Structured Schema",
    "entity_clarity": "Entity Clarity",
    "faq_presence": "FAQ Presence",
    "trust_consensus": "Trust & Consensus",
    "content_freshness": "Content Freshness",
    "keywords_modifiers": "Keywords & Modifiers",
}


def audit_site_aeo(crawled_pages, api_key=None, model=None,
                   progress_callback=None):
    """
    Compatibility wrapper expected by legacy callers of app.py.
    """
    from seo import SEOEngine

    seo_engine = SEOEngine()
    seo_results, _ = seo_engine.audit_site(crawled_pages)

    engine = AEOEngine()
    results, overall = engine.audit_site(crawled_pages, seo_results)

    page_audits = []

    for i, (url, result) in enumerate(results.items(), start=1):
        if progress_callback:
            progress_callback(i, len(results))

        page_audits.append({
            "url": url,
            "overall_score": result.score,
            "criteria_scores": result.factor_scores,
            "recommendations": result.recommended_keywords,
        })

    criteria_avgs = {}
    for key in CRITERIA_LABELS:
        vals = [r.factor_scores[key] * 10 for r in results.values()]
        criteria_avgs[key] = round(sum(vals) / len(vals), 1) if vals else 0

    return {
        "overall_score": overall,
        "page_audits": page_audits,
        "criteria_avgs": criteria_avgs,
        "used_llm": False,
    }