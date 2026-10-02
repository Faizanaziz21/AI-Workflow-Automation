"""SMTP (async, aiosmtplib) and IMAP (stdlib imaplib, run in a thread) helpers."""

from __future__ import annotations

import asyncio
import email
import imaplib
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import getaddresses, make_msgid, parseaddr
from typing import Any

import aiosmtplib

from app.connectors.sdk import ConnectorError


async def smtp_send(cfg: Any, creds: Any, message: EmailMessage, recipients: list[str], timeout: float = 30) -> None:
    kwargs: dict[str, Any] = {"hostname": cfg.smtp_host, "port": cfg.smtp_port, "timeout": timeout}
    if cfg.smtp_security == "tls":
        kwargs["use_tls"] = True
    elif cfg.smtp_security == "starttls":
        kwargs["start_tls"] = True
    else:
        kwargs["start_tls"] = False
    if creds.username:
        kwargs["username"] = creds.username
        kwargs["password"] = creds.password or ""
    try:
        await aiosmtplib.send(message, recipients=recipients, **kwargs)
    except aiosmtplib.SMTPAuthenticationError as exc:
        raise ConnectorError(f"SMTP authentication failed: {exc.code}", retryable=False) from exc
    except aiosmtplib.SMTPRecipientsRefused as exc:
        raise ConnectorError("All recipients were refused", retryable=False) from exc
    except (aiosmtplib.SMTPConnectError, aiosmtplib.SMTPServerDisconnected, aiosmtplib.SMTPTimeoutError) as exc:
        raise ConnectorError(f"SMTP connection problem: {exc}", retryable=True) from exc
    except aiosmtplib.SMTPResponseException as exc:
        raise ConnectorError(
            f"SMTP error {exc.code}: {exc.message}", retryable=exc.code >= 400 and exc.code < 500
        ) from exc


async def smtp_check(cfg: Any, creds: Any, timeout: float = 15) -> str:
    client = aiosmtplib.SMTP(
        hostname=cfg.smtp_host,
        port=cfg.smtp_port,
        timeout=timeout,
        use_tls=cfg.smtp_security == "tls",
        start_tls=cfg.smtp_security == "starttls",
    )
    await client.connect()
    try:
        if creds.username:
            await client.login(creds.username, creds.password or "")
        code, _ = await client.noop()
        return f"SMTP server responded {code}"
    finally:
        client.close()


def build_message(
    *,
    from_address: str,
    from_name: str,
    to: list[str],
    cc: list[str],
    subject: str,
    text: str | None,
    html: str | None,
    reply_to: str | None,
    headers: dict[str, str],
    idempotency_key: str | None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{from_name} <{from_address}>" if from_name else from_address
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    domain = from_address.split("@", 1)[-1]
    # Deterministic Message-ID from the idempotency key lets receivers dedupe retried sends.
    msg["Message-ID"] = f"<{idempotency_key}@{domain}>" if idempotency_key else make_msgid(domain=domain)
    for k, v in headers.items():
        if k.lower() not in ("from", "to", "cc", "bcc", "subject", "message-id"):
            msg[k] = v
    msg.set_content(text or "")
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def parse_message(uid: int, raw: bytes) -> dict[str, Any]:
    msg: Message = email.message_from_bytes(raw)
    text_body, html_body = "", None
    attachments: list[dict[str, Any]] = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.is_multipart():
            continue
        disposition = (part.get("Content-Disposition") or "").lower()
        ctype = part.get_content_type()
        payload = part.get_payload(decode=True) or b""
        if "attachment" in disposition:
            attachments.append({"filename": _decode(part.get_filename()), "content_type": ctype, "size": len(payload)})
            continue
        charset = part.get_content_charset() or "utf-8"
        decoded = payload.decode(charset, errors="replace")
        if ctype == "text/plain" and not text_body:
            text_body = decoded
        elif ctype == "text/html" and html_body is None:
            html_body = decoded
    from_name, from_addr = parseaddr(_decode(msg.get("From")))
    return {
        "uid": uid,
        "message_id": msg.get("Message-ID"),
        "from_address": from_addr,
        "from_name": from_name or None,
        "to": [a for _, a in getaddresses([_decode(msg.get("To"))]) if a],
        "subject": _decode(msg.get("Subject")),
        "date": msg.get("Date"),
        "text": text_body[:100_000],
        "html": html_body[:200_000] if html_body else None,
        "attachments": attachments,
    }


def _imap_fetch_sync(
    cfg: Any, creds: Any, folder: str, last_uid: int, limit: int, mark_seen: bool
) -> list[dict[str, Any]]:
    conn: imaplib.IMAP4 = (
        imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port) if cfg.imap_ssl else imaplib.IMAP4(cfg.imap_host, cfg.imap_port)
    )
    try:
        conn.login(creds.username or "", creds.password or "")
        conn.select(folder, readonly=not mark_seen)
        status, data = conn.uid("search", None, f"UID {last_uid + 1}:*")
        if status != "OK":
            raise ConnectorError(f"IMAP search failed: {status}", retryable=True)
        uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > last_uid)[:limit]
        out = []
        for uid in uids:
            status, fetched = conn.uid("fetch", str(uid), "(RFC822)")
            if status != "OK" or not fetched or not isinstance(fetched[0], tuple):
                continue
            out.append(parse_message(uid, fetched[0][1]))
            if mark_seen:
                conn.uid("store", str(uid), "+FLAGS", "(\\Seen)")
        return out
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: S110 - best-effort logout
            pass


async def imap_fetch_new(
    cfg: Any, creds: Any, folder: str, last_uid: int, limit: int, mark_seen: bool
) -> list[dict[str, Any]]:
    try:
        return await asyncio.to_thread(_imap_fetch_sync, cfg, creds, folder, last_uid, limit, mark_seen)
    except imaplib.IMAP4.error as exc:
        raise ConnectorError(f"IMAP error: {exc}", retryable=False) from exc
    except OSError as exc:
        raise ConnectorError(f"IMAP connection failed: {exc}", retryable=True) from exc
