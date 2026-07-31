import requests


class TelegramBot:
    def __init__(self,token,store,allowed_users,allowed_chats,request=None,timeout=20):
        self.token,self.store=token,store; self.allowed_users={int(x) for x in allowed_users}; self.allowed_chats={int(x) for x in allowed_chats}; self.timeout=timeout
        self.request=request or self._request
    def _request(self,method,payload):
        r=requests.post(f"https://api.telegram.org/bot{self.token}/{method}",json=payload,timeout=self.timeout); r.raise_for_status(); return r.json()
    def send_review(self,chat_id,attempt_id,text):
        if int(chat_id) not in self.allowed_chats: return False
        keyboard=[[{"text":"Accept","callback_data":f"attempt:{attempt_id}:accept"},{"text":"Reject","callback_data":f"attempt:{attempt_id}:reject"}]]
        self.request("sendMessage",{"chat_id":chat_id,"text":text,"reply_markup":{"inline_keyboard":keyboard}}); return True
    def notify(self,chat_id,text): return self.send_review(chat_id,0,text) if False else (self.request("sendMessage",{"chat_id":chat_id,"text":text}) if int(chat_id) in self.allowed_chats else False)
    def handle_callback(self,query):
        user=int(query.get("from",{}).get("id",-1)); chat=int(query.get("message",{}).get("chat",{}).get("id",-1))
        if user not in self.allowed_users or chat not in self.allowed_chats: return False
        try: prefix,raw_id,action=query["data"].split(":"); attempt_id=int(raw_id)
        except (KeyError,ValueError): return False
        if prefix!="attempt" or action not in ("accept","reject") or not self.store.get_attempt(attempt_id): return False
        self.store.set_attempt_verdict(attempt_id,"manual_accepted" if action=="accept" else "manual_rejected",{"telegram_user_id":user})
        attempt=self.store.get_attempt(attempt_id)
        if action=="reject":
            job=self.store.get_job(attempt["job_id"]); self.store.reject(job["lidarr_track_id"],attempt["provider"],attempt["source_id"],attempt_id)
        self.request("answerCallbackQuery",{"callback_query_id":query["id"]}); return True
    def poll_once(self,offset=None):
        result=self.request("getUpdates",{"timeout":20,**({"offset":offset} if offset else {})}).get("result",[])
        for update in result:
            if "callback_query" in update: self.handle_callback(update["callback_query"])
        return result
