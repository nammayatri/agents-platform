"""Notification system for alerting humans via Slack, email, or webhook.

The orchestrator triggers notifications when:
- A task is stuck (agent making no progress)
- A task fails after max retries
- A task completes
- A task needs human review
"""

import asyncio
import json
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import asyncpg
import httpx

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, db: asyncpg.Pool):
        self.db = db
        self.http = httpx.AsyncClient(timeout=10)

    async def notify(self, user_id: str, event: str, payload: dict) -> None:
        """Send notifications to all configured channels for this event type."""
        channels = await self.db.fetch(
            """
            SELECT * FROM notification_channels
            WHERE user_id = $1 AND is_active = TRUE AND $2 = ANY(notify_on)
            """,
            user_id,
            event,
        )
        for channel in channels:
            config = (
                json.loads(channel["config_json"])
                if isinstance(channel["config_json"], str)
                else channel["config_json"]
            )
            try:
                match channel["channel_type"]:
                    case "slack":
                        await self._send_slack(config, event, payload)
                    case "email":
                        await self._send_email(config, event, payload)
                    case "webhook":
                        await self._send_webhook(config, event, payload)
            except Exception as e:
                logger.error(
                    "Failed to send %s notification via %s: %s",
                    event,
                    channel["channel_type"],
                    e,
                )

    async def _send_slack(self, config: dict, event: str, payload: dict) -> None:
        """Send Slack notification via webhook or API."""
        webhook_url = config.get("webhook_url")
        if not webhook_url:
            logger.warning("Slack channel missing webhook_url")
            return

        message = self._format_message(event, payload)
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": message},
            }
        ]

        await self.http.post(
            webhook_url,
            json={"text": message, "blocks": blocks},
        )

    async def _get_email_config(self) -> dict:
        """Load admin-configured email settings from system_settings table."""
        row = await self.db.fetchrow(
            "SELECT value_json FROM system_settings WHERE key = 'email'"
        )
        if not row:
            return {}
        val = row["value_json"]
        if isinstance(val, str):
            return json.loads(val)
        return val

    async def _send_email(self, config: dict, event: str, payload: dict) -> None:
        """Send email notification.

        The recipient address comes from the user's channel config.
        The sending account (SMTP/API credentials) comes from admin
        system_settings under the 'email' key.
        """
        to_email = config.get("email")
        if not to_email:
            return

        email_cfg = await self._get_email_config()
        if not email_cfg:
            logger.warning(
                "Email to %s skipped — no email provider configured by admin "
                "(Settings > Email Config)",
                to_email,
            )
            return

        subject = f"[Agents] {event.upper()}: {payload.get('title', 'Task Update')}"
        body = self._format_message(event, payload)

        # Mode 1: SMTP (e.g. Gmail with app password)
        smtp_host = email_cfg.get("smtp_host")
        smtp_user = email_cfg.get("smtp_user")
        smtp_password = email_cfg.get("smtp_password")
        if smtp_host and smtp_user and smtp_password:
            smtp_port = int(email_cfg.get("smtp_port", 587))
            from_email = email_cfg.get("from_email") or smtp_user
            from_name = email_cfg.get("from_name") or "Agents Platform"

            def _send_smtp():
                msg = MIMEMultipart("alternative")
                msg["From"] = f"{from_name} <{from_email}>"
                msg["To"] = to_email
                msg["Subject"] = subject
                msg.attach(MIMEText(body, "plain"))

                # Simple HTML version
                html_body = body.replace("\n", "<br>")
                html = (
                    "<html><body style='font-family:sans-serif;color:#333'>"
                    f"{html_body}</body></html>"
                )
                msg.attach(MIMEText(html, "html"))

                with smtplib.SMTP(smtp_host, smtp_port) as server:
                    server.ehlo()
                    if smtp_port != 25:
                        server.starttls()
                        server.ehlo()
                    server.login(smtp_user, smtp_password)
                    server.sendmail(from_email, [to_email], msg.as_string())

            await asyncio.to_thread(_send_smtp)
            logger.info("Email sent via SMTP to %s for event %s", to_email, event)
            return

        # Mode 2: HTTP API (SendGrid, etc.)
        api_url = email_cfg.get("api_url")
        api_key = email_cfg.get("api_key")
        if api_url and api_key:
            from_email = email_cfg.get("from_email", "noreply@agents.local")
            await self.http.post(
                api_url,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "to": to_email,
                    "from": from_email,
                    "subject": subject,
                    "text": body,
                },
            )
            logger.info("Email sent via API to %s for event %s", to_email, event)
            return

        logger.warning(
            "Email to %s skipped — admin email config incomplete (need SMTP or API credentials)",
            to_email,
        )

    async def _send_webhook(self, config: dict, event: str, payload: dict) -> None:
        """Send generic webhook notification."""
        url = config.get("url")
        if not url:
            return

        headers = config.get("headers", {})
        await self.http.post(
            url,
            headers=headers,
            json={"event": event, "payload": payload},
        )

    def _format_message(self, event: str, payload: dict) -> str:
        title = payload.get("title", "Unknown Task")
        todo_id = payload.get("todo_id", "?")
        detail = payload.get("detail", "")

        match event:
            case "stuck":
                return (
                    f"*Task Stuck* | `{title}`\n"
                    f"Task `{todo_id[:8]}` has been stuck for a while. "
                    f"The agents need your help.\n{detail}"
                )
            case "failed":
                return (
                    f"*Task Failed* | `{title}`\n"
                    f"Task `{todo_id[:8]}` has failed after max retries.\n"
                    f"Error: {detail}"
                )
            case "completed":
                return (
                    f"*Task Completed* | `{title}`\n"
                    f"Task `{todo_id[:8]}` has been completed successfully.\n{detail}"
                )
            case "review":
                return (
                    f"*Review Needed* | `{title}`\n"
                    f"Task `{todo_id[:8]}` needs your review.\n{detail}"
                )
            case "build_started":
                return (
                    f"*Build Started* | `{title}`\n"
                    f"Task `{todo_id[:8]}` — CI/CD build has started after merge.\n{detail}"
                )
            case "build_completed":
                return (
                    f"*Build Completed* | `{title}`\n"
                    f"Task `{todo_id[:8]}` — Build completed successfully.\n{detail}"
                )
            case "build_failed":
                return (
                    f"*Build Failed* | `{title}`\n"
                    f"Task `{todo_id[:8]}` — CI/CD build failed.\n{detail}"
                )
            case "release_test_completed":
                return (
                    f"*Staging Deployed* | `{title}`\n"
                    f"Task `{todo_id[:8]}` — Staging release completed.\n{detail}"
                )
            case "release_test_failed":
                return (
                    f"*Staging Deploy Failed* | `{title}`\n"
                    f"Task `{todo_id[:8]}` — Staging release failed.\n{detail}"
                )
            case "release_prod_completed":
                return (
                    f"*Production Deployed* | `{title}`\n"
                    f"Task `{todo_id[:8]}` — Production release completed.\n{detail}"
                )
            case "release_prod_failed":
                return (
                    f"*Production Deploy Failed* | `{title}`\n"
                    f"Task `{todo_id[:8]}` — Production release failed.\n{detail}"
                )
            case _:
                return f"*{event.upper()}* | `{title}`\n{detail}"

    async def close(self) -> None:
        await self.http.aclose()
