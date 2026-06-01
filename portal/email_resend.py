"""Resend email client — admin notifications for portal signups.

Uses the Resend HTTP API directly (no SDK). The API key is read from
RESEND_API_KEY. Email-sending failures are logged but never crash the
caller; a missed notification email is not worth dropping a signup.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

import httpx

logger = logging.getLogger("portal.email_resend")

DEFAULT_FROM = os.environ.get("EMAIL_FROM", "SweGen PGx Portal <onboarding@resend.dev>")
RESEND_ENDPOINT = "https://api.resend.com/emails"

# Test-mode fallback. When the Resend account has no verified sender
# domain, Resend rejects all recipients except the account owner's
# verified address. If RESEND_VERIFIED_TO is set, the portal routes
# every transactional email there (with the original recipient list
# rendered in the body), so that admins still see notifications until
# a real domain is verified at resend.com/domains.
VERIFIED_TO = os.environ.get("RESEND_VERIFIED_TO", "").strip().lower()


async def send_email(
    api_key: str,
    to: Iterable[str] | str,
    subject: str,
    html: str,
    text: str | None = None,
    from_addr: str = DEFAULT_FROM,
) -> bool:
    if not api_key:
        logger.warning("RESEND_API_KEY not set — email skipped")
        return False
    intended = list(to) if not isinstance(to, str) else [to]

    # Resend test-mode workaround: when no domain is verified, route to the
    # account-owner address and announce the intended recipients in the body.
    if VERIFIED_TO and any((r or "").lower() != VERIFIED_TO for r in intended):
        recipients = [VERIFIED_TO]
        bcc_note = (
            "<hr/><p style='color:#777;font-size:12px'>"
            "<b>Resend test-mode notice.</b> This portal currently has no "
            "verified sender domain at resend.com, so notifications cannot "
            "be sent directly to all admins. They were intended for: "
            + ", ".join(intended)
            + ". Please forward this email to the other admins, or verify a "
            "domain at <a href='https://resend.com/domains'>resend.com/domains</a> "
            "and set <code>EMAIL_FROM</code> on the portal to a sender on that domain."
            "</p>"
        )
        html = html + bcc_note
        if text:
            text = text + "\n\n[Resend test-mode — intended for: " + ", ".join(intended) + "]"
    else:
        recipients = intended

    payload = {
        "from": from_addr,
        "to": recipients,
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                RESEND_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code >= 300:
                logger.warning(
                    f"Resend returned {resp.status_code}: {resp.text[:300]} "
                    f"(intended={intended}, sent={recipients}, from={from_addr})"
                )
                return False
            logger.info(f"Resend ok: sent to {recipients} (intended {intended})")
            return True
    except Exception as e:
        logger.warning(f"Resend send failed: {e}")
        return False


async def notify_admins_new_signup(
    api_key: str,
    admin_emails: Iterable[str],
    portal_base_url: str,
    user_email: str,
    user_name: str = "",
    reason: str = "",
) -> bool:
    subject = f"[SweGen PGx Portal] New access request: {user_email}"
    safe_name = (user_name or user_email.split("@")[0])
    safe_reason = (reason or "(no reason provided)")
    admin_url = f"{portal_base_url.rstrip('/')}/admin"
    html = f"""\
<div style="font-family:-apple-system,Inter,Arial,sans-serif;max-width:560px">
  <h2>New access request</h2>
  <p><b>{safe_name}</b> ({user_email}) is requesting access to the SweGen
  PGx Portal.</p>
  <p style="color:#555"><b>Reason given:</b><br/>{safe_reason}</p>
  <p>
    <a href="{admin_url}"
       style="display:inline-block;padding:10px 16px;background:#0f766e;
              color:white;text-decoration:none;border-radius:6px">
      Review in admin dashboard
    </a>
  </p>
  <hr/>
  <p style="color:#777;font-size:12px">
    You are receiving this because you are listed as a portal admin.
    Sent by the SweGen PGx Portal at {portal_base_url}.
  </p>
</div>
"""
    text = (
        f"New access request from {user_email} ({safe_name}).\n\n"
        f"Reason: {safe_reason}\n\n"
        f"Review at: {admin_url}\n"
    )
    return await send_email(
        api_key=api_key,
        to=list(admin_emails),
        subject=subject,
        html=html,
        text=text,
    )


async def notify_user_approved(
    api_key: str,
    user_email: str,
    portal_base_url: str,
) -> bool:
    subject = "[SweGen PGx Portal] Your access has been approved"
    portal_url = portal_base_url.rstrip("/")
    html = f"""\
<div style="font-family:-apple-system,Inter,Arial,sans-serif;max-width:560px">
  <h2>You are in</h2>
  <p>Your access to the <b>SweGen PGx Portal</b> has been approved.</p>
  <p>Sign in at
    <a href="{portal_url}">{portal_url}</a>
    and create your first session — you will get an agent URL that you
    can paste into Claude, Cursor, or any agent that supports HTTP tool use.</p>
  <p>Before you start, please re-read the responsible-use note on the
  portal landing page: do not paste sensitive data into your agent
  prompts, and be aware that prompt injection in retrieved content can
  cause your agent to issue requests you did not intend.</p>
  <p style="color:#777;font-size:12px">
    The Guardian Agent enforces the dataset sensitivity contract and
    every call is audited.
  </p>
</div>
"""
    return await send_email(
        api_key=api_key,
        to=user_email,
        subject=subject,
        html=html,
    )


async def notify_admins_new_report(
    api_key: str,
    admin_emails: Iterable[str],
    portal_base_url: str,
    report_id: str,
    title: str,
    author_email: str,
    author_name: str = "",
    description: str = "",
) -> bool:
    subject = f"[SweGen PGx Portal] New report submitted for review: {title[:80]}"
    safe_name = author_name or author_email.split("@")[0]
    safe_desc = description or "(no description provided)"
    admin_url = f"{portal_base_url.rstrip('/')}/admin#reports"
    preview_url = f"{portal_base_url.rstrip('/')}/admin/reports/{report_id}/preview"
    html = f"""\
<div style="font-family:-apple-system,Inter,Arial,sans-serif;max-width:560px">
  <h2>New community-report submission</h2>
  <p><b>{safe_name}</b> ({author_email}) has submitted a report for
  publication on the SweGen PGx Portal community page.</p>
  <p><b>Title:</b> {title}</p>
  <p style="color:#555"><b>Description:</b><br/>{safe_desc}</p>
  <p>
    <a href="{preview_url}"
       style="display:inline-block;padding:8px 14px;background:#0f766e;
              color:white;text-decoration:none;border-radius:6px;margin-right:8px">
      Preview report
    </a>
    <a href="{admin_url}"
       style="display:inline-block;padding:8px 14px;background:#1e293b;
              color:white;text-decoration:none;border-radius:6px">
      Admin dashboard
    </a>
  </p>
  <hr/>
  <p style="color:#777;font-size:12px">
    Reports are not visible on the public community page until an admin
    approves them. The preview link renders the HTML inside an isolated
    sandbox (no access to portal cookies or tokens).
  </p>
</div>
"""
    text = (
        f"New report submission: {title}\n"
        f"By: {author_email} ({safe_name})\n\n"
        f"Description: {safe_desc}\n\n"
        f"Preview: {preview_url}\n"
        f"Admin dashboard: {admin_url}\n"
    )
    return await send_email(
        api_key=api_key,
        to=list(admin_emails),
        subject=subject,
        html=html,
        text=text,
    )


async def notify_user_report_decision(
    api_key: str,
    user_email: str,
    portal_base_url: str,
    title: str,
    report_id: str,
    decision: str,
    reviewer_note: str = "",
) -> bool:
    portal_url = portal_base_url.rstrip("/")
    if decision == "approved":
        subject = f"[SweGen PGx Portal] Your report \"{title[:80]}\" was approved"
        view_url = f"{portal_url}/community#report-{report_id}"
        action = f"""\
<p>It is now visible to everyone on the
<a href="{portal_url}/community">community page</a>:</p>
<p>
  <a href="{view_url}"
     style="display:inline-block;padding:8px 14px;background:#15803d;
            color:white;text-decoration:none;border-radius:6px">
    Open my report
  </a>
</p>
"""
    else:
        subject = f"[SweGen PGx Portal] Your report \"{title[:80]}\" was not approved"
        action = f"""\
<p>Your report was not approved for publication. You can submit a
revised version through your AI agent at any time.</p>
"""
    note_block = ""
    if reviewer_note:
        note_block = (
            "<p style='background:#f4f4f5;padding:10px 14px;border-radius:6px'>"
            f"<b>Reviewer note:</b><br/>{reviewer_note}</p>"
        )
    html = f"""\
<div style="font-family:-apple-system,Inter,Arial,sans-serif;max-width:560px">
  <h2>Report decision: <b>{decision}</b></h2>
  <p>Your report <b>{title}</b> on the SweGen PGx Portal has been reviewed.</p>
  {action}
  {note_block}
  <hr/>
  <p style="color:#777;font-size:12px">
    SweGen PGx Portal — {portal_url}
  </p>
</div>
"""
    return await send_email(
        api_key=api_key,
        to=user_email,
        subject=subject,
        html=html,
    )
