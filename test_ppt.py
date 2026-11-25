from models import AuditResult
from ppt import PowerPointGenerator

# Dummy audit for testing
audit = AuditResult(
    overall_seo_score=82,
    overall_aeo_score=81,
    overall_seo_grade="B",
    overall_aeo_grade="B",
    overall_grade="B"
)

ppt = PowerPointGenerator()

output = ppt.generate_presentation(
    audit_result=audit,
    output_path="sample_report.pptx"
)

print("Generated:", output)