"""Gmail client — read, send, label, and (the HITL spine) DRAFT.

Everything outbound in SpotDesk is born as a Gmail draft threaded into the right
conversation; a human reviews it inside Gmail and presses Send. This client provides
the full draft lifecycle: create_draft / delete_draft / send_draft / draft_exists,
plus the classic read/send/thread operations.

googleapiclient + httplib2 are NOT thread-safe: all API calls are serialized per
client (the poll loop, monitors, and dashboard share one instance).
"""

from __future__ import annotations

import base64
import logging
import os
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .. import config

log = logging.getLogger("spotdesk.gmail")

# Threads that 404'd (stale cross-mailbox ids) — warn once, not every poll cycle.
_WARNED_MISSING_THREADS: set[str] = set()


class GmailClient:
    def __init__(self,
                 credentials_file: str | None = None,
                 token_file: str | None = None,
                 scopes: list[str] | None = None):
        self.credentials_file = credentials_file or config.GOOGLE_CREDENTIALS_FILE
        self.token_file = token_file or config.GOOGLE_TOKEN_FILE
        self.scopes = scopes or config.GMAIL_SCOPES
        self.service = None
        self.user_email: str | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    def authenticate(self, interactive: bool = False):
        """interactive=False (agent/service path) never opens a browser: a dead token
        raises a clear error telling the operator to run `python main.py --setup`,
        instead of blocking forever on a headless server."""
        creds = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, self.scopes)

        if not creds or not creds.valid:
            refreshed = False
            if creds and creds.expired and creds.refresh_token:
                try:
                    log.info("refreshing expired Gmail token…")
                    creds.refresh(Request())
                    refreshed = True
                except Exception as e:
                    log.error("token refresh failed: %s", e)
                    creds = None
            if not refreshed and (not creds or not creds.valid):
                if not interactive:
                    raise RuntimeError(
                        "Gmail token missing/expired and cannot be refreshed. "
                        "Run `python main.py --setup` to (re)authenticate.")
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, self.scopes)
                creds = flow.run_local_server(port=0)
            Path(self.token_file).parent.mkdir(parents=True, exist_ok=True)
            Path(self.token_file).write_text(creds.to_json())
            try:
                os.chmod(self.token_file, 0o600)   # token holds a refresh token
            except OSError:
                pass

        self.service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        profile = self.service.users().getProfile(userId="me").execute()
        self.user_email = profile.get("emailAddress")
        log.info("authenticated as %s", self.user_email)

    def _svc(self):
        if not self.service:
            raise RuntimeError("Gmail client not authenticated — call authenticate() first")
        return self.service

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    def list_inbox(self, after_date: str | None = None, max_results: int = 25,
                   unread_only: bool = True) -> list[dict]:
        labels = ["INBOX", "UNREAD"] if unread_only else ["INBOX"]
        query = f"after:{after_date}" if after_date else None
        with self._lock:
            res = self._svc().users().messages().list(
                userId="me", labelIds=labels, q=query, maxResults=max_results).execute()
            out = []
            for ref in res.get("messages", []):
                detail = self._get_message(ref["id"])
                if detail:
                    out.append(detail)
        return out

    def _get_message(self, message_id: str) -> dict | None:
        try:
            with self._lock:
                msg = self._svc().users().messages().get(
                    userId="me", id=message_id, format="full").execute()
            headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
            text_parts, html_parts, attachments = [], [], []
            self._extract_parts(msg["payload"], text_parts, html_parts, attachments)
            return {
                "gmail_id": msg["id"],
                "thread_id": msg.get("threadId"),
                "from_email": headers.get("from", ""),
                "to_email": headers.get("to", ""),
                "cc": headers.get("cc", ""),
                "subject": headers.get("subject", ""),
                "date": headers.get("date", ""),
                "rfc_message_id": headers.get("message-id", ""),
                "body_text": "\n".join(text_parts),
                "body_html": "\n".join(html_parts),
                "has_attachments": bool(attachments),
                "attachment_refs": attachments,
                "attachment_names": [a["filename"] for a in attachments],
                "labels": msg.get("labelIds", []),
            }
        except Exception as e:
            log.error("fetch %s failed: %s", message_id, e)
            return None

    def _extract_parts(self, payload: dict, text_parts: list, html_parts: list, attachments: list):
        mime = payload.get("mimeType", "")
        if mime == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                text_parts.append(base64.urlsafe_b64decode(data).decode("utf-8", errors="replace"))
        elif mime == "text/html":
            data = payload.get("body", {}).get("data", "")
            if data:
                html_parts.append(base64.urlsafe_b64decode(data).decode("utf-8", errors="replace"))
        elif payload.get("filename"):
            attachments.append({
                "filename": payload["filename"],
                "mime_type": mime,
                "attachment_id": payload.get("body", {}).get("attachmentId"),
                "size": payload.get("body", {}).get("size", 0),
            })
        for part in payload.get("parts", []):
            self._extract_parts(part, text_parts, html_parts, attachments)

    def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        with self._lock:
            att = self._svc().users().messages().attachments().get(
                userId="me", messageId=message_id, id=attachment_id).execute()
        return base64.urlsafe_b64decode(att.get("data", ""))

    def get_thread_replies(self, thread_id: str, after_message_id: str | None = None) -> list[dict]:
        """All messages in a thread after `after_message_id` (all if None)."""
        try:
            with self._lock:
                thread = self._svc().users().threads().get(
                    userId="me", id=thread_id, format="full").execute()
            out, found = [], after_message_id is None
            for msg in thread.get("messages", []):
                if not found:
                    if msg["id"] == after_message_id:
                        found = True
                    continue
                headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
                text_parts, html_parts, atts = [], [], []
                self._extract_parts(msg["payload"], text_parts, html_parts, atts)
                out.append({
                    "gmail_id": msg["id"], "thread_id": thread_id,
                    "from_email": headers.get("from", ""), "to_email": headers.get("to", ""),
                    "cc": headers.get("cc", ""), "subject": headers.get("subject", ""),
                    "date": headers.get("date", ""),
                    "body_text": "\n".join(text_parts), "body_html": "\n".join(html_parts),
                    "labels": msg.get("labelIds", []),
                })
            return out
        except HttpError as e:
            if getattr(getattr(e, "resp", None), "status", None) == 404:
                if thread_id not in _WARNED_MISSING_THREADS:
                    _WARNED_MISSING_THREADS.add(thread_id)
                    log.warning("thread %s not in this mailbox (stale id) — skipping", thread_id)
                return []
            log.error("thread %s fetch failed: %s", thread_id, e)
            return []
        except Exception as e:
            log.error("thread %s fetch failed: %s", thread_id, e)
            return []

    def get_rfc_message_id(self, gmail_id: str) -> str | None:
        """RFC Message-ID header for threading (Gmail returns headers LOWERCASE)."""
        with self._lock:
            msg = self._svc().users().messages().get(
                userId="me", id=gmail_id, format="metadata").execute()
        for h in msg.get("payload", {}).get("headers", []):
            if h["name"].lower() == "message-id":
                return h["value"]
        return None

    # ------------------------------------------------------------------
    # Building MIME
    # ------------------------------------------------------------------
    @staticmethod
    def _mime(to: str, subject: str, body_html: str, cc: list[str] | None = None,
              in_reply_to: str | None = None, body_text: str | None = None) -> str:
        msg = MIMEMultipart("alternative")
        msg["to"] = to
        if cc:
            msg["cc"] = ", ".join(cc)
        msg["subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        if body_text:
            msg.attach(MIMEText(body_text, "plain", "utf-8"))
        msg.attach(MIMEText(body_html, "html", "utf-8"))
        return base64.urlsafe_b64encode(msg.as_bytes()).decode()

    # ------------------------------------------------------------------
    # Sending (direct — used only where policy explicitly allows auto-send)
    # ------------------------------------------------------------------
    def send(self, to: str, subject: str, body_html: str, cc: list[str] | None = None,
             thread_id: str | None = None, in_reply_to: str | None = None) -> dict:
        body = {"raw": self._mime(to, subject, body_html, cc, in_reply_to)}
        if thread_id:
            body["threadId"] = thread_id
        with self._lock:
            res = self._svc().users().messages().send(userId="me", body=body).execute()
        log.info("sent to %s%s | id=%s", to, f" (cc {', '.join(cc)})" if cc else "", res.get("id"))
        return res

    def send_reply(self, thread_id: str, to: str, subject: str, body_html: str,
                   cc: list[str] | None = None, in_reply_to: str | None = None) -> dict:
        subj = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        return self.send(to, subj, body_html, cc=cc, thread_id=thread_id, in_reply_to=in_reply_to)

    # ------------------------------------------------------------------
    # Drafts — the human-in-the-loop spine
    # ------------------------------------------------------------------
    def create_draft(self, to: str, subject: str, body_html: str,
                     cc: list[str] | None = None, thread_id: str | None = None,
                     in_reply_to: str | None = None) -> dict:
        """Create a draft, threaded into `thread_id` when given (with proper
        In-Reply-To/References so the human sees it inline in the conversation).
        Returns {draft_id, message_id, thread_id}."""
        if thread_id and subject and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        message: dict = {"raw": self._mime(to, subject, body_html, cc, in_reply_to)}
        if thread_id:
            message["threadId"] = thread_id
        with self._lock:
            res = self._svc().users().drafts().create(
                userId="me", body={"message": message}).execute()
        msg = res.get("message", {}) or {}
        log.info("draft created for %s (%s) | draft=%s", to, subject[:50], res.get("id"))
        return {"draft_id": res.get("id"),
                "message_id": msg.get("id"),
                "thread_id": msg.get("threadId") or thread_id}

    def delete_draft(self, draft_id: str) -> bool:
        try:
            with self._lock:
                self._svc().users().drafts().delete(userId="me", id=draft_id).execute()
            return True
        except HttpError as e:
            if getattr(getattr(e, "resp", None), "status", None) == 404:
                return False        # already gone (sent or discarded by the human)
            raise

    def send_draft(self, draft_id: str) -> dict | None:
        """Send an existing draft as-is (the one-click 'release' path — what goes out
        is exactly the reviewed draft object, edits included)."""
        try:
            with self._lock:
                res = self._svc().users().drafts().send(
                    userId="me", body={"id": draft_id}).execute()
            log.info("draft %s released → message %s", draft_id, res.get("id"))
            return res
        except HttpError as e:
            if getattr(getattr(e, "resp", None), "status", None) == 404:
                return None         # human already sent or deleted it
            raise

    def draft_exists(self, draft_id: str) -> bool:
        try:
            with self._lock:
                self._svc().users().drafts().get(userId="me", id=draft_id,
                                                 format="minimal").execute()
            return True
        except HttpError as e:
            if getattr(getattr(e, "resp", None), "status", None) == 404:
                return False
            raise

    def find_sent_in_thread(self, thread_id: str, after_epoch_ms: int = 0) -> dict | None:
        """Newest message WE sent on a thread (used to detect a human pressing Send on
        our draft: the draft disappears and a SENT message from us appears)."""
        try:
            with self._lock:
                thread = self._svc().users().threads().get(
                    userId="me", id=thread_id, format="metadata").execute()
        except HttpError:
            return None
        ours = (self.user_email or "").lower()
        best = None
        for msg in thread.get("messages", []):
            if "SENT" not in msg.get("labelIds", []):
                continue
            if int(msg.get("internalDate", 0)) < after_epoch_ms:
                continue
            headers = {h["name"].lower(): h["value"]
                       for h in msg.get("payload", {}).get("headers", [])}
            if ours and ours not in headers.get("from", "").lower():
                continue
            if best is None or int(msg.get("internalDate", 0)) > int(best.get("internalDate", 0)):
                best = msg
        return best

    # ------------------------------------------------------------------
    # Labels / read-state
    # ------------------------------------------------------------------
    def mark_as_read(self, message_id: str):
        with self._lock:
            self._svc().users().messages().modify(
                userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}).execute()

    def add_label(self, message_id: str, label_name: str):
        label_id = self._get_or_create_label(label_name)
        with self._lock:
            self._svc().users().messages().modify(
                userId="me", id=message_id, body={"addLabelIds": [label_id]}).execute()

    def _get_or_create_label(self, label_name: str) -> str:
        with self._lock:
            labels = self._svc().users().labels().list(userId="me").execute()
            for label in labels.get("labels", []):
                if label["name"] == label_name:
                    return label["id"]
            new = self._svc().users().labels().create(
                userId="me",
                body={"name": label_name, "labelListVisibility": "labelShow",
                      "messageListVisibility": "show"}).execute()
        return new["id"]
