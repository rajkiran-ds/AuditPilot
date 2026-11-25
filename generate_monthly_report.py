import os
import sys
import uuid

from crawler import Crawler
from seo import SEOEngine
from aeo import AEOEngine
from recommendation_engine import RecommendationEngine
from ppt import PowerPointGenerator
from email_agent import EmailAgent
from models import AuditResult

# Read GitHub Secrets
WEBSITE_URL = os.getenv("WEBSITE_URL")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

if not WEBSITE_URL:
    raise ValueError("WEBSITE_URL secret not found.")

print("Starting monthly audit...")

# 1. Crawl
crawler = Crawler(max_pages=20)
crawl_result = crawler.crawl(WEBSITE_URL)

# 2. SEO
seo_engine = SEOEngine()
seo_results, seo_score = seo_engine.audit_site(crawl_result)

# 3. AEO
aeo_engine = AEOEngine()
aeo_results, aeo_score = aeo_engine.audit_site(crawl_result, seo_results)

# 4. Recommendations
rec_engine = RecommendationEngine()
recommendations = rec_engine.generate_recommendations(
    seo_results,
    aeo_results,
)

def _letter_grade(score: float) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"

# 5. PPT
audit = AuditResult(
    pages=crawl_result.pages,
    seo_results=seo_results,
    aeo_results=aeo_results,
    recommendations=recommendations,
    overall_seo_score=seo_score,
    overall_aeo_score=aeo_score,
    overall_grade=_letter_grade((seo_score + aeo_score) / 2),
)
audit.pages_crawled = len(crawl_result.pages)
ppt = PowerPointGenerator()
ppt.generate(audit, "Executive_AI_Audit.pptx")

# 6. Email
email_agent = EmailAgent(
    server_command=sys.executable,
    server_args=["-u", "mcp_gmail_server.py"],
)
result = email_agent.send_report(
    sender_email=os.getenv("SENDER_EMAIL"),
    app_password=os.getenv("GMAIL_APP_PASSWORD"),
    recipients=[os.getenv("RECIPIENT_EMAIL")],
    audit=audit,
    ppt_path="Executive_AI_Audit.pptx",
    chat_session_id=str(uuid.uuid4())
)
print(result.message)
if not result.success:
    raise SystemExit(f"Email failed: {result.message}")