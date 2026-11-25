from mcp.server.fastmcp import FastMCP
import smtplib
import os
from email.message import EmailMessage

mcp = FastMCP("GmailServer")


@mcp.tool()
def send_email(
    sender_email: str,
    app_password: str,
    to: list[str],
    subject: str,
    body: str,
    attachment_path: str = "",
) -> str:
    """Send an email with an optional PPT attachment."""

    msg = EmailMessage()
    msg["From"] = sender_email
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)

    if attachment_path and os.path.exists(attachment_path):
        with open(attachment_path, "rb") as f:
            msg.add_attachment(
                f.read(),
                maintype="application",
                subtype="vnd.openxmlformats-officedocument.presentationml.presentation",
                filename=os.path.basename(attachment_path),
            )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender_email, app_password)
        smtp.send_message(msg)

    return f"Email sent successfully to {', '.join(to)}"


if __name__ == "__main__":
    mcp.run()