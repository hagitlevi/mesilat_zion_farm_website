import requests
from django.conf import settings

def send_sms_via_phone(to: str, text: str) -> bool:
    if not (settings.SEND_SMS and settings.PHONE_SMS_GATEWAY_URL and settings.PHONE_SMS_SECRET):
        return False
    try:
        r = requests.post(
            settings.PHONE_SMS_GATEWAY_URL.rstrip("/") + "/sms",
            json={"to": to, "text": text, "secret": settings.PHONE_SMS_SECRET},
            timeout=5,
        )
        return r.ok
    except Exception:
        return False
