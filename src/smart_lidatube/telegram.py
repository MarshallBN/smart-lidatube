"""Fail-closed Telegram review and long polling."""

import requests

from .audit_origin import is_audit_origin
from .review_policy import review_action_error


class TelegramBot:
    def __init__(
        self,
        token,
        store,
        allowed_users,
        allowed_chats,
        request=None,
        timeout=20,
    ):
        self.token = token
        self.store = store
        self.allowed_users = {int(value) for value in allowed_users}
        self.allowed_chats = {int(value) for value in allowed_chats}
        self.timeout = timeout
        self.request = request or self._request
        saved = self.store.get_setting("telegram_update_offset")
        self.offset = int(saved) if saved is not None else None

    def _request(self, method, payload):
        response = requests.post(
            f"https://api.telegram.org/bot{self.token}/{method}",
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def send_review(self, chat_id, attempt_id, text):
        if int(chat_id) not in self.allowed_chats:
            return False
        attempt = self.store.get_attempt(attempt_id)
        job = self.store.get_job(attempt["job_id"]) if attempt else None
        audit = is_audit_origin(job)
        keyboard = ([
            [
                {"text": "Accept replacement", "callback_data": f"attempt:{attempt_id}:accept"},
                {"text": "Reject candidate", "callback_data": f"attempt:{attempt_id}:reject"},
            ],
            [
                {"text": "Ignore track", "callback_data": f"attempt:{attempt_id}:ignore_track"},
                {"text": "Audit later", "callback_data": f"attempt:{attempt_id}:audit_later"},
            ],
        ] if audit else [
            [
                {"text": "Accept", "callback_data": f"attempt:{attempt_id}:accept"},
                {"text": "Reject / next", "callback_data": f"attempt:{attempt_id}:reject"},
            ],
            [{"text": "Cancel job", "callback_data": f"attempt:{attempt_id}:cancel"}],
        ])
        self.request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": {"inline_keyboard": keyboard},
            },
        )
        return True

    def notify(self, chat_id, text):
        if int(chat_id) not in self.allowed_chats:
            return False
        self.request("sendMessage", {"chat_id": chat_id, "text": text})
        return True

    def send_audit_digest(self, chat_id, date, report_empty=False):
        """Send one safe daily report; details remain bounded behind callbacks."""
        key = f"audit_digest_sent:{date}"
        if self.store.get_setting(key):
            return False
        events = self.store.audit_digest_events(date)
        if not events and not report_empty:
            return False
        if int(chat_id) not in self.allowed_chats:
            return False
        counts = {}
        for event in events: counts[event["result_status"]] = counts.get(event["result_status"], 0) + 1
        text = "Library audit — %s\nUpdated classifications: %s\n%s" % (date, len(events), ", ".join(f"{k}: {v}" for k,v in sorted(counts.items())) or "No changes")
        payload = {"chat_id": chat_id, "text": text}
        if events:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": f"Details ({len(events)})", "callback_data": f"audit:{date}:0"}]]}
        self.request("sendMessage", payload)
        self.store.set_setting(key, "1")
        return True

    def _send_audit_page(self, chat_id, date, page):
        events=self.store.audit_digest_events(date); start=page*10; chunk=events[start:start+10]; pages=max(1,(len(events)+9)//10)
        lines=[f"Library audit details — {date}", f"Page {page+1}/{pages}"]
        for event in chunk:
            safe=event["evidence_json"]; lines.append("• %s — %s — %s (%s)" % (safe.get("artist", "Track"), safe.get("title", event["lidarr_track_id"]), event["result_status"], safe.get("reason", "checked")))
        buttons=[]
        if page: buttons.append({"text":"Previous","callback_data":f"audit:{date}:{page-1}"})
        if page+1<pages: buttons.append({"text":"Next","callback_data":f"audit:{date}:{page+1}"})
        self.request("sendMessage", {"chat_id":chat_id,"text":"\n".join(lines),"reply_markup":{"inline_keyboard":[buttons]} if buttons else {}})

    def _answer(self, query, text):
        """Always acknowledge a callback so the client never spins silently."""
        try:
            self.request("answerCallbackQuery", {
                "callback_query_id": query["id"], "text": str(text)[:200],
            })
        except Exception:
            # Acknowledgement is best-effort; never break review handling on it.
            pass

    def _edit_review(self, query, outcome):
        """Record the action on the review message itself so presses are visible."""
        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        text = message.get("text") or ""
        if chat_id is None or message_id is None or not text:
            return
        try:
            self.request("editMessageText", {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": f"{text}\n\n— {outcome}",
                "reply_markup": {"inline_keyboard": []},
            })
        except Exception:
            # Message too old / not editable: the callback answer still gives feedback.
            pass

    def handle_callback(self, query):
        user = int(query.get("from", {}).get("id", -1))
        chat = int(query.get("message", {}).get("chat", {}).get("id", -1))
        if user not in self.allowed_users or chat not in self.allowed_chats:
            self._answer(query, "Not authorized for review actions.")
            return False
        try:
            prefix, raw_id, action = query["data"].split(":")
            if prefix == "audit":
                page = int(action)
                if page < 0:
                    self._answer(query, "Already on the first page.")
                    return False
                self._send_audit_page(chat, raw_id, page)
                self._answer(query, "Audit details.")
                return True
            attempt_id = int(raw_id)
        except (KeyError, ValueError):
            self._answer(query, "Unrecognized button.")
            return False
        if prefix != "attempt":
            self._answer(query, "Unrecognized button.")
            return False
        evidence = {"telegram_user_id": user, "telegram_chat_id": chat}
        attempt = self.store.get_attempt(attempt_id)
        if attempt is None:
            self._answer(query, "This review no longer exists.")
            return False
        job = self.store.get_job(attempt["job_id"])
        audit = is_audit_origin(job)
        policy_error = review_action_error(
            self.store.review_provider(attempt_id), action
        )
        if policy_error:
            self._edit_review(query, f"Action unavailable: {policy_error}")
            self._answer(query, policy_error)
            return False
        if audit:
            if action not in ("accept", "reject", "ignore_track", "audit_later"):
                self._answer(query, "Action not available for this review.")
                return False
            accepted = self.store.apply_audit_review(attempt_id, action, evidence)
        else:
            if action not in ("accept", "reject", "cancel"):
                self._answer(query, "Action not available for this review.")
                return False
            accepted = self.store.apply_review(attempt_id, action, evidence)
        if accepted is None:
            outcome = "Already handled or stale"
            self._edit_review(query, f"⊘ {outcome}")
            self._answer(query, f"{outcome}.")
            return False
        outcome = {
            "accept": "✓ Accepted — replacement will proceed",
            "reject": "✓ Rejected",
            "cancel": "✓ Job cancelled",
            "ignore_track": "✓ Track ignored (do-not-upgrade set)",
            "audit_later": "✓ Deferred — will recheck later",
        }.get(action, f"✓ {action}")
        self._edit_review(query, outcome)
        self._answer(query, outcome)
        return True

    def poll_once(self, offset=None):
        requested_offset = self.offset if offset is None else offset
        payload = {"timeout": self.timeout}
        if requested_offset is not None:
            payload["offset"] = requested_offset
        updates = self.request("getUpdates", payload).get("result", [])
        for update in updates:
            if "callback_query" in update:
                self.handle_callback(update["callback_query"])
            update_id = update.get("update_id")
            if update_id is not None:
                self.offset = max(self.offset or 0, int(update_id) + 1)
                self.store.set_setting("telegram_update_offset", self.offset)
        return updates
