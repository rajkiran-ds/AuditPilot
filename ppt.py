"""
ppt.py

Executive PowerPoint (.pptx) report generator for the SEO + AEO Website
Intelligence Agent (v2). Replaces the legacy `report.py` (ReportLab/PDF).

Uses only `python-pptx` and `models.py`. Builds a fixed 8-slide, 16:9
executive deck in a McKinsey/Salesforce-style enterprise theme: white
background, dark-blue headings, blue-accent KPI cards.

Scoring note: per product requirement, the AEO score is never shown as an
independent number. It is always decomposed and recomposed as:

    AEO Final = (SEO Score x 0.40) + Semantic AEO Score (/60)

where Semantic AEO Score sums six sub-metrics (Structured Schema, Entity
Clarity, FAQ Presence, Trustworthiness, Freshness, Keywords & Modifiers),
each contributing up to 10 points.
"""

from dataclasses import dataclass

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.slide import Slide
from pptx.util import Emu, Inches, Pt

from models import AuditResult, RecommendationItem

# ---------------------------------------------------------------------------
# Theme constants — McKinsey / Salesforce style: white canvas, navy headings,
# blue accents, slate-gray supporting text.
# ---------------------------------------------------------------------------
COLOR_WHITE = RGBColor(0xFF, 0xFF, 0xFF)
COLOR_NAVY = RGBColor(0x0B, 0x2D, 0x5C)
COLOR_BLUE = RGBColor(0x1F, 0x6F, 0xEB)
COLOR_LIGHT_BLUE = RGBColor(0xE9, 0xF1, 0xFD)
COLOR_SLATE = RGBColor(0x44, 0x50, 0x5C)
COLOR_LIGHT_GRAY = RGBColor(0xF4, 0xF5, 0xF7)
COLOR_BORDER_GRAY = RGBColor(0xDA, 0xDE, 0xE3)
COLOR_AMBER = RGBColor(0xC9, 0x7A, 0x1A)
COLOR_RED = RGBColor(0xB3, 0x38, 0x2C)
COLOR_GREEN = RGBColor(0x2E, 0x7D, 0x4F)

SLIDE_WIDTH = Inches(13.333)
SLIDE_HEIGHT = Inches(7.5)

FONT_FAMILY = "Calibri"

_SEMANTIC_METRIC_LABELS: tuple[str, ...] = (
    "Structured Schema",
    "Entity Clarity",
    "FAQ Presence",
    "Trustworthiness",
    "Freshness",
    "Keywords & Modifiers",
)

_SEO_METRICS: tuple[tuple[str, str], ...] = (
    ("Title", "title_score"),
    ("Meta Description", "meta_score"),
    ("H1 Hierarchy", "h1_score"),
    ("Internal Linking", "internal_link_score"),
    ("Canonical", "canonical_score"),
    ("Shareable Images", "shareable_images_score"),
    ("ALT Text", "alt_score"),
    ("Sitemap", "sitemap_score"),
    ("Robots.txt", "robots_score"),
    ("Content Quality", "content_quality_score"),
)
REFERENCE_URLS = {
    "Title":
        "https://developers.google.com/search/docs/fundamentals/seo-starter-guide",
    "Meta Description":
        "https://developers.google.com/search/docs/appearance/snippet",
    "Canonical":
        "https://developers.google.com/search/docs/crawling-indexing/consolidate-duplicate-urls",
    "H1 Hierarchy":
        "https://developers.google.com/search/docs/fundamentals/seo-starter-guide",
    "Internal Linking":
        "https://developers.google.com/search/docs/crawling-indexing/links-crawlable",
    "Sitemap":
        "https://www.sitemaps.org/protocol.html",
    "Robots.txt":
        "https://developers.google.com/search/docs/crawling-indexing/robots/intro",
    "Structured Schema":
        "https://schema.org",
    "Entity Clarity":
        "https://schema.org/Organization",
    "FAQ Presence":
        "https://schema.org/FAQPage",
    "Trust / Consensus":
        "https://developers.google.com/search/docs/fundamentals/creating-helpful-content",
    "Content Freshness":
        "https://developers.google.com/search/docs/fundamentals/creating-helpful-content",
    "Keywords & Modifiers":
        "https://developers.google.com/search/docs/fundamentals/seo-starter-guide",
}

_MAX_REFERENCE_ROWS = 14


@dataclass(frozen=True)
class _Finding:
    """One deduplicated, site-wide issue with severity, impact and effort."""

    title: str
    severity: int
    impact: str
    effort: str
    evidence: str
    affected_count: int


class PowerPointGenerator:
    """Builds the 8-slide executive .pptx report from a completed AuditResult."""

    def generate_presentation(self, audit_result: AuditResult, output_path: str) -> str:
        """Build the full deck and save it to output_path. Returns the path."""
        prs = Presentation()
        prs.slide_width = SLIDE_WIDTH
        prs.slide_height = SLIDE_HEIGHT

        seo_foundation, semantic_avgs, semantic_aeo, final_aeo, citation_avg = (
            self._compute_aeo_final(audit_result)
        )
        final_grade = self._grade_from_score(final_aeo)
        seo_grade = self._grade_from_score(audit_result.overall_seo_score)
        overall_grade = self._grade_from_score((audit_result.overall_seo_score + final_aeo) / 2)

        self._build_slide_1(prs, audit_result, seo_grade, final_aeo, final_grade, overall_grade)
        self._build_slide_2(prs, audit_result, seo_grade)
        self._build_slide_3(
            prs, audit_result, seo_foundation, semantic_avgs, semantic_aeo,
            final_aeo, final_grade, citation_avg,
        )
        self._build_slide_4(prs, audit_result)
        self._build_slide_5(prs, audit_result)
        self._build_slide_6(prs, audit_result)
        self._build_slide_7(prs, audit_result)
        self._build_slide_8(prs, audit_result)

        return self._save(prs, output_path)
    def generate(self, audit_result: AuditResult, output_path: str = "SEO_AEO_Report.pptx") -> str:
        return self.generate_presentation(audit_result, output_path)

    # ------------------------------------------------------------------
    # Slide 1 — Executive Summary
    # ------------------------------------------------------------------
    def _build_slide_1(
        self,
        prs: Presentation,
        audit_result: AuditResult,
        seo_grade: str,
        final_aeo: float,
        final_grade: str,
        overall_grade: str,
    ) -> None:
        slide = self._blank_slide(prs)
        self._add_title(slide, "Executive Summary")

        site_url = audit_result.pages[0].url if audit_result.pages else "N/A"
        self._add_textbox(
            slide, Inches(0.6), Inches(1.15), Inches(12.1), Inches(0.4),
            site_url, size=Pt(14), color=COLOR_SLATE,
        )
        self._add_textbox(
            slide, Inches(0.6), Inches(1.55), Inches(12.1), Inches(0.35),
            f"{len(audit_result.pages)} page(s) audited", size=Pt(12), color=COLOR_SLATE,
        )

        card_y = Inches(2.3)
        card_w = Inches(2.85)
        card_h = Inches(2.2)
        gap = Inches(0.25)
        cards = [
            ("SEO Score", f"{audit_result.overall_seo_score:.1f}", seo_grade),
            ("Final AEO Score", f"{final_aeo:.1f}", final_grade),
            ("Overall Grade", overall_grade, ""),
            ("Pages Audited", str(len(audit_result.pages)), ""),
        ]
        x = Inches(0.6)
        for label, value, sub in cards:
            self._add_kpi_card(slide, x, card_y, card_w, card_h, label, value, sub)
            x = Emu(x + card_w + gap)

        self._add_textbox(
            slide, Inches(0.6), Inches(4.9), Inches(12.1), Inches(1.8),
            "The AEO score is derived, not independent: it combines the site's SEO "
            "foundation (40% weight) with a six-part semantic readiness assessment "
            "(60% weight) covering structured data, entity clarity, FAQ presence, "
            "trustworthiness, content freshness, and keyword modifiers. See Slide 3 "
            "for the full breakdown.",
            size=Pt(12), color=COLOR_SLATE, italic=True,
        )

    # ------------------------------------------------------------------
    # Slide 2 — SEO Score Breakdown
    # ------------------------------------------------------------------
    def _build_slide_2(
        self, prs: Presentation, audit_result: AuditResult, seo_grade: str
    ) -> None:
        slide = self._blank_slide(prs)
        self._add_title(slide, "SEO Score Breakdown")
        self._add_textbox(
            slide, Inches(0.6), Inches(1.1), Inches(10.0), Inches(0.4),
            f"Overall SEO Grade: {seo_grade}  ({audit_result.overall_seo_score:.1f} / 100)",
            size=Pt(14), color=COLOR_NAVY, bold=True,
        )

        total_pages = len(audit_result.seo_results) or 1
        headers = ["Metric", "Avg. Score"]
        rows = []

        for label, attr in _SEO_METRICS:
            values = [getattr(r, attr) for r in audit_result.seo_results.values()]
            avg = sum(values) / len(values) if values else 0.0
            rows.append([label, f"{avg:.1f}"])
            self._add_score_table(
                slide, headers, rows,
                x=Inches(0.6), y=Inches(1.7),
                width=Inches(6.2), height=Inches(5.3),
                col_widths=[Inches(4.2), Inches(2.0)],
            )

    # ------------------------------------------------------------------
    # Slide 3 — AEO Score Breakdown
    # ------------------------------------------------------------------
    def _build_slide_3(
        self,
        prs: Presentation,
        audit_result: AuditResult,
        seo_foundation: float,
        semantic_avgs: dict[str, float],
        semantic_aeo: float,
        final_aeo: float,
        final_grade: str,
        citation_avg: float,
    ) -> None:
        slide = self._blank_slide(prs)
        self._add_title(slide, "AEO Score Breakdown")

        left_x = Inches(0.6)
        left_w = Inches(6.0)

        self._add_textbox(
            slide, left_x, Inches(1.1), left_w, Inches(0.35),
            "Section 1 — SEO Foundation (40% weight)", size=Pt(13), color=COLOR_NAVY, bold=True,
        )
        self._add_textbox(
            slide, left_x, Inches(1.48), left_w, Inches(0.35),
            f"SEO Score: {audit_result.overall_seo_score:.1f} / 100", size=Pt(11), color=COLOR_SLATE,
        )
        self._add_textbox(
            slide, left_x, Inches(1.8), left_w, Inches(0.35),
            f"Weighted Contribution: {seo_foundation:.1f} / 40", size=Pt(11), color=COLOR_SLATE, bold=True,
        )

        self._add_textbox(
            slide, left_x, Inches(2.35), left_w, Inches(0.35),
            "Section 2 — Semantic AEO (60% weight)", size=Pt(13), color=COLOR_NAVY, bold=True,
        )
        y = Inches(2.73)
        for label in _SEMANTIC_METRIC_LABELS:
            raw = semantic_avgs.get(label, 0.0)
            weighted = raw / 100 * 10
            self._add_textbox(
                slide, left_x, y, left_w, Inches(0.3),
                f"{label}: {raw:.0f}/100  →  {weighted:.1f}/10", size=Pt(11), color=COLOR_SLATE,
            )
            y = Emu(y + Inches(0.32))

        self._add_textbox(
            slide, left_x, y + Inches(0.1), left_w, Inches(0.35),
            f"Semantic AEO Subtotal: {semantic_aeo:.1f} / 60", size=Pt(11), color=COLOR_SLATE, bold=True,
        )

        final_y = y + Inches(0.6)
        self._add_kpi_card(slide,left_x,final_y,Inches(5.95),Inches(1.6),"Final AEO Score",f"{final_aeo:.1f}",final_grade,)

        radar_x = Inches(7.1)
        radar_y = Inches(1.1)
        radar_size = Inches(5.6)
        radar_values = [round(semantic_avgs.get(label, 0.0) / 100 * 10, 1) for label in _SEMANTIC_METRIC_LABELS]
        self._add_radar_chart(slide, list(_SEMANTIC_METRIC_LABELS), radar_values, radar_x, radar_y, radar_size)

    # ------------------------------------------------------------------
    # Slide 4 — Critical Findings
    # ------------------------------------------------------------------
    def _build_slide_4(self, prs: Presentation, audit_result: AuditResult) -> None:
        slide = self._blank_slide(prs)
        self._add_title(slide, "Critical Findings")

        findings = self._collect_critical_findings(audit_result)[:5]
        if not findings:
            self._add_textbox(
                slide, Inches(0.6), Inches(1.3), Inches(12.1), Inches(0.5),
                "No critical issues detected across the audited pages.",
                size=Pt(14), color=COLOR_SLATE,
            )
            return

        card_h = Inches(1.05)
        gap = Inches(0.12)
        y = Inches(1.15)
        for finding in findings:
            self._add_finding_card(slide, Inches(0.6), y, Inches(12.1), card_h, finding)
            y = Emu(y + card_h + gap)

    # ------------------------------------------------------------------
    # Slide 5 — Short-Term Fixes
    # ------------------------------------------------------------------
    def _build_slide_5(self, prs: Presentation, audit_result: AuditResult) -> None:
        slide = self._blank_slide(prs)
        self._add_title(slide, "Short-Term Fixes (0-30 Days)")
        items = audit_result.recommendations.short_term if audit_result.recommendations else []
        self._add_recommendation_cards(slide, items)

    # ------------------------------------------------------------------
    # Slide 6 — Medium-Term Fixes
    # ------------------------------------------------------------------
    def _build_slide_6(self, prs: Presentation, audit_result: AuditResult) -> None:
        slide = self._blank_slide(prs)
        self._add_title(slide, "Medium-Term Fixes (30-60 Days)")
        items = audit_result.recommendations.medium_term if audit_result.recommendations else []
        self._add_recommendation_cards(slide, items)

    # ------------------------------------------------------------------
    # Slide 7 — Long-Term Strategy
    # ------------------------------------------------------------------
    def _build_slide_7(self, prs: Presentation, audit_result: AuditResult) -> None:
        slide = self._blank_slide(prs)
        self._add_title(slide, "Long-Term Strategy (60-90+ Days)")

        strategy_defaults: dict[str, str] = {
            "Topical Authority": "Expand depth and breadth of coverage on core topics to "
                                  "compound organic and AI-citation authority over time.",
            "Entity Clarity": "Reinforce unambiguous brand, product, and people entities "
                               "sitewide so AI systems can confidently attribute content.",
            "Freshness": "Establish a recurring content refresh cadence to keep priority "
                         "pages current for both ranking algorithms and answer engines.",
            "Brand Consistency": "Standardize brand naming, tone, and presentation across "
                                  "all pages to strengthen entity recognition.",
            "Shareable Visual Assets": "Invest in original charts, diagrams, and branded "
                                        "imagery that earn backlinks and social shares.",
            "AI Visibility": "Monitor citation appearances across AI answer engines monthly "
                              "and iterate content based on which pages get cited.",
        }

        long_term_items = audit_result.recommendations.long_term if audit_result.recommendations else []
        matched: dict[str, RecommendationItem] = {}
        for category in strategy_defaults:
            key_terms = category.lower().split()
            for item in long_term_items:
                if any(term in item.title.lower() for term in key_terms):
                    matched[category] = item
                    break

        col_w = Inches(3.9)
        row_h = Inches(2.9)
        gaps_x = Inches(0.2)
        gaps_y = Inches(0.2)
        x0 = Inches(0.6)
        y0 = Inches(1.15)

        for i, (category, default_text) in enumerate(strategy_defaults.items()):
            col = i % 3
            row = i // 3
            x = Emu(x0 + col * (col_w + gaps_x))
            y = Emu(y0 + row * (row_h + gaps_y))
            item = matched.get(category)
            description = item.description if item else default_text
            affected = len(item.affected_urls) if item else 0
            self._add_strategy_card(slide, x, y, col_w, row_h, category, description, affected)

    # ------------------------------------------------------------------
    # Slide 8 — Evidence & References
    # ------------------------------------------------------------------
    def _build_slide_8(self, prs: Presentation, audit_result: AuditResult) -> None:
        slide = self._blank_slide(prs)
        self._add_title(slide, "Evidence & References")

        rows = []

        # SEO
        for url, seo_result in audit_result.seo_results.items():
            for i, issue in enumerate(seo_result.issues):
                evidence = seo_result.evidence[i] if i < len(seo_result.evidence) else ""

                reference = "-"
                for metric, ref_url in REFERENCE_URLS.items():
                    if metric.lower() in issue.lower():
                        reference = ref_url
                        break

                rows.append([url, issue, evidence, reference])

        # AEO
        for url, aeo_result in audit_result.aeo_results.items():
            for i, issue in enumerate(aeo_result.issues):
                evidence = aeo_result.evidence[i] if i < len(aeo_result.evidence) else ""

            reference = "-"
            for metric, ref_url in REFERENCE_URLS.items():
                if metric.lower() in issue.lower():
                    reference = ref_url
                    break

            rows.append([url, issue, evidence, reference])

        self._add_reference_table(
            slide,
            ["Affected URL", "Deducted Metric", "Evidence", "Reference"],
            rows[:14],
            x=Inches(0.6),
            y=Inches(1.1),
            width=Inches(12.1),
            height=Inches(5.6),
            col_widths=[Inches(3.2), Inches(2.6), Inches(3.4), Inches(2.9)],
        )
    # ==================================================================
    # Required modular helper methods
    # ==================================================================
    def _add_title(self, slide: Slide, text: str) -> None:
        """Add the standard navy slide headline used across every slide."""
        box = slide.shapes.add_textbox(Inches(0.6), Inches(0.35), Inches(12.1), Inches(0.7))
        tf = box.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        run = p.add_run()
        run.text = text
        run.font.size = Pt(28)
        run.font.bold = True
        run.font.color.rgb = COLOR_NAVY
        run.font.name = FONT_FAMILY

        rule = slide.shapes.add_shape(1, Inches(0.6), Inches(1.02), Inches(12.1), Pt(2.2))
        rule.fill.solid()
        rule.fill.fore_color.rgb = COLOR_BLUE
        rule.line.fill.background()
        rule.shadow.inherit = False

    def _add_kpi_card(
        self, slide: Slide, x, y, width, height, label: str, value: str, sub: str = ""
    ) -> None:
        """Draw one blue-accent KPI card: top accent bar, big value, label, optional sub-value."""
        card = slide.shapes.add_shape(5, x, y, width, height)  # 5 = ROUNDED_RECTANGLE
        card.fill.solid()
        card.fill.fore_color.rgb = COLOR_LIGHT_BLUE
        card.line.color.rgb = COLOR_BORDER_GRAY
        card.line.width = Pt(0.75)
        card.shadow.inherit = False

        accent = slide.shapes.add_shape(1, x, y, width, Pt(6))  # 1 = RECTANGLE
        accent.fill.solid()
        accent.fill.fore_color.rgb = COLOR_BLUE
        accent.line.fill.background()
        accent.shadow.inherit = False

        value_box = slide.shapes.add_textbox(x, Emu(y + Inches(0.35)), width, Inches(0.9))
        vtf = value_box.text_frame
        vtf.word_wrap = True
        vp = vtf.paragraphs[0]
        vp.alignment = PP_ALIGN.CENTER
        vrun = vp.add_run()
        vrun.text = value
        vrun.font.size = Pt(30)
        vrun.font.bold = True
        vrun.font.color.rgb = COLOR_NAVY
        vrun.font.name = FONT_FAMILY

        label_box = slide.shapes.add_textbox(x, Emu(y + height - Inches(0.65)), width, Inches(0.35))
        ltf = label_box.text_frame
        ltf.word_wrap = True
        lp = ltf.paragraphs[0]
        lp.alignment = PP_ALIGN.CENTER
        lrun = lp.add_run()
        lrun.text = label
        lrun.font.size = Pt(12)
        lrun.font.color.rgb = COLOR_SLATE
        lrun.font.name = FONT_FAMILY

        if sub:
            sub_box = slide.shapes.add_textbox(x, Emu(y + height - Inches(0.32)), width, Inches(0.3))
            stf = sub_box.text_frame
            stf.word_wrap = True
            sp = stf.paragraphs[0]
            sp.alignment = PP_ALIGN.CENTER
            srun = sp.add_run()
            srun.text = f"Grade {sub}" if len(sub) <= 2 else sub
            srun.font.size = Pt(11)
            srun.font.bold = True
            srun.font.color.rgb = COLOR_BLUE
            srun.font.name = FONT_FAMILY

    def _add_score_table(
        self, slide: Slide, headers: list[str], rows: list[list[str]],
        x, y, width, height, col_widths: list,
    ) -> None:
        """Generic styled table: navy header row, zebra-striped body rows."""
        n_rows = len(rows) + 1
        n_cols = len(headers)
        graphic_frame = slide.shapes.add_table(n_rows, n_cols, x, y, width, height)
        table = graphic_frame.table

        for i, w in enumerate(col_widths):
            table.columns[i].width = w

        for c, header_text in enumerate(headers):
            cell = table.cell(0, c)
            cell.text = header_text
            cell.fill.solid()
            cell.fill.fore_color.rgb = COLOR_NAVY
            para = cell.text_frame.paragraphs[0]
            para.font.size = Pt(12)
            para.font.bold = True
            para.font.color.rgb = COLOR_WHITE
            para.font.name = FONT_FAMILY
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE

        for r, row_values in enumerate(rows, start=1):
            row_fill = COLOR_WHITE if r % 2 == 1 else COLOR_LIGHT_GRAY
            for c, cell_text in enumerate(row_values):
                cell = table.cell(r, c)
                cell.text = str(cell_text)
                cell.fill.solid()
                cell.fill.fore_color.rgb = row_fill
                para = cell.text_frame.paragraphs[0]
                para.font.size = Pt(10.5)
                para.font.color.rgb = COLOR_SLATE
                para.font.name = FONT_FAMILY
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                cell.margin_left = Emu(Inches(0.08))
                cell.margin_right = Emu(Inches(0.08))
                cell.margin_top = Emu(Inches(0.02))
                cell.margin_bottom = Emu(Inches(0.02))

    def _add_radar_chart(
        self, slide: Slide, labels: list[str], values_0_10: list[float], x, y, size,
    ) -> None:
        """Draw a hexagonal spider/radar chart using raw python-pptx freeform shapes."""
        n = len(labels)
        cx = Emu(x + size // 2)
        cy = Emu(y + size // 2)
        max_radius = size // 2 - Inches(0.9)

        # Background grid rings at 25/50/75/100% radius.
        for fraction in (0.25, 0.5, 0.75, 1.0):
            points = [
                self._polar_point(cx, cy, int(max_radius * fraction), i, n)
                for i in range(n)
            ]
            fb = slide.shapes.build_freeform(start_x=points[0][0], start_y=points[0][1], scale=1.0)
            fb.add_line_segments(points[1:] + [points[0]], close=True)
            ring = fb.convert_to_shape()
            ring.fill.background()
            ring.line.color.rgb = COLOR_BORDER_GRAY
            ring.line.width = Pt(0.75)
            ring.shadow.inherit = False

        # Axis spokes from center to each vertex.
        for i in range(n):
            end_x, end_y = self._polar_point(cx, cy, max_radius, i, n)
            connector = slide.shapes.add_connector(1, cx, cy, end_x, end_y)  # 1 = STRAIGHT
            connector.line.color.rgb = COLOR_BORDER_GRAY
            connector.line.width = Pt(0.75)

        # Data polygon.
        data_points = [
            self._polar_point(cx, cy, int(max_radius * min(v, 10) / 10), i, n)
            for i, v in enumerate(values_0_10)
        ]
        fb = slide.shapes.build_freeform(start_x=data_points[0][0], start_y=data_points[0][1], scale=1.0)
        fb.add_line_segments(data_points[1:] + [data_points[0]], close=True)
        data_shape = fb.convert_to_shape()
        data_shape.fill.solid()
        data_shape.fill.fore_color.rgb = COLOR_BLUE
        data_shape.line.color.rgb = COLOR_BLUE
        data_shape.line.width = Pt(1.75)
        data_shape.shadow.inherit = False

        # Axis labels with values, placed just outside the outer ring.
        for i, (label, value) in enumerate(zip(labels, values_0_10)):
            label_x, label_y = self._polar_point(cx, cy, max_radius + Inches(0.55), i, n)
            box = slide.shapes.add_textbox(
                Emu(label_x - Inches(1.1)), Emu(label_y - Inches(0.15)), Inches(2.2), Inches(0.35),
            )
            tf = box.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            p.alignment = PP_ALIGN.CENTER
            run = p.add_run()
            run.text = f"{label} ({value:.1f})"
            run.font.size = Pt(9.5)
            run.font.color.rgb = COLOR_SLATE
            run.font.name = FONT_FAMILY

    def _add_recommendation_cards(self, slide: Slide, items: list[RecommendationItem]) -> None:
        """Render up to 4 recommendation cards (title, description, badges, rationale)."""
        if not items:
            self._add_textbox(
                slide, Inches(0.6), Inches(1.3), Inches(12.1), Inches(0.5),
                "No recommendations triggered for this horizon.", size=Pt(14), color=COLOR_SLATE,
            )
            return

        card_h = Inches(1.45)
        gap = Inches(0.15)
        y = Inches(1.15)
        for item in items[:4]:
            self._add_single_recommendation_card(slide, Inches(0.6), y, Inches(12.1), card_h, item)
            y = Emu(y + card_h + gap)

    def _add_reference_table(
        self, slide: Slide, headers: list[str], rows: list[list[str]],
        x, y, width, height, col_widths: list,
    ) -> None:
        """Evidence/reference table — thin styling variant of _add_score_table for dense data."""
        self._add_score_table(slide, headers, rows, x, y, width, height, col_widths)

    def _save(self, prs: Presentation, output_path: str) -> str:
        """Persist the presentation to disk and return the path."""
        prs.save(output_path)
        return output_path

    # ==================================================================
    # Supporting private helpers
    # ==================================================================
    def _blank_slide(self, prs: Presentation) -> Slide:
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        background = slide.shapes.add_shape(1, 0, 0, prs.slide_width, prs.slide_height)
        background.fill.solid()
        background.fill.fore_color.rgb = COLOR_WHITE
        background.line.fill.background()
        background.shadow.inherit = False
        slide.shapes._spTree.remove(background._element)
        slide.shapes._spTree.insert(2, background._element)
        return slide

    def _add_textbox(
        self, slide: Slide, x, y, width, height, text: str,
        size: Pt = Pt(12), color: RGBColor = COLOR_SLATE, bold: bool = False, italic: bool = False,
    ):
        box = slide.shapes.add_textbox(x, y, width, height)
        tf = box.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        run = p.add_run()
        run.text = text
        run.font.size = size
        run.font.bold = bold
        run.font.italic = italic
        run.font.color.rgb = color
        run.font.name = FONT_FAMILY
        return box

    def _polar_point(self, cx, cy, radius, index: int, total: int) -> tuple[Emu, Emu]:
        """Return the (x, y) EMU point for axis `index` of `total`, starting at top (12 o'clock)."""
        import math

        angle_deg = -90 + (360 / total) * index
        angle_rad = math.radians(angle_deg)
        px = Emu(int(cx + radius * math.cos(angle_rad)))
        py = Emu(int(cy + radius * math.sin(angle_rad)))
        return px, py

    def _add_finding_card(self, slide: Slide, x, y, width, height, finding: _Finding) -> None:
        card = slide.shapes.add_shape(5, x, y, width, height)
        card.fill.solid()
        card.fill.fore_color.rgb = COLOR_LIGHT_GRAY
        card.line.color.rgb = COLOR_BORDER_GRAY
        card.line.width = Pt(0.75)
        card.shadow.inherit = False

        sev_color = COLOR_RED if finding.severity >= 8 else (COLOR_AMBER if finding.severity >= 5 else COLOR_GREEN)
        badge = slide.shapes.add_shape(5, Emu(x + width - Inches(1.15)), Emu(y + Inches(0.12)), Inches(0.95), Inches(0.35))
        badge.fill.solid()
        badge.fill.fore_color.rgb = sev_color
        badge.line.fill.background()
        badge.shadow.inherit = False
        btf = badge.text_frame
        btf.word_wrap = False
        bp = btf.paragraphs[0]
        bp.alignment = PP_ALIGN.CENTER
        brun = bp.add_run()
        brun.text = f"Sev {finding.severity}/10"
        brun.font.size = Pt(10)
        brun.font.bold = True
        brun.font.color.rgb = COLOR_WHITE
        brun.font.name = FONT_FAMILY

        self._add_textbox(
            slide, Emu(x + Inches(0.2)), Emu(y + Inches(0.08)), Emu(width - Inches(1.4)), Inches(0.35),
            finding.title, size=Pt(13), color=COLOR_NAVY, bold=True,
        )
        detail = (
            f"Impact: {finding.impact}   |   Effort: {finding.effort}   |   "
            f"Affected URLs: {finding.affected_count}"
        )
        self._add_textbox(
            slide, Emu(x + Inches(0.2)), Emu(y + Inches(0.44)), Emu(width - Inches(0.4)), Inches(0.28),
            detail, size=Pt(10.5), color=COLOR_SLATE,
        )
        evidence_text = finding.evidence or "Detected during automated crawl."
        self._add_textbox(
            slide, Emu(x + Inches(0.2)), Emu(y + Inches(0.72)), Emu(width - Inches(0.4)), Inches(0.3),
            f"Evidence: {evidence_text}", size=Pt(10), color=COLOR_SLATE, italic=True,
        )

    def _add_single_recommendation_card(
        self, slide: Slide, x, y, width, height, item: RecommendationItem
    ) -> None:
        card = slide.shapes.add_shape(5, x, y, width, height)
        card.fill.solid()
        card.fill.fore_color.rgb = COLOR_LIGHT_GRAY
        card.line.color.rgb = COLOR_BORDER_GRAY
        card.line.width = Pt(0.75)
        card.shadow.inherit = False

        accent = slide.shapes.add_shape(1, x, y, Pt(5), height)
        accent.fill.solid()
        accent.fill.fore_color.rgb = COLOR_BLUE
        accent.line.fill.background()
        accent.shadow.inherit = False

        self._add_textbox(
            slide, Emu(x + Inches(0.2)), Emu(y + Inches(0.08)), Emu(width - Inches(0.4)), Inches(0.3),
            item.title, size=Pt(13), color=COLOR_NAVY, bold=True,
        )
        self._add_textbox(
            slide, Emu(x + Inches(0.2)), Emu(y + Inches(0.4)), Emu(width - Inches(0.4)), Inches(0.32),
            item.description, size=Pt(10.5), color=COLOR_SLATE,
        )

        severity = self._priority_to_severity(item.priority)
        badges = (
            f"Priority: {item.priority}   |   Impact: {item.impact}   |   "
            f"Effort: {item.effort}   |   Severity: {severity}/10   |   "
            f"Affected URLs: {len(item.affected_urls)}"
        )
        self._add_textbox(
            slide, Emu(x + Inches(0.2)), Emu(y + Inches(0.74)), Emu(width - Inches(0.4)), Inches(0.28),
            badges, size=Pt(10), color=COLOR_BLUE, bold=True,
        )
        self._add_textbox(
            slide, Emu(x + Inches(0.2)), Emu(y + Inches(1.02)), Emu(width - Inches(0.4)), Inches(0.28),
            f"Evidence: {item.rationale}", size=Pt(9.5), color=COLOR_SLATE, italic=True,
        )

        if "keyword" in item.title.lower() or "keyword" in item.description.lower():
            self._add_textbox(
                slide, Emu(x + Inches(0.2)), Emu(y + height - Inches(0.02)), Emu(width - Inches(0.4)), Inches(0.01),
                "", size=Pt(1),
            )

    def _add_strategy_card(
        self, slide: Slide, x, y, width, height, category: str, description: str, affected: int
    ) -> None:
        card = slide.shapes.add_shape(5, x, y, width, height)
        card.fill.solid()
        card.fill.fore_color.rgb = COLOR_LIGHT_BLUE
        card.line.color.rgb = COLOR_BORDER_GRAY
        card.line.width = Pt(0.75)
        card.shadow.inherit = False

        self._add_textbox(
            slide, Emu(x + Inches(0.18)), Emu(y + Inches(0.15)), Emu(width - Inches(0.36)), Inches(0.4),
            category, size=Pt(13), color=COLOR_NAVY, bold=True,
        )
        self._add_textbox(
            slide, Emu(x + Inches(0.18)), Emu(y + Inches(0.6)), Emu(width - Inches(0.36)), Emu(height - Inches(1.0)),
            description, size=Pt(10.5), color=COLOR_SLATE,
        )
        if affected:
            self._add_textbox(
                slide, Emu(x + Inches(0.18)), Emu(y + height - Inches(0.35)), Emu(width - Inches(0.36)), Inches(0.28),
                f"Affects {affected} page(s)", size=Pt(9.5), color=COLOR_BLUE, bold=True,
            )

    # ------------------------------------------------------------------
    # Scoring computation
    # ------------------------------------------------------------------
    def _compute_aeo_final(
        self, audit_result: AuditResult
    ) -> tuple[float, dict[str, float], float, float, float]:
        """Compute SEO Foundation, Semantic averages, Semantic subtotal, Final AEO, citation avg."""
        aeo_values = list(audit_result.aeo_results.values())

        def _avg(attr: str) -> float:
            if not aeo_values:
                return 0.0
            return sum(getattr(r, attr) for r in aeo_values) / len(aeo_values)

        semantic_avgs = {
            "Structured Schema": _avg("structured_schema_score"),
            "Entity Clarity": _avg("entity_clarity_score"),
            "FAQ Presence": _avg("faq_score"),
            "Trustworthiness": _avg("trustworthiness_score"),
            "Freshness": _avg("freshness_score"),
            "Keywords & Modifiers": _avg("keyword_modifier_score"),
        }
        citation_avg = _avg("citation_readiness_score")

        seo_foundation = audit_result.overall_seo_score * 0.40
        semantic_aeo = sum(value / 100 * 10 for value in semantic_avgs.values())
        final_aeo = seo_foundation + semantic_aeo

        return seo_foundation, semantic_avgs, semantic_aeo, final_aeo, citation_avg

    def _grade_from_score(self, score: float) -> str:
        if score >= 85:
            return "A"
        if score >= 70:
            return "B"
        if score >= 50:
            return "C"
        return "D"

    # ------------------------------------------------------------------
    # Critical findings synthesis
    # ------------------------------------------------------------------
    def _collect_critical_findings(self, audit_result: AuditResult) -> list[_Finding]:
        total_pages = len(audit_result.pages) or max(
            len(audit_result.seo_results), len(audit_result.aeo_results), 1
        )

        aggregate: dict[str, dict] = {}
        for url, seo_result in audit_result.seo_results.items():
            self._accumulate_issues(aggregate, url, seo_result.issues, seo_result.evidence)
        for url, aeo_result in audit_result.aeo_results.items():
            self._accumulate_issues(aggregate, url, aeo_result.issues, aeo_result.evidence)

        findings: list[_Finding] = []
        for title, data in aggregate.items():
            affected_count = len(data["urls"])
            severity = self._classify_severity(title, affected_count, total_pages)
            impact = "High" if severity >= 8 else ("Medium" if severity >= 5 else "Low")
            effort = self._classify_effort(title)
            evidence = data["evidence"] or ""
            findings.append(_Finding(
                title=title, severity=severity, impact=impact, effort=effort,
                evidence=evidence, affected_count=affected_count,
            ))

        findings.sort(key=lambda f: f.severity, reverse=True)
        return findings

    def _accumulate_issues(
        self, aggregate: dict[str, dict], url: str, issues: list[str], evidence: list[str]
    ) -> None:
        for i, issue in enumerate(issues):
            entry = aggregate.setdefault(issue, {"urls": set(), "evidence": ""})
            entry["urls"].add(url)
            if not entry["evidence"] and i < len(evidence) and evidence[i]:
                entry["evidence"] = evidence[i]

    def _classify_severity(self, issue_text: str, affected_count: int, total_pages: int) -> int:
        text = issue_text.lower()
        if any(k in text for k in ("noindex", "broken link", "missing title", "duplicate title")):
            base = 9
        elif any(k in text for k in ("missing meta description", "missing canonical", "missing h1")):
            base = 7
        elif any(k in text for k in ("missing faq", "missing schema", "weak heading", "internal linking")):
            base = 6
        elif "alt" in text:
            base = 5
        elif any(k in text for k in ("entity clarity", "trust", "freshness", "topical authority", "brand")):
            base = 4
        else:
            base = 3

        if total_pages > 0 and (affected_count / total_pages) >= 0.5:
            base = min(10, base + 1)
        return base

    def _classify_effort(self, issue_text: str) -> str:
        text = issue_text.lower()
        if any(k in text for k in ("title", "meta description", "alt", "canonical", "h1", "robots.txt", "sitemap")):
            return "Low"
        if any(k in text for k in ("heading", "internal linking", "schema", "faq", "keyword", "comparison")):
            return "Medium"
        return "High"

    def _priority_to_severity(self, priority: str) -> int:
        return {"High": 8, "Medium": 5, "Low": 3}.get(priority, 5)