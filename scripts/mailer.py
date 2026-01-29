import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import List, Optional

def send_email(subject: str, body: str, attachments: Optional[List[Path]] = None) -> None:
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    email_from = os.environ["EMAIL_FROM"]
    email_to = os.environ["EMAIL_TO"]

    msg = EmailMessage()
    msg["From"] = email_from
    msg["To"] = email_to
    msg["Subject"] = subject
    msg.set_content(body)

    for p in attachments or []:
        data = p.read_bytes()
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=p.name)

    with smtplib.SMTP(smtp_host, smtp_port) as s:
        s.starttls()
        s.login(smtp_user, smtp_pass)
        s.send_message(msg)
