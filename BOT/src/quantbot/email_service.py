"""Admin-configured daily strategy email reminders over SMTP (QQ mail by default)."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.message import EmailMessage
from email.utils import formataddr

from sqlalchemy import select

from . import email_templates
from .db import transaction
from .models import DailyReport, EmailDelivery, EmailSubscription, User

log = logging.getLogger(__name__)

STRATEGY_IDS = {"1": "strategy1", "2": "strategy2"}
MAX_ATTEMPTS = 5
_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+$")


@dataclass(frozen=True)
class SmtpConfig:
    sender: str
    password: str
    host: str
    port: int

    @classmethod
    def from_env(cls) -> SmtpConfig | None:
        sender = os.getenv("EMAIL_SENDER", "").strip()
        password = os.getenv("EMAIL_PASSWORD", "").strip()
        if not sender or not password:
            return None
        return cls(sender, password, os.getenv("EMAIL_SMTP_HOST", "smtp.qq.com").strip(),
                   int(os.getenv("EMAIL_SMTP_PORT", "465")))


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 254 or not _EMAIL.match(email):
        raise ValueError("邮箱格式不正确。")
    return email


def parse_strategies(value: str) -> str:
    """Accept "1,2" / "1，2" / "2 1"; keep only known strategies, sorted and de-duplicated."""
    items = [x for x in re.split(r"[,，\s]+", value.strip()) if x]
    if not items:
        raise ValueError("请提供策略编号，例如 1,2。")
    unknown = [x for x in items if x not in STRATEGY_IDS]
    if unknown:
        raise ValueError(f"未知策略：{'、'.join(unknown)}；当前可选：{','.join(STRATEGY_IDS)}。")
    return ",".join(sorted(set(items), key=int))


def strategy_numbers(value: str) -> list[str]:
    return [x for x in value.split(",") if x in STRATEGY_IDS]


def html_to_text(body: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", body)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip() + "\n"


def compose(number: str, today: date, report_body: str | None, has_actions: bool | None) -> tuple[str, str]:
    """Fixed template when nothing to do; the user's full daily report otherwise."""
    fields = dict(date=today.isoformat(), strategy=number)
    if has_actions:
        return email_templates.ACTION_SUBJECT.format(**fields), html_to_text(report_body or "")
    return (email_templates.NO_ACTION_SUBJECT.format(**fields),
            email_templates.NO_ACTION_BODY.format(**fields))


def send(config: SmtpConfig, receiver: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["From"] = formataddr(("策略提醒", config.sender))
    message["To"] = receiver
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP_SSL(config.host, config.port, context=ssl.create_default_context(), timeout=30) as smtp:
        smtp.login(config.sender, config.password)
        smtp.send_message(message)


def pending(session, today: date) -> list[tuple[str, str, str, str | None, bool | None]]:
    """(email, number, strategy_id, body, has_actions) for each ready report not yet emailed."""
    out = []
    for sub in session.scalars(select(EmailSubscription).order_by(EmailSubscription.email)):
        user = session.get(User, sub.user_id)
        if not user or not user.is_active:
            continue
        for number in strategy_numbers(sub.strategies):
            strategy_id = STRATEGY_IDS[number]
            report = session.scalar(select(DailyReport).where(
                DailyReport.user_id == sub.user_id, DailyReport.strategy_id == strategy_id,
                DailyReport.session_date == today))
            if report is None or not report.data_ready:
                continue
            delivery = session.scalar(select(EmailDelivery).where(
                EmailDelivery.email == sub.email, EmailDelivery.strategy_id == strategy_id,
                EmailDelivery.session_date == today))
            if delivery and (delivery.sent_at or delivery.attempts >= MAX_ATTEMPTS):
                continue
            out.append((sub.email, number, strategy_id, report.body, report.has_actions))
    return out


async def deliver_emails(app, today: date, opening: datetime) -> None:
    config = app.bot_data.get("smtp")
    if config is None:
        return
    factory = app.bot_data["session_factory"]
    with transaction(factory) as session:
        jobs = pending(session, today)
    for email, number, strategy_id, body, has_actions in jobs:
        if datetime.now(UTC) >= opening:
            break  # Same rule as Telegram: never send an actionable premarket report after the open.
        subject, text = compose(number, today, body, has_actions)
        error = None
        try:
            await asyncio.to_thread(send, config, email, subject, text)
        except (smtplib.SMTPException, OSError) as exc:
            error = type(exc).__name__
            log.warning("email reminder failed: %s strategy %s: %s", email, number, error)
        with transaction(factory) as session:
            delivery = session.scalar(select(EmailDelivery).where(
                EmailDelivery.email == email, EmailDelivery.strategy_id == strategy_id,
                EmailDelivery.session_date == today))
            if delivery is None:
                if session.get(EmailSubscription, email) is None:
                    continue
                delivery = EmailDelivery(email=email, strategy_id=strategy_id, session_date=today, attempts=0)
                session.add(delivery)
            delivery.attempts += 1
            delivery.last_error = error
            if error is None:
                delivery.sent_at = datetime.now(UTC)
