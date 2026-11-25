"""
scheduler.py

Monthly orchestration agent for the SEO + AEO Website Intelligence Agent.

Responsibilities:
    * Store scheduled website audits (JSON persistence)
    * Load schedule configuration
    * Decide whether an audit should run on a given day
    * Trigger the end-to-end pipeline (placeholder orchestration)
    * Return audit status

Designed to be invoked once per month by an external runner (e.g. GitHub Actions).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Final

logger: Final[logging.Logger] = logging.getLogger(__name__)

MIN_RUN_DAY: Final[int] = 1
MAX_RUN_DAY: Final[int] = 31

PIPELINE_STAGES: Final[tuple[str, ...]] = (
    "Crawler Agent",
    "Feature Engine",
    "SEO Engine",
    "AEO Engine",
    "Recommendation Engine",
    "PPT Engine",
    "Email Agent",
)


class ScheduleError(Exception):
    """Raised when a schedule file is missing, malformed, or invalid."""


@dataclass(slots=True)
class ScheduledAudit:
    """A single scheduled website audit."""

    website_url: str
    run_day: int
    recipients: list[str] = field(default_factory=list)
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialise the audit to a JSON-compatible dictionary."""
        return asdict(self)


class SchedulerAgent:
    """Monthly orchestration agent for website audits."""

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def load_schedule(self, config_path: str) -> list[ScheduledAudit]:
        """Read a JSON schedule file and return the audits it contains.

        Accepts either a top-level list of audit objects or an object with
        an ``"audits"`` key holding that list.
        """
        path = Path(config_path)
        if not path.is_file():
            raise ScheduleError(f"Schedule file not found: {path}")

        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ScheduleError(f"Invalid JSON in {path}: {exc}") from exc

        entries = self._extract_entries(raw)
        audits = [self._parse_audit(entry, index) for index, entry in enumerate(entries)]
        logger.info("Loaded %d scheduled audit(s) from %s", len(audits), path)
        return audits

    def save_schedule(self, audits: list[ScheduledAudit], config_path: str) -> None:
        """Write the given audits to ``config_path`` as JSON."""
        for index, audit in enumerate(audits):
            self._validate_audit(audit, index)

        path = Path(config_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        payload: dict[str, Any] = {"audits": [audit.to_dict() for audit in audits]}
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Saved %d scheduled audit(s) to %s", len(audits), path)

    # ------------------------------------------------------------------
    # Scheduling logic
    # ------------------------------------------------------------------
    def should_run(self, audit: ScheduledAudit, today: date) -> bool:
        """Return True if the audit is enabled and today matches its run day.

        For months shorter than the configured day (e.g. run_day=31 in
        April), the audit runs on the last day of that month so it is
        never skipped.
        """
        if not audit.enabled:
            return False
        effective_day = min(audit.run_day, self._days_in_month(today))
        return today.day == effective_day

    # ------------------------------------------------------------------
    # Pipeline orchestration
    # ------------------------------------------------------------------
    def run_pipeline(self, audit: ScheduledAudit) -> dict[str, Any]:
        """Run the full audit pipeline for one website (placeholder orchestration).

        Execution order:
            Crawler Agent
                ↓
            Feature Engine
                ↓
            SEO Engine
                ↓
            AEO Engine
                ↓
            Recommendation Engine
                ↓
            PPT Engine
                ↓
            Email Agent
        """
        execution_date = date.today()
        logger.info("Starting pipeline for %s", audit.website_url)

        stage_handlers: tuple[tuple[str, Callable[[ScheduledAudit], None]], ...] = (
            (PIPELINE_STAGES[0], self._run_crawler_agent),
            (PIPELINE_STAGES[1], self._run_feature_engine),
            (PIPELINE_STAGES[2], self._run_seo_engine),
            (PIPELINE_STAGES[3], self._run_aeo_engine),
            (PIPELINE_STAGES[4], self._run_recommendation_engine),
            (PIPELINE_STAGES[5], self._run_ppt_engine),
            (PIPELINE_STAGES[6], self._run_email_agent),
        )

        status = "completed"
        for stage_name, handler in stage_handlers:
            try:
                logger.info("Stage started: %s", stage_name)
                handler(audit)
                logger.info("Stage finished: %s", stage_name)
            except Exception:
                logger.exception("Stage failed: %s", stage_name)
                status = f"failed:{stage_name}"
                break

        return {
            "status": status,
            "website": audit.website_url,
            "execution_date": execution_date.isoformat(),
            "recipients": list(audit.recipients),

              # Generated artifacts
            "audit_result": None,
            "ppt_path": None,
            "chat_session_id": None,
}


    def run_due_audits(
        self, config_path: str, today: date | None = None
    ) -> list[dict[str, Any]]:
        """Load the schedule and run every audit that is due today."""
        effective_today = today or date.today()
        audits = self.load_schedule(config_path)
        results: list[dict[str, Any]] = []
        for audit in audits:
            if self.should_run(audit, effective_today):
                results.append(self.run_pipeline(audit))
            else:
                logger.info("Skipping %s (not due today)", audit.website_url)
        return results

    # ------------------------------------------------------------------
    # Pipeline stage placeholders
    # ------------------------------------------------------------------
    def _run_crawler_agent(self, audit: ScheduledAudit) -> None:
        logger.info("Crawler Agent placeholder executed for %s", audit.website_url)

    def _run_feature_engine(self, audit: ScheduledAudit) -> None:
        logger.info("Feature Engine placeholder executed for %s", audit.website_url)

    def _run_seo_engine(self, audit: ScheduledAudit) -> None:
        logger.info("SEO Engine placeholder executed for %s", audit.website_url)

    def _run_aeo_engine(self, audit: ScheduledAudit) -> None:
        logger.info("AEO Engine placeholder executed for %s", audit.website_url)

    def _run_recommendation_engine(self, audit: ScheduledAudit) -> None:
        logger.info("Recommendation Engine placeholder executed for %s", audit.website_url)

    def _run_ppt_engine(self, audit: ScheduledAudit) -> None:
        logger.info("PPT Engine placeholder executed for %s", audit.website_url)

    def _run_email_agent(self, audit: ScheduledAudit) -> None:
        logger.info(
            "Email Agent placeholder executed for %s (%d recipient(s))",
            audit.website_url,
            len(audit.recipients),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_entries(raw: Any) -> list[Any]:
        """Normalise the JSON root into a list of raw audit entries."""
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict) and isinstance(raw.get("audits"), list):
            return raw["audits"]
        raise ScheduleError(
            "Schedule JSON must be a list of audits or an object with an 'audits' list."
        )

    def _parse_audit(self, entry: Any, index: int) -> ScheduledAudit:
        """Convert one raw JSON entry into a validated ScheduledAudit."""
        if not isinstance(entry, dict):
            raise ScheduleError(f"Audit #{index} must be a JSON object.")

        missing = [key for key in ("website_url", "run_day") if key not in entry]
        if missing:
            raise ScheduleError(f"Audit #{index} is missing required field(s): {', '.join(missing)}")

        recipients_raw = entry.get("recipients", [])
        if not isinstance(recipients_raw, list):
            raise ScheduleError(f"Audit #{index}: 'recipients' must be a list of strings.")

        audit = ScheduledAudit(
            website_url=str(entry["website_url"]).strip(),
            run_day=self._coerce_day(entry["run_day"], index),
            recipients=[str(item).strip() for item in recipients_raw if str(item).strip()],
            enabled=bool(entry.get("enabled", True)),
        )
        self._validate_audit(audit, index)
        return audit

    @staticmethod
    def _coerce_day(value: Any, index: int) -> int:
        """Convert a raw run_day value to int, rejecting booleans and non-integers."""
        if isinstance(value, bool):
            raise ScheduleError(f"Audit #{index}: 'run_day' must be an integer.")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ScheduleError(f"Audit #{index}: 'run_day' must be an integer.") from exc

    @staticmethod
    def _validate_audit(audit: ScheduledAudit, index: int) -> None:
        """Raise ScheduleError if the audit contains invalid values."""
        if not audit.website_url:
            raise ScheduleError(f"Audit #{index}: 'website_url' must not be empty.")
        if not MIN_RUN_DAY <= audit.run_day <= MAX_RUN_DAY:
            raise ScheduleError(
                f"Audit #{index}: 'run_day' must be between {MIN_RUN_DAY} and {MAX_RUN_DAY}."
            )

    @staticmethod
    def _days_in_month(value: date) -> int:
        """Return the number of days in the month of the given date."""
        if value.month == 12:
            next_month_start = date(value.year + 1, 1, 1)
        else:
            next_month_start = date(value.year, value.month + 1, 1)
        return (next_month_start - date(value.year, value.month, 1)).days