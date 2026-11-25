"""
chat_agent.py
Enterprise AI Website Intelligence Agent — RAG-based conversational layer.

The Chat Agent is a retrieval-augmented chatbot over ONE generated audit.
It NEVER crawls websites, NEVER calculates new scores, and NEVER invents
information. Before answering, it retrieves the audit chunks most relevant
to the question (TF-IDF vector retrieval over a local, dependency-free
index) and grounds the Groq LLM answer strictly in those chunks.

Flow:
    app.py (chat page at /chat/{session_id})
        → ChatAgent.index_audit(seo, aeo, recommendations)
        → ChatAgent.ask(question)
            1. retrieve top-k audit chunks (RAG)
            2. Groq LLM answers from retrieved chunks only
            3. deterministic fallback if Groq is unavailable

If the answer is not present in the retrieved audit evidence, the agent
replies exactly: "That information is not available in the generated audit."
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional

_UNAVAILABLE = "That information is not available in the generated audit."
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


# ---------------------------------------------------------------------------
# Local TF-IDF retrieval index (no external services, fully deterministic)
# ---------------------------------------------------------------------------
@dataclass
class _Chunk:
    text: str
    source: str  # e.g. "seo", "aeo", "recommendations:short_term"


class _TfidfIndex:
    """Minimal in-memory TF-IDF index used for RAG retrieval."""

    def __init__(self) -> None:
        self._chunks: list[_Chunk] = []
        self._vectors: list[dict[str, float]] = []
        self._idf: dict[str, float] = {}
        self._norms: list[float] = []
        self._built = False

    def add(self, text: str, source: str) -> None:
        text = " ".join(str(text).split())
        if text:
            self._chunks.append(_Chunk(text=text, source=source))

    def build(self) -> None:
        docs = [_tokenize(c.text) for c in self._chunks]
        df: dict[str, int] = {}
        for tokens in docs:
            for term in set(tokens):
                df[term] = df.get(term, 0) + 1
        n = max(len(docs), 1)
        self._idf = {t: math.log((1 + n) / (1 + freq)) + 1.0 for t, freq in df.items()}

        self._vectors, self._norms = [], []
        for tokens in docs:
            tf: dict[str, int] = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1
            vec = {t: (count / len(tokens)) * self._idf[t] for t, count in tf.items()}
            norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
            self._vectors.append(vec)
            self._norms.append(norm)
        self._built = True

    def search(self, query: str, k: int = 6) -> list[str]:
        if not self._built or not self._chunks:
            return []
        q_tf: dict[str, int] = {}
        tokens = _tokenize(query)
        for t in tokens:
            q_tf[t] = q_tf.get(t, 0) + 1
        if not q_tf:
            return []
        q_vec = {
            t: (count / len(tokens)) * self._idf.get(t, 0.0)
            for t, count in q_tf.items()
        }
        q_norm = math.sqrt(sum(v * v for v in q_vec.values())) or 1.0

        scored = []
        for i, vec in enumerate(self._vectors):
            dot = sum(w * vec.get(t, 0.0) for t, w in q_vec.items())
            score = dot / (q_norm * self._norms[i])
            if score > 0:
                scored.append((score, i))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [self._chunks[i].text for _, i in scored[:k]]

    def __len__(self) -> int:
        return len(self._chunks)


# ---------------------------------------------------------------------------
# Session state for the /chat/{session_id} deep link
# ---------------------------------------------------------------------------
@dataclass
class ChatSession:
    """Server-side state for one Chat-with-AI deep link.

    ``app.py`` creates one ChatSession per audit, renders the chat UI at
    ``/chat/{session_id}``, and routes each question to ``agent.ask``.
    """

    session_id: str
    agent: "ChatAgent"
    seo_results: dict = field(default_factory=dict)
    aeo_results: dict = field(default_factory=dict)
    recommendations: dict = field(default_factory=dict)

    def ask(self, question: str) -> str:
        return self.agent.ask(
            question,
            seo_results=self.seo_results,
            aeo_results=self.aeo_results,
            recommendations=self.recommendations,
        )


# ---------------------------------------------------------------------------
# Chat Agent
# ---------------------------------------------------------------------------
class ChatAgent:
    """RAG-based conversational assistant over a generated website audit.

    Parameters
    ----------
    api_key:
        Groq API key. If empty or the Groq package is unavailable, the
        agent runs in deterministic fallback mode.
    model:
        Groq model identifier.
    top_k:
        Number of audit chunks retrieved per question.
    """

    DEFAULT_MODEL = "llama-3.3-70b-versatile"

    _SYSTEM_PROMPT = (
        "You are an Enterprise AI Website Intelligence Consultant.\n"
        "Strict rules:\n"
        "- You are given RETRIEVED audit evidence and a user question.\n"
        "- Answer ONLY from the retrieved evidence. NEVER crawl websites, "
        "NEVER calculate new scores, NEVER invent information.\n"
        "- If the evidence does not contain the answer, reply exactly: "
        f'"{_UNAVAILABLE}"\n'
        "- Give executive, business-style answers; be concise.\n"
        "- Use bullet points whenever listing items."
    )

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        top_k: int = 6,
    ) -> None:
        self.api_key: Optional[str] = (api_key or "").strip() or None
        self.model: str = model or self.DEFAULT_MODEL
        self.top_k = max(1, int(top_k))
        self._index = _TfidfIndex()
        self._context: dict[str, Any] = {}
        self._client: Any = None
        if self.api_key:
            try:
                from groq import Groq

                self._client = Groq(api_key=self.api_key)
            except Exception:
                self._client = None

    # ------------------------------------------------------------------ API
    def index_audit(
        self,
        seo_results: dict,
        aeo_results: dict,
        recommendations: dict,
    ) -> None:
        """Chunk the generated audit and build the retrieval index (RAG).

        Called once per audit — e.g., when the chat deep link is created in
        ``app.py`` after the pipeline completes.
        """
        self._index = _TfidfIndex()
        self._context = self._build_context(seo_results, aeo_results, recommendations)
        for text, source in self._make_chunks(self._context):
            self._index.add(text, source)
        self._index.build()

    def ask(
        self,
        question: str,
        seo_results: Optional[dict] = None,
        aeo_results: Optional[dict] = None,
        recommendations: Optional[dict] = None,
    ) -> str:
        """Answer a question using retrieval-augmented generation.

        If no audit has been indexed yet, one is indexed on the fly from the
        provided dictionaries. Answers are grounded only in retrieved audit
        chunks; if Groq is unavailable a deterministic fallback is used.
        """
        if not isinstance(question, str) or not question.strip():
            return "Please provide a question about the generated audit."

        if len(self._index) == 0 and any(
            x is not None for x in (seo_results, aeo_results, recommendations)
        ):
            self.index_audit(
                seo_results or {},
                aeo_results or {},
                recommendations or {},
            )

        if len(self._index) == 0:
            return _UNAVAILABLE

        evidence = self._index.search(question.strip(), k=self.top_k)

        if self._client is not None:
            try:
                return self._ask_llm(question.strip(), evidence)
            except Exception:
                return self._ask_deterministic(question.strip(), evidence)

        return self._ask_deterministic(question.strip(), evidence)

    # ------------------------------------------------------------- RAG → LLM
    def _ask_llm(self, question: str, evidence: list[str]) -> str:
        if not evidence:
            return _UNAVAILABLE
        context_block = "\n".join(f"- {e}" for e in evidence)
        messages = [
            {"role": "system", "content": self._SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Retrieved audit evidence:\n{context_block}\n\n"
                    f"Question: {question}"
                ),
            },
        ]
        response = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.2,
        )
        content = response.choices[0].message.content
        return content.strip() if isinstance(content, str) else str(content).strip()

    # ------------------------------------------------- deterministic fallback
    def _ask_deterministic(self, question: str, evidence: list[str]) -> str:
        q = question.lower()
        blob = " ".join(evidence).lower()

        if "critical" in q and "issue" in q:
            issues = [e for e in evidence if "critical" in e.lower()]
            return self._bulletize(issues, "Critical issues identified:")
        if "seo score" in q and ("why" in q or "low" in q):
            rows = [e for e in evidence if "seo score" in e.lower() or "grade" in e.lower()]
            return self._bulletize(rows or evidence[:4], "SEO performance evidence:")
        if "aeo score" in q and ("why" in q or "low" in q):
            rows = [e for e in evidence if "factor" in e.lower() or "aeo" in e.lower()]
            return self._bulletize(rows or evidence[:4], "AEO performance evidence:")
        if "immediate attention" in q or "which pages" in q or "urgent" in q:
            rows = [e for e in evidence if "page" in e.lower() or "http" in e.lower()]
            return self._bulletize(rows[:5], "Pages requiring attention:")
        if "30 day" in q or "30-day" in q or "quick win" in q:
            rows = [e for e in evidence if "short-term" in e.lower() or "short_term" in e.lower()]
            return self._bulletize(rows, "Focus areas for the next 30 days:")
        if "90 day" in q or "90-day" in q or "long-term" in q or "long term" in q:
            rows = [e for e in evidence if "long-term" in e.lower() or "long_term" in e.lower()]
            return self._bulletize(rows, "Long-term roadmap:")
        if "summar" in q or "recommend" in q or "roadmap" in q:
            rec_rows = [e for e in evidence if "recommend" in e.lower() or "-term" in e.lower()]
            return self._bulletize(rec_rows or evidence[:6], "Recommendations summary:")
        if "score" in q or "health" in q or "grade" in q:
            return self._bulletize(evidence[:5], "Key audit findings:")
        return self._bulletize(
            evidence[:5],
            "Here is the most relevant audit evidence I found:",
        ) if evidence else _UNAVAILABLE

    @staticmethod
    def _bulletize(items: list[str], heading: str) -> str:
        if not items:
            return _UNAVAILABLE
        return "\n".join([heading, ""] + [f"- {i}" for i in items])

    # ------------------------------------------------------- context & chunks
    def _build_context(
        self,
        seo_results: Any,
        aeo_results: Any,
        recommendations: Any,
    ) -> dict[str, Any]:
        seo = seo_results if isinstance(seo_results, dict) else {}
        aeo = aeo_results if isinstance(aeo_results, dict) else {}
        recs = recommendations if isinstance(recommendations, dict) else {}

        pages = self._as_list(self._get(seo, "pages", "page_audits", "results")) or (
            self._as_list(self._get(aeo, "pages", "page_audits", "results"))
        )

        critical: list[str] = []
        for audit in pages:
            if not isinstance(audit, dict):
                continue
            url = audit.get("url", "unknown page")
            for issue in self._as_list(audit.get("issues")):
                if isinstance(issue, dict) and str(issue.get("severity", "")).lower() == "critical":
                    label = issue.get("issue") or issue.get("title") or "Critical issue"
                    critical.append(f"{label} ({url})")

        problems = self._as_list(self._get(seo, "top_problem_pages"))
        if not problems:
            rows = []
            for audit in pages:
                if isinstance(audit, dict):
                    score = self._num(audit.get("score"))
                    if score is not None:
                        rows.append((score, audit.get("url", "unknown"), audit.get("grade")))
            rows.sort(key=lambda r: r[0])
            problems = [
                {"url": url, "score": score, "grade": grade}
                for score, url, grade in rows[:10]
            ]

        return {
            "overall_seo_score": self._get(seo, "overall_score", "seo_score", "score"),
            "overall_aeo_score": self._get(aeo, "overall_score", "aeo_score", "score"),
            "overall_grade": self._get(seo, "overall_grade", "grade"),
            "pages_crawled": self._get(seo, "pages_crawled", "total_pages") or len(pages),
            "critical_issues": critical,
            "top_problem_pages": problems,
            "aeo_factor_averages": self._get(aeo, "factor_averages", "criteria_averages", "factors") or {},
            "recommendations": recs,
        }

    def _make_chunks(self, ctx: dict[str, Any]) -> list[tuple[str, str]]:
        """Flatten the audit context into retrievable text chunks."""
        chunks: list[tuple[str, str]] = []

        def add(text: str, source: str) -> None:
            chunks.append((" ".join(str(text).split()), source))

        add(
            f"Overall SEO score: {ctx.get('overall_seo_score')}; "
            f"Overall AEO score: {ctx.get('overall_aeo_score')}; "
            f"Overall grade: {ctx.get('overall_grade')}; "
            f"Pages crawled: {ctx.get('pages_crawled')}.",
            "seo",
        )
        for issue in ctx.get("critical_issues", [])[:10]:
            add(f"Critical issue: {issue}", "seo")
        for p in self._as_list(ctx.get("top_problem_pages"))[:10]:
            if isinstance(p, dict):
                add(
                    f"Problem page: {p.get('url')} — SEO score "
                    f"{p.get('score', p.get('seo_score'))}, grade {p.get('grade') or '—'}.",
                    "seo",
                )
        factors = ctx.get("aeo_factor_averages") or {}
        if isinstance(factors, dict):
            for name, value in factors.items():
                add(
                    f"AEO factor {str(name).replace('_', ' ').title()}: {value}.",
                    "aeo",
                )

        recs = ctx.get("recommendations") or {}
        if isinstance(recs, dict):
            summary = recs.get("executive_summary") or recs.get("summary")
            if isinstance(summary, list):
                for s in summary:
                    add(f"Executive summary: {s}", "recommendations")
            elif summary:
                add(f"Executive summary: {summary}", "recommendations")
            for horizon, key in (("Short-term (30 days)", "short_term"),
                                 ("Mid-term (60 days)", "mid_term"),
                                 ("Long-term (90 days)", "long_term")):
                for item in self._as_list(recs.get(key))[:6]:
                    add(
                        f"{horizon} recommendation: {self._rec_text(item)}",
                        f"recommendations:{key}",
                    )
        return chunks

    # -------------------------------------------------------------- utilities
    @staticmethod
    def _get(d: Any, *keys: str) -> Any:
        if not isinstance(d, dict):
            return None
        for key in keys:
            value = d.get(key)
            if value is not None:
                return value
        return None

    @staticmethod
    def _as_list(value: Any) -> list:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    @staticmethod
    def _num(value: Any) -> Optional[float]:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            match = re.search(r"-?\d+(?:\.\d+)?", value)
            if match:
                return float(match.group())
        return None

    @staticmethod
    def _rec_text(item: Any) -> str:
        if isinstance(item, dict):
            head = item.get("title") or item.get("issue") or item.get("heading") or "Recommendation"
            body = item.get("detail") or item.get("description") or item.get("action") or ""
            return f"{head} — {body}" if body else str(head)
        return str(item)