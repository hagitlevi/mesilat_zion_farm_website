# homePage/services/notify.py
import os
from twilio.rest import Client

def send_sms(to_e164: str, text: str):
    """
    שולח SMS דרך Twilio. מספרי טלפון בפורמט E.164 (למשל +9725XXXXXXXX).
    """
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    from_num = os.getenv("TWILIO_FROM")
    if not (sid and token and from_num and to_e164):
        return False

    client = Client(sid, token)
    client.messages.create(
        to=to_e164,
        from_=from_num,
        body=text
    )
    return True



