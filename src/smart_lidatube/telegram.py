"""Fail-closed Telegram review and long polling."""

import requests


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
        keyboard = [
            [
                {
                    "text": "Accept",
                    "callback_data": f"attempt:{attempt_id}:accept",
                },
                {
                    "text": "Reject / next",
                    "callback_data": f"attempt:{attempt_id}:reject",
                },
            ],
            [
                {
                    "text": "Cancel job",
                    "callback_data": f"attempt:{attempt_id}:cancel",
                }
            ],
        ]
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

    def handle_callback(self, query):
        user = int(query.get("from", {}).get("id", -1))
        chat = int(query.get("message", {}).get("chat", {}).get("id", -1))
        if user not in self.allowed_users or chat not in self.allowed_chats:
            return False
        try:
            prefix, raw_id, action = query["data"].split(":")
            if prefix == "audit":
                page = int(action)
                if page < 0:
                    return False
                self._send_audit_page(chat, raw_id, page)
                self.request("answerCallbackQuery", {"callback_query_id": query["id"], "text": "Audit details."})
                return True
            attempt_id = int(raw_id)
        except (KeyError, ValueError):
            return False
        if prefix != "attempt" or action not in ("accept", "reject", "cancel"):
            return False
        evidence = {"telegram_user_id": user, "telegram_chat_id": chat}
        accepted = self.store.apply_review(attempt_id, action, evidence)
        text = ("Review accepted." if accepted is not None else
                "This review was already handled or is stale.")
        self.request("answerCallbackQuery", {
            "callback_query_id": query["id"], "text": text
        })
        return accepted is not None

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
