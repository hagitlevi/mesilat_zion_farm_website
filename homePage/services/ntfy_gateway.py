import requests
from django.conf import settings

def send_sms_via_ntfy(phone: str, text: str, timeout: int = 5) -> bool:
    """
    שולח הודעה ל-ntfy כך שהמאקרו שלך יקבל:
    {not_title} = מספר הטלפון, {notification} = טקסט ההודעה.
    """
    topic = getattr(settings, "NTFY_TOPIC", "")
    base  = getattr(settings, "NTFY_URL", "https://ntfy.sh").rstrip("/")
    prio  = str(getattr(settings, "NTFY_PRIORITY", 5))

    if not topic or not phone or not text:
        return False

    url = f"{base}/{topic}"
    headers = {
        "Title": phone,                             # יגיע ל-{not_title}
        "Priority": prio,
        "Content-Type": "text/plain; charset=utf-8",
        # אם יהיה Token בעתיד: "Authorization": "Bearer <TOKEN>"
    }
    r = requests.post(url, data=f"{text}", headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.ok
