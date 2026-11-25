"""
email_agent.py
Enterprise AI Website Intelligence Agent — stakeholder email delivery via MCP.

Workflow position:
    Recommendation Engine → PPT Generator → Email Agent (MCP)

This module communicates with an MCP (Model Context Protocol) email server
over stdio. It never opens a raw SMTP socket itself: Gmail delivery is
performed by the configured MCP server, which is authenticated with the
sender's Gmail address and app password.

Expected MCP tool contract (discovered dynamically at runtime):
    tool name : any tool matching "send" + "mail" (preferred: "send_email")
    arguments : {
        "sender_email": str,
        "app_password":   str,
        "to":             list[str],
        "subject":        str,
        "body":           str,          # plain text
        "attachment_path": str          # optional; local .pptx path
    }

Email contents:
    Subject : Website Intelligence Audit Completed
    Body    : Overall health, Top 3 risks, Top 3 opportunities,
              link to the PPT, and a Chat-with-AI deep link.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

CHAT_BASE_URL = "https://company.local/chat"
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class EmailDeliveryResult:
    """Outcome of an attempted report email delivery via MCP.

    Attributes
    ----------
    success:
        True only if the MCP email tool accepted the delivery request.
    recipients:
        Final deduplicated, validated recipient list.
    subject:
        Subject line used for the message.
    sent_at:
        ISO-8601 timestamp of the attempt.
    attachment_path:
        Local path of the PowerPoint handed to the MCP server.
    ppt_link:
        Link to the PPT included in the email body.
    chat_link:
        Chat-with-AI deep link included in the email body.
    message:
        Human-readable status or error description.
    """

    success: bool = False
    recipients: list[str] = field(default_factory=list)
    subject: str = ""
    sent_at: str = ""
    attachment_path: str = ""
    ppt_link: str = ""
    chat_link: str = ""
    message: str = ""


# ---------------------------------------------------------------------------
# MCP client helpers
# ---------------------------------------------------------------------------
def _run_sync(coro: Any) -> Any:
    """Run an async MCP coroutine from sync code, in any environment."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    # A loop is already running (e.g., Jupyter); execute on a fresh thread.
    result: dict[str, Any] = {}

    def _worker() -> None:
        result["value"] = asyncio.run(coro)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join()
    return result.get("value")


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
class EmailAgent:
    """Delivers the executive audit report through an MCP email server.

    Parameters
    ----------
    server_command:
        Executable used to launch the MCP email server (stdio transport).
    server_args:
        Arguments for the server command.
    env:
        Extra environment variables for the server process (e.g., Gmail
        OAuth material the server itself may need).
    request_timeout:
        Per-call timeout in seconds for MCP tool invocations.
    """

    SUBJECT = "Website Intelligence Audit Completed"

    def __init__(
        self,
        server_command: str = "python",
        server_args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
        request_timeout: float = 60.0,
    ) -> None:
        self.server_command = server_command
        self.server_args = server_args or ["mcp_gmail_server.py"]
        self.env = env or {}
        self.request_timeout = request_timeout

    # ------------------------------------------------------------------ API
    def send_report(
        self,
        sender_email: str,
        app_password: str,
        recipients: list[str],
        audit: Any,
        ppt_path: str,
        chat_session_id: str,
        ppt_link: Optional[str] = None,
    ) -> EmailDeliveryResult:
        """Send the audit summary email (with PPT) through the MCP server.

        Parameters
        ----------
        sender_email:
            Gmail address used as the sender identity.
        app_password:
            Gmail app password (16-character app-specific credential),
            forwarded to the MCP email server.
        recipients:
            One or more stakeholder addresses; duplicates are removed.
        audit:
            Audit payload (dict or object) with seo/aeo scores, grade,
            critical issues, and recommendations.
        ppt_path:
            Local filesystem path of the generated .pptx.
        chat_session_id:
            Session id used to build the Chat-with-AI deep link
            (``https://company.local/chat/{id}``).
        ppt_link:
            Public URL of the PPT placed in the email body. Defaults to
            ``ppt_path`` when the deck is served from a known location.

        Returns
        -------
        EmailDeliveryResult
            Structured outcome; ``success`` is False with a descriptive
            ``message`` on any validation or MCP failure.
        """
        sent_at = datetime.now().isoformat(timespec="seconds")
        chat_link = f"{CHAT_BASE_URL}/{chat_session_id}" if chat_session_id else CHAT_BASE_URL
        ppt_url = ppt_link or ppt_path or ""

        # ---- sender / credential validation ------------------------------
        if not self._is_valid_email(sender_email):
            return EmailDeliveryResult(
                sent_at=sent_at, attachment_path=ppt_path or "",
                ppt_link=ppt_url, chat_link=chat_link,
                message="Invalid sender email address.",
            )
        if not app_password or not app_password.strip():
            return EmailDeliveryResult(
                sent_at=sent_at, attachment_path=ppt_path or "",
                ppt_link=ppt_url, chat_link=chat_link,
                message="Gmail app password is required.",
            )

        # ---- recipient validation / dedupe --------------------------------
        clean_recipients = self._dedupe_valid(recipients)
        if not clean_recipients:
            return EmailDeliveryResult(
                sent_at=sent_at, attachment_path=ppt_path or "",
                ppt_link=ppt_url, chat_link=chat_link,
                message="No valid recipient email addresses were provided.",
            )

        # ---- attachment validation ----------------------------------------
        if ppt_path and not os.path.isfile(ppt_path):
            return EmailDeliveryResult(
                recipients=clean_recipients, sent_at=sent_at,
                attachment_path=ppt_path, ppt_link=ppt_url, chat_link=chat_link,
                message=f"PowerPoint file not found: {ppt_path}",
            )

        # ---- build and send -------------------------------------------------
        body = self._build_body(audit, ppt_url, chat_link)
        arguments = {
            "sender_email": sender_email.strip(),
            "app_password": app_password.strip(),
            "to": clean_recipients,
            "subject": self.SUBJECT,
            "body": body,
            "attachment_path": ppt_path or "",
        }

        ok, info = _run_sync(self._dispatch(arguments))
        if not ok:
            return EmailDeliveryResult(
                recipients=clean_recipients, subject=self.SUBJECT,
                sent_at=sent_at, attachment_path=ppt_path or "",
                ppt_link=ppt_url, chat_link=chat_link, message=info,
            )

        return EmailDeliveryResult(
            success=True, recipients=clean_recipients, subject=self.SUBJECT,
            sent_at=sent_at, attachment_path=ppt_path or "",
            ppt_link=ppt_url, chat_link=chat_link,
            message=f"Report emailed successfully to {len(clean_recipients)} "
                    f"recipient(s) via MCP.",
        )

    # ------------------------------------------------------------- MCP layer
    async def _dispatch(self, arguments: dict[str, Any]) -> tuple[bool, str]:
        """Discover the email tool on the MCP server and invoke it."""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            return False, (
                "The 'mcp' package is not installed. "
                "Install it with: pip install mcp"
            )

        env = {**os.environ, **self.env}
        params = StdioServerParameters(
            command=self.server_command,
            args=self.server_args,
            env=env,
        )

        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(
                    read, write, read_timeout_seconds=timedelta(seconds=self.request_timeout)
                ) as session:
                    await session.initialize()

                    tools = await session.list_tools()
                    tool_name = self._select_email_tool(
                        [t.name for t in tools.tools]
                    )
                    if tool_name is None:
                        return False, (
                            "No email-sending tool found on the MCP server. "
                            "Expected a tool like 'send_email'."
                        )

                    result = await session.call_tool(tool_name, arguments)
                    if result.isError:
                        text = "".join(
                            c.text for c in result.content if hasattr(c, "text")
                        )
                        return False, f"MCP tool '{tool_name}' failed: {text}"
                    return True, f"Delivered via MCP tool '{tool_name}'."
        except FileNotFoundError:
            return False, (
                f"MCP server command not found: {self.server_command!r}. "
                "Check server_command/server_args."
            )
        except Exception as exc:
            if hasattr(exc, "exceptions"):
                detail = "; ".join(repr(e) for e in exc.exceptions)
            else:
                detail = repr(exc)
            return False, f"MCP communication failed: {detail}"

    @staticmethod
    def _select_email_tool(tool_names: list[str]) -> Optional[str]:
        for name in tool_names:
            if name == "send_email":
                return name
        for name in tool_names:
            lowered = name.lower()
            if "send" in lowered and ("mail" in lowered or "email" in lowered):
                return name
        return None

    # ---------------------------------------------------------- payload build
    def _build_body(self, audit: Any, ppt_url: str, chat_link: str) -> str:
        seo_score = self._fmt(self._audit_get(audit, "seo_score", "overall_seo_score"))
        aeo_score = self._fmt(self._audit_get(audit, "aeo_score", "overall_aeo_score"))
        grade = self._audit_get(audit, "overall_grade", "grade") or "Not graded"
        pages = self._audit_get(audit, "pages_crawled", "total_pages")
        pages_txt = f"{pages}" if isinstance(pages, (int, float)) else "N/A"

        risks = self._top_risks(audit)
        opportunities = self._top_opportunities(audit)

        lines: list[str] = [
            "Dear Stakeholder,",
            "",
            "The website intelligence audit has completed. "
            "Key results are summarized below; the full executive deck is "
            "available at the link provided.",
            "",
            "1. Overall Health",
            "-----------------",
            f"Overall SEO Score : {seo_score}",
            f"Overall AEO Score : {aeo_score}",
            f"Overall Grade     : {grade}",
            f"Pages Analyzed    : {pages_txt}",
            "",
            "2. Top 3 Risks",
            "--------------",
        ]
        lines.extend(f"{i}. {r}" for i, r in enumerate(risks, 1))
        lines.extend([
            "",
            "3. Top 3 Opportunities",
            "----------------------",
        ])
        lines.extend(f"{i}. {o}" for i, o in enumerate(opportunities, 1))
        lines.extend([
            "",
            "4. Executive Deck (PowerPoint)",
            "------------------------------",
            ppt_url or "Contact the analytics team for the deck.",
            "",
            "Chat with AI about this report:",
            chat_link,
            "",
            "Best regards,",
            "Enterprise AI Website Intelligence Agent",
        ])
        return "\n".join(lines)

    def _top_risks(self, audit: Any) -> list[str]:
        critical = self._audit_get(audit, "critical_issues")
        items = critical if isinstance(critical, list) else self._as_list(critical)
        risks: list[str] = []
        for item in items[:3]:
            risks.append(self._rec_text(item) if not isinstance(item, str) else item)
        if risks:
            return risks

        # Fallback: highest-severity recommendations as the risk narrative.
        recs = self._audit_get(audit, "recommendations")
        all_items: list = []
        if recs is not None:
            for attr in ("short_term", "medium_term", "long_term"):
                all_items.extend(self._as_list(getattr(recs, attr, None)))
        if all_items:
            ranked = sorted(
                all_items, key=lambda i: getattr(i, "severity", 0), reverse=True
            )
            return [self._rec_text(i) for i in ranked[:3]]

        # Fallback: lowest-scoring pages as the risk narrative.
        for p in self._as_list(self._audit_get(audit, "top_problem_pages"))[:3]:
            if isinstance(p, dict):
                risks.append(
                    f"{p.get('url', 'Page')} — SEO score "
                    f"{self._fmt(self._audit_get(p, 'score', 'seo_score'))} "
                    f"(grade {p.get('grade') or '—'})"
                )
        return risks or ["No material risks identified by the audit."]

    def _top_opportunities(self, audit: Any) -> list[str]:
        recs = self._audit_get(audit, "recommendations")
        for attr in ("short_term", "medium_term"):
            items = self._as_list(getattr(recs, attr, None) if recs is not None else None)
            if items:
                return [self._rec_text(i) for i in items[:3]]
        factors = self._audit_get(audit, "aeo_factor_averages", "factors") or {}
        if isinstance(factors, dict) and factors:
            weakest = sorted(factors.items(), key=lambda kv: kv[1])[:3]
            return [
                f"Improve {k.replace('_', ' ').title()} (current: {v}) "
                f"for the largest AEO gains."
                for k, v in weakest
            ]
        return ["No structured opportunities returned by the audit."]

    # -------------------------------------------------------------- utilities
    @staticmethod
    def _is_valid_email(address: Any) -> bool:
        return isinstance(address, str) and bool(EMAIL_RE.match(address.strip()))

    def _dedupe_valid(self, recipients: Any) -> list[str]:
        seen: set[str] = set()
        clean: list[str] = []
        for raw in self._as_list(recipients):
            addr = raw.strip().lower() if isinstance(raw, str) else ""
            if addr and self._is_valid_email(addr) and addr not in seen:
                seen.add(addr)
                clean.append(addr)
        return clean

    @staticmethod
    def _audit_get(audit: Any, *keys: str) -> Any:
        for key in keys:
            if isinstance(audit, dict) and audit.get(key) is not None:
                return audit[key]
            if not isinstance(audit, dict) and hasattr(audit, key):
                value = getattr(audit, key)
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
    def _rec_text(item: Any) -> str:
        if isinstance(item, dict):
            head = item.get("title") or item.get("issue") or item.get("heading") or "Item"
            body = item.get("detail") or item.get("description") or item.get("action") or ""
            return f"{head} — {body}" if body else str(head)
        if hasattr(item, "title"):
            head = getattr(item, "title", None) or "Item"
            body = getattr(item, "description", None) or getattr(item, "rationale", None) or ""
            return f"{head} — {body}" if body else str(head)
        return str(item)

    @staticmethod
    def _fmt(value: Any) -> str:
        if isinstance(value, bool) or value is None:
            return "Not available"
        if isinstance(value, (int, float)):
            return f"{value:.0f}/100" if value == int(value) else f"{value:.1f}/100"
        return str(value)