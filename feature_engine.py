"""
feature_engine.py

Shared preprocessing layer for the SEO + AEO Website Intelligence Agent (v2).
Converts raw HTML into a structured `PageData` object. Performs extraction
only: no scoring, no business logic, no LLM calls.

Extracted signals:
    Technical : title, meta description, canonical, robots meta, internal and
                external link counts, H1-H6 hierarchy, image and ALT counts,
                word count, paragraph count, visible text
    AEO       : schema.org types (JSON-LD + microdata), FAQ item count, entity
                mentions, published date, last-modified metadata

`PageData` is defined in models.py and is not modified here. Because the model
is not part of this file, `_build_page_data` offers every extracted value under
its common field names and passes to `PageData` only the fields it declares.
"""

import json
import re
from collections import Counter
from dataclasses import fields, is_dataclass
from inspect import Parameter, signature
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from bs4.element import (
    CData,
    Comment,
    Declaration,
    Doctype,
    ProcessingInstruction,
)

from models import PageData

_STRUCTURAL_TAGS_TO_STRIP = ("script", "style", "nav", "footer", "noscript", "svg")
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_FAQ_SCHEMA_TYPES = ("FAQPage",)
_KNOWN_SCHEMA_TYPES = ("Organization", "FAQPage", "Article", "Product")

# Tags whose text is never body content (structural tags plus the document head).
_NON_CONTENT_TAGS = list(_STRUCTURAL_TAGS_TO_STRIP) + ["head", "template"]
_NON_TEXT_NODES = (Comment, CData, Declaration, Doctype, ProcessingInstruction)
_NON_PAGE_HREF_PREFIXES = ("#", "mailto:", "tel:", "javascript:", "sms:", "data:")

_MAX_TEXT_CHARS = 20000
_MAX_ENTITY_MENTIONS = 20

_ORGANIZATION_TYPES = frozenset(
    {
        "Organization",
        "Corporation",
        "LocalBusiness",
        "OnlineBusiness",
        "NGO",
        "EducationalOrganization",
        "GovernmentOrganization",
    }
)
_FAQ_ITEM_TAGS = ("dt", "summary", "h2", "h3", "h4", "h5", "h6")

_PUBLISHED_META_KEYS = (
    "article:published_time",
    "og:article:published_time",
    "datepublished",
    "dc.date.issued",
    "dc.date",
    "publishdate",
    "pubdate",
    "date",
)
_MODIFIED_META_KEYS = (
    "article:modified_time",
    "og:article:modified_time",
    "og:updated_time",
    "datemodified",
    "dc.date.modified",
    "last-modified",
    "revised",
)

_PROPER_PHRASE_PATTERN = re.compile(r"\b[A-Z][A-Za-z0-9&'\-]+(?:[ ]+[A-Z][A-Za-z0-9&'\-]+)+\b")
_ACRONYM_PATTERN = re.compile(r"\b[A-Z]{2,6}\b")
_LEADING_STOPWORDS = frozenset(
    {
        "The", "A", "An", "In", "On", "At", "For", "And", "Of", "To", "With", "Our",
        "Your", "We", "You", "It", "This", "That", "These", "Those", "Read", "Learn",
        "Click", "Contact", "Home", "About", "See", "Get", "View", "Sign", "Log",
    }
)  # fmt: skip
_JSON_LD_TYPE_PATTERN = re.compile(r'"@type"\s*:\s*"([^"]+)"')


class FeatureEngine:
    """Extracts structured, score-free features from a single page's raw HTML."""

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def extract_features(
        self,
        url: str,
        html: str,
        status_code: int,
        sitemap_last_modified: str | None = None,
    ) -> PageData:
        """Parse raw HTML for `url` and return a populated PageData object."""
        soup = self._parse_html(html or "")

        title = self._extract_title(soup)
        meta_description = self._extract_meta(soup)
        h1, h2_count, headings = self._extract_headings(soup)
        internal_links, external_links = self._extract_links(soup, url)
        total_images, missing_alt_images = self._extract_images(soup)
        schema_types = self._extract_schema(soup)
        faq_count = self._extract_faqs(soup, schema_types)
        canonical = self._extract_canonical(soup, url)
        word_count = self._extract_word_count(soup)
        robots_meta = self._extract_robots_meta(soup)
        entities = self._extract_entity_mentions(soup)

        published_date = self._extract_published_date(soup)
        html_modified = self._extract_modified_date(soup)
        last_modified = sitemap_last_modified or html_modified
        text = self._extract_text(soup)
        paragraph_count = self._extract_paragraph_count(soup)

        payload: dict[str, Any] = {
            "url": url,
            "status_code": status_code,
            "title": title,
            "title_length": len(title),
            "meta_description": meta_description,
            "meta_description_length": len(meta_description),
            "h1": h1,
            "h1_texts": h1,
            "h1_count": len(h1),
            "h2_count": h2_count,
            "headings": headings,
            "heading_structure": headings,
            "internal_links": internal_links,
            "external_links": external_links,
            "total_images": total_images,
            "images_total": total_images,
            "image_count": total_images,
            "missing_alt_images": missing_alt_images,
            "images_missing_alt": missing_alt_images,
            "schema_types": schema_types,
            "has_schema_markup": bool(schema_types),
            "faq_count": faq_count,
            "has_faq_schema": any(t in schema_types for t in _FAQ_SCHEMA_TYPES),
            "canonical": canonical,
            "canonical_url": canonical,
            "word_count": word_count,
            "paragraph_count": paragraph_count,
            "robots_meta": robots_meta,
            "entities": entities,
            "entity_mentions": entities,
            "published_date": published_date,
            "last_modified": last_modified,
            "sitemap_last_modified": sitemap_last_modified,
            "text": text,
            "text_content": text,
            "text_sample": text,
        }
        return self._build_page_data(payload)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------
    def _parse_html(self, html: str) -> BeautifulSoup:
        """Convert raw HTML into a BeautifulSoup object."""
        return BeautifulSoup(html, "html.parser")

    # ------------------------------------------------------------------
    # Head elements
    # ------------------------------------------------------------------
    def _extract_title(self, soup: BeautifulSoup) -> str:
        """Return the stripped <title> text, or an empty string."""
        tag = soup.find("title")
        if tag is None:
            return ""
        return " ".join(tag.get_text(" ", strip=True).split())

    def _extract_meta(self, soup: BeautifulSoup, name: str = "description") -> str:
        """Return the content of `<meta name="{name}">` (case-insensitive), or ''."""
        pattern = re.compile(rf"^{re.escape(name)}$", re.IGNORECASE)
        for meta in soup.find_all("meta", attrs={"name": pattern}):
            content = meta.get("content")
            if isinstance(content, str) and content.strip():
                return " ".join(content.split())
        return ""

    def _extract_robots_meta(self, soup: BeautifulSoup) -> str:
        """Return the robots meta directive (for example 'noindex, follow'), or ''."""
        return self._extract_meta(soup, "robots")

    def _extract_canonical(self, soup: BeautifulSoup, base_url: str | None = None) -> str:
        """Return the canonical href (resolved against `base_url` when relative), or ''."""
        for link in soup.find_all("link", rel="canonical"):
            href = link.get("href")
            if not isinstance(href, str) or not href.strip():
                continue
            href = href.strip()
            return urljoin(base_url, href) if base_url else href
        return ""

    def _extract_robots_directives(self, soup: BeautifulSoup) -> list[str]:
        """Return the robots meta directives as a lowercase list."""
        raw = self._extract_robots_meta(soup)
        return [part.strip().lower() for part in raw.split(",") if part.strip()]

    # ------------------------------------------------------------------
    # Headings, links, images
    # ------------------------------------------------------------------
    def _extract_headings(
        self, soup: BeautifulSoup
    ) -> tuple[list[str], int, dict[str, list[str]]]:
        """Return (H1 texts, number of H2s, mapping of h1..h6 to heading texts)."""
        headings: dict[str, list[str]] = {}
        for tag_name in _HEADING_TAGS:
            headings[tag_name] = [
                " ".join(tag.get_text(" ", strip=True).split())
                for tag in soup.find_all(tag_name)
            ]
        return headings["h1"], len(headings["h2"]), headings

    def _extract_links(self, soup: BeautifulSoup, base_url: str) -> tuple[int, int]:
        """Return (internal link count, external link count) for anchors on the page."""
        base_host = self._host(base_url)
        internal = 0
        external = 0
        for anchor in soup.find_all("a", href=True):
            href = str(anchor["href"]).strip()
            if not href or href.lower().startswith(_NON_PAGE_HREF_PREFIXES):
                continue
            absolute = urljoin(base_url, href)
            if urlparse(absolute).scheme not in ("http", "https"):
                continue
            if self._host(absolute) == base_host:
                internal += 1
            else:
                external += 1
        return internal, external

    def _extract_images(self, soup: BeautifulSoup) -> tuple[int, int]:
        """Return (total images, images with a missing or blank ALT attribute)."""
        images = soup.find_all("img")
        missing = sum(1 for image in images if not str(image.get("alt") or "").strip())
        return len(images), missing

    # ------------------------------------------------------------------
    # Structured data
    # ------------------------------------------------------------------
    def _extract_schema(self, soup: BeautifulSoup) -> list[str]:
        """Return unique schema.org type names from JSON-LD and microdata."""
        types: list[str] = []

        for raw, parsed in self._json_ld_payloads(soup):
            if parsed is None:
                types.extend(self._short_type(m) for m in _JSON_LD_TYPE_PATTERN.findall(raw))
                continue
            for node in self._flatten_nodes(parsed):
                types.extend(self._node_types(node))

        for tag in soup.find_all(attrs={"itemtype": True}):
            types.extend(self._short_type(part) for part in str(tag["itemtype"]).split())

        return list(dict.fromkeys(t for t in types if t))

    def _extract_faqs(self, soup: BeautifulSoup, schema_types: list[str]) -> int:
        """Return the number of FAQ items found in schema markup or FAQ sections.

        Counts Question entities in JSON-LD and microdata, and question items
        inside HTML containers whose id or class marks them as an FAQ. The
        larger of the two counts is returned so one FAQ is never counted twice.
        """
        schema_questions = 0
        for _raw, parsed in self._json_ld_payloads(soup):
            if parsed is None:
                continue
            schema_questions += sum(
                1 for node in self._flatten_nodes(parsed) if "Question" in self._node_types(node)
            )
        for tag in soup.find_all(attrs={"itemtype": True}):
            if any(self._short_type(p) == "Question" for p in str(tag["itemtype"]).split()):
                schema_questions += 1

        seen: set[int] = set()
        for container in soup.find_all(self._is_faq_container):
            items = container.find_all("details") or container.find_all("dt")
            if not items:
                items = [
                    tag
                    for tag in container.find_all(_FAQ_ITEM_TAGS)
                    if tag.get_text(" ", strip=True).endswith("?")
                ]
            seen.update(id(item) for item in items)

        return max(schema_questions, len(seen))

    # ------------------------------------------------------------------
    # Content
    # ------------------------------------------------------------------
    def _extract_word_count(self, soup: BeautifulSoup) -> int:
        """Return the number of words in visible body content (nav/footer excluded)."""
        return sum(len(text.split()) for text in self._visible_strings(soup))

    def _extract_text(self, soup: BeautifulSoup) -> str:
        """Return visible body text as a single string, capped in length."""
        return " ".join(self._visible_strings(soup))[:_MAX_TEXT_CHARS]

    def _extract_paragraph_count(self, soup: BeautifulSoup) -> int:
        """Return the number of non-empty <p> elements in body content."""
        return sum(
            1
            for paragraph in soup.find_all("p")
            if paragraph.get_text(strip=True) and not self._is_excluded(paragraph)
        )

    def _extract_entity_mentions(self, soup: BeautifulSoup) -> dict[str, list[str]]:
        """Return entity mentions grouped by label, extracted without NLP.

        Labels:
            organization : JSON-LD organizations, og:site_name, application-name
            product      : JSON-LD Product names
            service      : JSON-LD Service names
            person       : JSON-LD Person names, meta author
            mentions     : frequent capitalised phrases and acronyms in body text
        """
        buckets: dict[str, list[str]] = {
            "organization": [],
            "product": [],
            "service": [],
            "person": [],
            "mentions": [],
        }

        for _raw, parsed in self._json_ld_payloads(soup):
            if parsed is None:
                continue
            for node in self._flatten_nodes(parsed):
                name = node.get("name")
                if not isinstance(name, str) or not name.strip():
                    continue
                node_types = set(self._node_types(node))
                if node_types & _ORGANIZATION_TYPES:
                    buckets["organization"].append(name.strip())
                if "Product" in node_types:
                    buckets["product"].append(name.strip())
                if "Service" in node_types:
                    buckets["service"].append(name.strip())
                if "Person" in node_types:
                    buckets["person"].append(name.strip())

        for key in ("og:site_name", "application-name"):
            value = self._meta_content(soup, key)
            if value:
                buckets["organization"].append(value)
        author = self._meta_content(soup, "author")
        if author:
            buckets["person"].append(author)

        text = "\n".join(self._visible_strings(soup))
        counter: Counter[str] = Counter()
        for match in _PROPER_PHRASE_PATTERN.findall(text):
            words = match.split()
            while words and words[0] in _LEADING_STOPWORDS:
                words.pop(0)
            if len(words) >= 2:
                counter[" ".join(words)] += 1
        counter.update(_ACRONYM_PATTERN.findall(text))
        buckets["mentions"] = [name for name, _count in counter.most_common(_MAX_ENTITY_MENTIONS)]

        return {label: self._unique_casefold(names) for label, names in buckets.items()}

    # ------------------------------------------------------------------
    # Freshness metadata
    # ------------------------------------------------------------------
    def _extract_published_date(self, soup: BeautifulSoup) -> str:
        """Return the page's declared publication date string, or ''."""
        for key in _PUBLISHED_META_KEYS:
            value = self._meta_content(soup, key)
            if value:
                return value
        value = self._json_ld_value(soup, "datePublished")
        if value:
            return value
        value = self._itemprop_value(soup, "datePublished")
        if value:
            return value
        time_tag = soup.find("time", attrs={"datetime": True})
        return str(time_tag["datetime"]).strip() if time_tag is not None else ""

    def _extract_modified_date(self, soup: BeautifulSoup) -> str:
        """Return the page's declared last-modified date string, or ''."""
        for key in _MODIFIED_META_KEYS:
            value = self._meta_content(soup, key)
            if value:
                return value
        return self._json_ld_value(soup, "dateModified") or self._itemprop_value(
            soup, "dateModified"
        )

    # ------------------------------------------------------------------
    # PageData construction
    # ------------------------------------------------------------------
    def _build_page_data(self, payload: dict[str, Any]) -> PageData:
        """Construct PageData from the payload, keeping only fields it declares."""
        accepted = self._page_data_fields()
        values = payload if accepted is None else {k: v for k, v in payload.items() if k in accepted}
        try:
            return PageData(**values)
        except TypeError as exc:
            raise TypeError(
                f"PageData could not be built from the extracted features ({exc}). "
                "Add the missing field name to the payload in extract_features()."
            ) from exc

    @staticmethod
    def _page_data_fields() -> set[str] | None:
        """Return the constructor field names of PageData, or None if it takes **kwargs."""
        if is_dataclass(PageData):
            return {f.name for f in fields(PageData) if f.init}
        model_fields = getattr(PageData, "model_fields", None)
        if isinstance(model_fields, dict):
            return set(model_fields)
        try:
            parameters = signature(PageData).parameters
        except (TypeError, ValueError):
            return None
        if any(p.kind is Parameter.VAR_KEYWORD for p in parameters.values()):
            return None
        return {name for name in parameters if name != "self"}

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------
    def _visible_strings(self, soup: BeautifulSoup) -> list[str]:
        """Return stripped text nodes from body content, excluding nav, footer and scripts."""
        root = soup.body or soup
        strings: list[str] = []
        for node in root.find_all(string=True):
            if isinstance(node, _NON_TEXT_NODES):
                continue
            if node.find_parent(_NON_CONTENT_TAGS) is not None:
                continue
            text = " ".join(str(node).split())
            if text:
                strings.append(text)
        return strings

    @staticmethod
    def _is_excluded(tag: Tag) -> bool:
        """True if the tag sits inside a structural or head element."""
        return tag.find_parent(_NON_CONTENT_TAGS) is not None

    @staticmethod
    def _host(url: str) -> str:
        """Return the lowercase hostname of a URL without a leading 'www.'."""
        host = (urlparse(url).hostname or "").lower()
        return host[4:] if host.startswith("www.") else host

    @staticmethod
    def _short_type(value: str) -> str:
        """Reduce 'https://schema.org/Article' to 'Article'."""
        return value.strip().rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1].strip()

    def _node_types(self, node: dict[str, Any]) -> list[str]:
        """Return the short @type names declared on a JSON-LD node."""
        raw = node.get("@type")
        if isinstance(raw, str):
            return [self._short_type(raw)]
        if isinstance(raw, list):
            return [self._short_type(item) for item in raw if isinstance(item, str)]
        return []

    @staticmethod
    def _json_ld_payloads(soup: BeautifulSoup) -> list[tuple[str, Any]]:
        """Return (raw text, parsed JSON or None when invalid) for each JSON-LD script."""
        payloads: list[tuple[str, Any]] = []
        for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
            raw = (script.string or script.get_text() or "").strip()
            if not raw:
                continue
            try:
                payloads.append((raw, json.loads(raw)))
            except ValueError:
                payloads.append((raw, None))
        return payloads

    def _flatten_nodes(self, data: Any) -> list[dict[str, Any]]:
        """Return every dict nested anywhere in parsed JSON-LD (including @graph)."""
        nodes: list[dict[str, Any]] = []
        if isinstance(data, dict):
            nodes.append(data)
            for value in data.values():
                if isinstance(value, (dict, list)):
                    nodes.extend(self._flatten_nodes(value))
        elif isinstance(data, list):
            for item in data:
                nodes.extend(self._flatten_nodes(item))
        return nodes

    def _json_ld_value(self, soup: BeautifulSoup, key: str) -> str:
        """Return the first string value for `key` found in any JSON-LD node, or ''."""
        for _raw, parsed in self._json_ld_payloads(soup):
            if parsed is None:
                continue
            for node in self._flatten_nodes(parsed):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    @staticmethod
    def _itemprop_value(soup: BeautifulSoup, prop: str) -> str:
        """Return the content/datetime/text of the first element with itemprop=`prop`."""
        tag = soup.find(attrs={"itemprop": prop})
        if tag is None:
            return ""
        value = tag.get("content") or tag.get("datetime") or tag.get_text(strip=True)
        return str(value).strip()

    @staticmethod
    def _meta_content(soup: BeautifulSoup, key: str) -> str:
        """Return meta content whose name, property or itemprop equals `key` (case-insensitive)."""
        wanted = key.lower()
        for meta in soup.find_all("meta"):
            label = meta.get("name") or meta.get("property") or meta.get("itemprop") or ""
            content = meta.get("content")
            if str(label).strip().lower() == wanted and isinstance(content, str) and content.strip():
                return " ".join(content.split())
        return ""

    @staticmethod
    def _is_faq_container(tag: Tag) -> bool:
        """True for block elements whose id or class marks them as an FAQ section."""
        if tag.name not in ("section", "div", "ul", "ol", "dl", "article", "aside"):
            return False
        identifiers = f"{tag.get('id') or ''} {' '.join(tag.get('class') or [])}".lower()
        return any(marker in identifiers for marker in ("faq", "frequently-asked", "frequently_asked"))

    @staticmethod
    def _unique_casefold(values: list[str]) -> list[str]:
        """De-duplicate strings case-insensitively, preserving first-seen order."""
        seen: set[str] = set()
        unique: list[str] = []
        for value in values:
            key = value.casefold()
            if key not in seen:
                seen.add(key)
                unique.append(value)
        return unique