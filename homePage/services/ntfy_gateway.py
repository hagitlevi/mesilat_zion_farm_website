import secrets
import requests
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from decimal import Decimal
from datetime import datetime, timedelta
from django.utils import timezone
from homePage.models import Activity, Booking, TreatmentSession
from types import SimpleNamespace
from decimal import InvalidOperation
from django.db import IntegrityError
import smtplib
from django.core.mail import BadHeaderError
from django.core.mail import send_mail
from django.db import transaction
from zoneinfo import ZoneInfo
import logging

logger = logging.getLogger(__name__)

##-----------------------------------פונקציות עזר------------------------------

def get_treatment_amount_nis(session) -> Decimal | None:
  """
  מחזיר מחיר ב-₪ לפי Activity בשם 'שיעורי רכיבה/ טיפולית'.
  אם יש start_time/end_time – מנסה התאמה מדויקת ל-duration_minutes.
  אחרת נופל לפעילות הראשונה עם price לא-ריק.
  """

  qs = Activity.objects.filter(name="שיעורי רכיבה/ טיפולית").exclude(price__isnull=True)
  if not qs.exists():
    return None

  minutes = None
  st = getattr(session, "start_time", None)
  et = getattr(session, "end_time", None)
  base_date = getattr(session, "date", None) or timezone.localdate()

  if st and et:
    start_dt = datetime.combine(base_date, st)
    end_dt = datetime.combine(base_date, et)
    if end_dt <= start_dt:  # ביטחון למקרה חריג
      end_dt += timedelta(days=1)
    minutes = int((end_dt - start_dt).total_seconds() // 60)

  act = None
  if minutes is not None:
    act = qs.filter(duration_minutes=minutes).order_by("id").first()
  if not act:
    act = qs.order_by("duration_minutes", "id").first()

  if act and act.price is not None:
    return Decimal(act.price)

  return None

def ltr_text(s: str) -> str:
  """מציג s משמאל-לימין בתוך טקסט RTL (ל־SMS זה פותר היפוך "HH:MM–HH:MM")"""
  return "\u2066" + s + "\u2069"  # LRI ... PDI

def session_msgid(session) -> str:
  """
  יוצר Message-ID דטרמיניסטי לפי payment_ref כדי שכל הודעות ההמשך יתייחסו אליו.
  """
  ref = (session.payment_ref or gen_unique_ref_any()).replace(" ", "")
  # דומיין ל-Message-ID: מנסה לקחת מה-DEFAULT_FROM_EMAIL, ואם לא – נופל לברירת מחדל.
  from_domain = getattr(settings, "DEFAULT_FROM_EMAIL", "")
  domain = from_domain.split("@")[-1] if "@" in from_domain else "mesilat-zion.local"
  return f"<session-{ref}@{domain}>"

def gen_unique_ref_any(digits: int = 8) -> str:
  """מייצר מחרוזת ייחודית בפורמט MZ-XXXXXXXX, מנסה עד 25 פעמים מול ה-DB לפני פולבאק נדיר עם תאריך"""
  for _ in range(25):
    cand = "MZ-" + "".join(secrets.choice("0123456789") for _ in range(digits))
    if (not Booking.objects.filter(payment_ref=cand).exists()
        and not TreatmentSession.objects.filter(payment_ref=cand).exists()):
      return cand
  return "MZ-" + timezone.now().strftime("%y%m%d%H%M%S")

def normalize_phone_il(phone: str) -> str:
  """מסיר כל תו שאינו ספרה, וממיר קידומת 972 ל-0 אם יש לפחות 11 ספרות בסך הכל."""
  p = "".join(ch for ch in (phone or "") if ch.isdigit())
  if p.startswith("972") and len(p) >= 11:
    p = "0" + p[3:]
  return p

def _notify_time_change_core(obj, new_txt: str, amount: Decimal | None, log_tag: str) -> bool:
  """
  שולח מייל (טקסט בלבד) + SMS עבור שינוי שעה, גם עבור Booking וגם עבור Session.
  obj: Booking או TreatmentSession
  new_txt: טקסט "תאריך/שעה חדשים" שכבר חישבת בחוץ
  amount: סכום לתצוגה ב-SMS (ל-Booking), או None
  log_tag: תווית לוג קצרה לזיהוי מקור הקריאה
  """
  sent_any = False

  # ===== Email =====
  to_email = (getattr(obj, "customer_email", "") or "").strip()
  if to_email:
    try:
      root_msgid = session_msgid(obj)  # מזהה יציב לפי payment_ref
    except AttributeError:
      root_msgid = None

    subject_ref = getattr(obj, "payment_ref", "") or "—"
    customer_name = (
        (getattr(obj, "customer_name", "") or "")
        or (getattr(obj, "customer_full_name", "") or "")
    ).strip()

    headers = {"In-Reply-To": root_msgid, "References": root_msgid} if root_msgid else {}
    email = EmailMultiAlternatives(
      f"Re: אישור הזמנה – חוות מסילת ציון · {subject_ref}",
      f"שלום {customer_name},\nעודכנה שעת המפגש שלך בחוות מסילת ציון:\n{new_txt}\n",
      getattr(settings, "DEFAULT_FROM_EMAIL", None),
      [to_email],
      headers=headers,
    )
    try:
      email.send(fail_silently=True)
      sent_any = True
    except (smtplib.SMTPException, OSError, BadHeaderError) as ex:
      print(f"[{log_tag}] email failed: {ex}")

  # ===== SMS =====
  phone_norm = normalize_phone_il(getattr(obj, "customer_phone", "") or "")
  if getattr(settings, "SEND_SMS", False) and phone_norm:
    # אם זה Booking (יש start_dt), נבנה אובייקט "דמוי-Session" כדי למחזר את הפורמט
    if hasattr(obj, "start_dt"):
      sd = getattr(obj, "start_dt", None)
      ed = getattr(obj, "end_dt", None)

      p_raw = getattr(obj, "participants", 0)
      try:
        participants = int(p_raw) if p_raw is not None else 0
      except (TypeError, ValueError):
        participants = 0

      session_like = SimpleNamespace(
        date=sd.date() if sd else None,
        start_time=sd.time() if sd else None,
        end_time=ed.time() if ed else None,
        payment_ref=getattr(obj, "payment_ref", "") or "",
        customer_full_name=getattr(obj, "customer_name", "") or "",
        customer_phone=getattr(obj, "customer_phone", "") or "",
        customer_email=getattr(obj, "customer_email", "") or "",
        instructor=None,
        participants=participants,
        details=getattr(obj, "details", "") or "",
      )
      sess_for_sms = session_like
    else:
      # כבר TreatmentSession
      sess_for_sms = obj

    sms_text = format_session_sms(
      sess_for_sms,
      amount=amount,  # ל-Session תשאירי None; ל-Booking תעבירי סכום שחישבת
      header="עודכנה שעת המפגש שלך בחוות מסילת ציון:\n",
    )
    try:
      send_sms_via_ntfy(phone_norm, sms_text)
      sent_any = True or sent_any
    except requests.RequestException as ex:
      print(f"[{log_tag}] SMS send failed: {ex}")

  return sent_any


##-----------------------------------פונקציות לשליחת SMS------------------------------

def send_sms_via_ntfy(phone: str, text: str, timeout: int = 5) -> bool:
    """
    שולח הודעה ל-ntfy כך שהמאקרו שלך יקבל:
    {not_title} = מספר הטלפון, {notification} = טקסט ההודעה.
    """
    logger.debug("send_sms_via_ntfy called with phone: %s, text: %s", phone, text)

    if not getattr(settings, "SEND_SMS", False):
        return False
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

def format_session_sms(session, amount: Decimal | None = None, header: str | None = None) -> str:
    """
    שליחת SMS דרך האדמין
    """
    logger.debug("format_session_sms called with session: %s, amount: %s, header: %s", session, amount, header)

    # אם לא קיבלנו override – נחשב מה-Activity "שיעורי רכיבה/ טיפולית"
    if amount is None:
      amount = get_treatment_amount_nis(session)


    name  = (session.customer_full_name or "לקוח/ה").strip()
    instr = getattr(session.instructor, "full_name", "") if getattr(session, "instructor", None) else ""
    ref   = session.payment_ref or "—"

    if getattr(session, "date", None):
        if session.start_time and session.end_time:
            times = f"{session.start_time.strftime('%H:%M')}–{session.end_time.strftime('%H:%M')}"
            when = f"{session.date:%d.%m.%Y} בשעה {ltr_text(times)}"
        elif session.start_time:
            t = session.start_time.strftime("%H:%M")
            when = f"{session.date:%d.%m.%Y} בשעה {ltr_text(t)}"
        else:
            when = f"{session.date:%d.%m.%Y}"
    else:
        when = ""

    # אם header לא סופק – נשמור על ההתנהגות הישנה (ניסוח "נקבעה בהצלחה")
    top_line = header or "ההזמנה נקבעה בהצלחה בחוות מסילת ציון."

    lines = [
        f"היי {name},",
        top_line,
        f"מס' הזמנה: {ref}",
    ]
    if when:  lines.append(f"תאריך: {when}")
    if instr: lines.append(f"מדריך/ה: {instr}")
    if amount is not None:
        lines.append(f"סכום ששולם: ₪{amount:}")  # שומר כמו שהיה אצלך

    try:
      participants = int(getattr(session, "participants", 0) or 0)
    except (TypeError, ValueError):
      participants = 0

    if header is not None and participants >= 2:
        lines.append(f"מספר משתתפים: {participants}")

    lines.append("מיקום בוויז: חוות מסילת ציון")
    lines.append("יש להגיע עם מכנס ארוך ונעליים סגורות.")
    lines.append(" ")
    lines.append(f"מחכים לראותכם!\n")
    lines.append("(יש לשמור את מספר ההזמנה לביטולים והחזרים כספיים)")
    return "\n".join(lines)

def format_booking_sms(payment, booking) -> str:
    """פורמט SMS ייעודי להזמנות (Booking) – כולל פרטים כמו activity, participants, start/end."""
    logger.debug("format_booking_sms called with payment: %s, booking: %s", payment, booking)

    name = (getattr(payment, "customer_name", "") or getattr(booking, "customer_name", "")).strip() or "לקוח/ה"
    charge_id = getattr(payment, "charge_id", "") or "—"
    participants = getattr(booking, "participants", None)
    activity_name = getattr(getattr(booking, "activity", None), "name", "")
    start_dt = getattr(booking, "start_dt", None)
    end_dt = getattr(booking, "end_dt", None)

    ag = getattr(payment, "amount_agorot", None)
    try:
      amount_nis = (Decimal(ag) / Decimal("100")) if ag is not None else None
    except (InvalidOperation, TypeError, ValueError):
      amount_nis = None

    lines = [
      f"היי {name},\n\nההזמנה ל{activity_name} בחוות מסילת ציון בוצעה בהצלחה",
      f"מס' הזמנה: {charge_id}",
    ]

    if start_dt:
        time_str = f"{start_dt:%d.%m.%Y}\nבשעה {end_dt:%H:%M}" + (f"–{start_dt:%H:%M}" if end_dt else "")
        lines.append(f"תאריך: {time_str}")

    if participants and participants > 1:
        lines.append(f"מס' משתתפים: {participants}")
    if amount_nis is not None:
        lines.append(f"סכום: ₪{int(amount_nis)}")

    lines.append("מיקום בוויז: חוות מסילת ציון")
    lines.append("יש להגיע עם מכנס ארוך ונעליים סגורות")

    lines.append(" ")
    lines.append(f"מחכים לראותכם!\n")
    lines.append("(יש לשמור את מספר ההזמנה לביטולים והחזרים כספיים)")
    return "\n".join(lines)

def notify_booking_success(payment, booking):
  """פונקציה עזר לשליחת התראות (מייל + SMS) לאחר יצירת Booking חדש בהצלחה."""
  logger.debug("notify_booking_success called with payment: %s, booking: %s", payment, booking)

  if getattr(settings, "SEND_EMAIL", False):
    try:
      send_booking_email(payment, booking)
    except Exception:
      pass

  if getattr(settings, "SEND_SMS", False):
    try:
      sms_text = format_booking_sms(payment, booking)
      send_sms_via_ntfy(payment.phone, sms_text)
    except Exception:
      pass


##-----------------------------------פונקציות לשליחת מייל------------------------------

def send_treatment_email(session, amount: Decimal | None = None) -> bool:
  """שולח מייל עם פרטי הזמנה ללקוח, כולל פורמט טוב לשעת המפגש, סכום, ומידע נוסף. מחזיר True אם נשלח לפחות מייל אחד (לא משנה אם SMS הצליח או לא)."""
  logger.debug("send_treatment_email called with session: %s, amount: %s", session, amount)

  if not getattr(settings, "SEND_EMAIL", False):
    return
  if not (session.customer_email or "").strip():
    return False

  # חישוב סכום אם לא הועבר
  if amount is None:
    amount = get_treatment_amount_nis(session)

  if not getattr(session, "payment_ref", None):
    session.payment_ref = gen_unique_ref_any()
    try:
      session.save(update_fields=["payment_ref"])
    except IntegrityError:
      # התנגשות נדירה על מפתח ייחודי → מייצרים מזהה חדש ומנסים פעם נוספת
      session.payment_ref = gen_unique_ref_any()
      session.save(update_fields=["payment_ref"])

  subject_ref = session.payment_ref or "—"
  base_subject = f"אישור הזמנה – חוות מסילת ציון · {subject_ref}"

  # --- כיווניות נכונה לזמנים ---
  LRI, PDI = "\u2066", "\u2069"  # isolate LTR לחלק של השעה בטקסט
  # טקסט (SMS/Plain): נעטוף רק את החלק של השעה ב-LTR isolate
  if getattr(session, "date", None):
    if session.start_time and session.end_time:
      times = f"{session.start_time.strftime('%H:%M')}–{session.end_time.strftime('%H:%M')}"
      when_text = f"{session.date:%d.%m.%Y} בשעה {LRI}{times}{PDI}"
      when_html = f"""{session.date:%d.%m.%Y} בשעה <span dir="ltr" style="unicode-bidi:isolate">{times}</span>"""
    elif session.start_time:
      t = session.start_time.strftime("%H:%M")
      when_text = f"{session.date:%d.%m.%Y} בשעה {LRI}{t}{PDI}"
      when_html = f"""{session.date:%d.%m.%Y} בשעה <span dir="ltr" style="unicode-bidi:isolate">{t}</span>"""
    else:
      when_text = f"{session.date:%d.%m.%Y}"
      when_html = when_text
  else:
    when_text = ""
    when_html = ""

  instr = getattr(session.instructor, "full_name", "") if getattr(session, "instructor", None) else ""
  name = (session.customer_full_name or "").strip() or "לקוח/ה"

  # --- גוף טקסטואלי (Plain) ---
  rlm = "\u200F"  # שומר RTL כללי
  text_lines = [
    f"שלום {name},", "",
    "ההזמנה שלך בחוות מסילת ציון נקבעה בהצלחה!",
    f"מספר הזמנה: {subject_ref}",
  ]
  if amount is not None:
    text_lines.append(f"סכום ששולם: ₪{amount:.2f}")
  if when_text:
    text_lines.append(f"תאריך ושעה: {when_text}")
  if instr:
    text_lines.append(f"מדריך/ה: {instr}")
  text_lines += [
    "", "מיקום בוויז: חוות מסילת ציון",
    "אנא הגיעו עם מכנס ארוך ונעליים סגורות.", "",
    "שמרו מייל זה. לביטול/החזר תזדקקו למספר ההזמנה.",
    "זהו מייל אוטומטי – אין להשיב אליו.",
  ]
  text_body = rlm + "\n".join(text_lines)

  # --- טבלת HTML ---
  amount_row = f"""
      <tr>
        <td style="padding:12px 16px;background:#fafafa;font-size:14px;color:#666;text-align:right">סכום ששולם</td>
        <td style="padding:12px 16px;font-size:14px;color:#111;text-align:right">₪{amount:.2f}</td>
      </tr>
    """ if amount is not None else ""
  time_row = f"""
      <tr>
        <td style="padding:12px 16px;background:#fafafa;font-size:14px;color:#666;text-align:right">תאריך ושעה</td>
        <td style="padding:12px 16px;font-size:14px;color:#111;text-align:right">{when_html}</td>
      </tr>
    """ if when_html else ""
  instr_row = f"""
      <tr>
        <td style="padding:12px 16px;background:#fafafa;font-size:14px;color:#666;text-align:right">מדריך/ה</td>
        <td style="padding:12px 16px;font-size:14px;color:#111;text-align:right">{instr}</td>
      </tr>
    """ if instr else ""

  html_body = f"""<!doctype html>
<html lang="he" dir="rtl">
  <head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{base_subject}</title></head>
  <body style="margin:0;background:#f6f7f9;font-family:Arial,Helvetica,'Segoe UI',sans-serif;direction:rtl;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="width:100%;padding:24px 0;">
      <tr><td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0"
               style="width:100%;max-width:600px;background:#ffffff;border-radius:12px;overflow:hidden;table-layout:fixed;">
          <tr><td style="padding:18px 24px;background:#2f2a27;color:#fff;font-weight:700;font-size:18px;text-align:right;">
            פרטי ההזמנה</td></tr>
          <tr><td style="padding:22px;word-break:break-word;overflow-wrap:anywhere;">
            <h1 style="margin:0 0 12px 0;font-size:20px;color:#222;line-height:1.35;">שלום {name},</h1>
            <p style="margin:0 0 16px 0;font-size:15px;color:#333;line-height:1.6;">ההזמנה נקבעה בהצלחה!</p>
            <table role="presentation" cellpadding="0" cellspacing="0"
                   style="width:100%;border:1px solid #eee;border-radius:10px;overflow:hidden;border-collapse:separate;">
              <tr>
                <td style="padding:12px 16px;background:#fafafa;font-size:14px;color:#666;text-align:right">מספר הזמנה</td>
                <td style="padding:12px 16px;font-size:14px;color:#111;text-align:right;word-break:break-word;overflow-wrap:anywhere;">{subject_ref}</td>
              </tr>
              {amount_row}{time_row}{instr_row}
            </table>
            <p style="margin:10px 0 12px 0;font-size:13.5px;color:#555;line-height:1.6;">
              שמרו מייל זה. לביטול/החזר תזדקקו למספר ההזמנה.<br>
              זהו מייל אוטומטי – אין להשיב אליו.
            </p>
            <hr style="border:none;border-top:1px solid #eee;margin:18px 0">
            <p style="margin:0;font-size:12px;color:#999;line-height:1.5;">© חוות מסילת ציון · כל הזכויות שמורות</p>
          </td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""

  # --- Message-ID קבוע לשרשור עתידי ---
  from_addr = getattr(settings, "DEFAULT_FROM_EMAIL", None)
  stable_msgid = session_msgid(session)

  # --- שליחה עם EmailMultiAlternatives ---
  email = EmailMultiAlternatives(
    base_subject,
    text_body,
    from_addr,
    [session.customer_email],
    headers={
      "Message-ID": stable_msgid,
      "References": stable_msgid,
    },
  )

  email.attach_alternative(html_body, "text/html")
  sent = email.send(fail_silently=True)
  return sent >= 1

def send_booking_email(payment, booking):
  """שולח מייל עם פרטי הזמנה ללקוח עבור Booking, כולל פורמט טוב לשעת המפגש, סכום, ומידע נוסף. מחזיר True אם נשלח לפחות מייל אחד (לא משנה אם SMS הצליח או לא)."""
  logger.debug("send_booking_email called with payment: %s, booking: %s", payment, booking)

  if not getattr(settings, "SEND_EMAIL", False):
    return
  if not getattr(payment, "email", None):
    return False

  # נתונים
  amount_nis = ((getattr(payment, "amount_agorot", 0) or 0) / 100)
  charge_id = getattr(payment, "charge_id", None) or "—"
  customer = (getattr(payment, "customer_name", "") or "").strip()
  participants = getattr(booking, "participants", 1)
  start_dt = getattr(booking, "start_dt", None)
  end_dt = getattr(booking, "end_dt", None)

  # כותרת ייחודית (מפחית קיפול "טקסט מצוטט")
  subject = f"אישור הזמנה – חוות מסילת ציון · {charge_id}"

  # --- טקסט גיבוי RTL (RLM) ---
  rlm = "\u200F"
  text_body_core = (
      f"שלום {customer},\n\n"
      f"התשלום עבר בהצלחה!\n\n"
      f"מספר עסקה: {charge_id}\n"
      f"סכום ששולם: ₪{amount_nis:.2f}\n"
      f"מספר משתתפים: {participants}\n"
      + (f"תאריך: {start_dt:%d.%m.%Y} בשעה {start_dt:%H:%M}"
         + (f"–{end_dt:%H:%M}" if end_dt else "") + "\n" if start_dt else "")
      + "\nנתראה בחווה 🐴\n\n"
        f"שמרו מייל זה. לביטול/החזר תזדקקו למספר העסקה \n"
        "זהו מייל אוטומטי – אין להשיב אליו."
  )
  text_body = rlm + text_body_core

  # --- שורת זמן ל-HTML (רק אם יש start_dt) ---
  time_row = ""
  if start_dt:
    time_row = (
      f"<tr>"
      f"<td align='right' style='padding:12px 16px;background:#fafafa;font-size:14px;color:#666;text-align:right'>תאריך ושעה</td>"
      f"<td align='right' style='padding:12px 16px;font-size:14px;color:#111;text-align:right'>"
      f"{start_dt:%d.%m.%Y} בשעה {start_dt:%H:%M}"
      f"{'–' + end_dt.strftime('%H:%M') if end_dt else ''}"
      f"</td></tr>"
    )

  # --- HTML RTL ממורכז, רספונסיבי, עם מניעת גלישה ---
  html_body = f"""<!doctype html>
      <html lang="he" dir="rtl">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>פרטי ההזמנה</title>
        </head>
        <body style="margin:0;background:#f6f7f9;font-family:Arial,Helvetica,'Segoe UI',sans-serif;direction:rtl;-ms-text-size-adjust:100%;-webkit-text-size-adjust:100%;mso-line-height-rule:exactly;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                 style="width:100%;background:#f6f7f9;padding:24px 0;border-collapse:collapse;mso-table-lspace:0pt;mso-table-rspace:0pt;">
            <tr>
              <td align="center" style="padding:0;">
                <center>
                  <table role="presentation" width="600" cellpadding="0" cellspacing="0"
                         style="width:100%;max-width:600px;background:#ffffff;border-radius:12px;overflow:hidden;border-collapse:collapse;table-layout:fixed;margin:0 auto;direction:rtl;text-align:right;">
                    <tr>
                      <td style="padding:18px 24px;background:#2f2a27;color:#fff;font-weight:700;font-size:18px;text-align:right;word-break:break-word;overflow-wrap:anywhere;">
                        פרטי ההזמנה
                      </td>
                    </tr>
                    <tr>
                      <td style="padding:22px;word-break:break-word;overflow-wrap:anywhere;">
                        <h1 style="margin:0 0 12px 0;font-size:20px;color:#222;line-height:1.35;">שלום {customer},</h1>
                        <p style="margin:0 0 16px 0;font-size:15px;color:#333;line-height:1.6;">התשלום עבר בהצלחה!</p>

                        <table role="presentation" cellpadding="0" cellspacing="0"
                               style="width:100%;border:1px solid #eee;border-radius:10px;overflow:hidden;border-collapse:separate;">
                          <tr>
                            <td align="right" style="padding:12px 16px;background:#fafafa;font-size:14px;color:#666;text-align:right">מספר עסקה</td>
                            <td align="right" style="padding:12px 16px;font-size:14px;color:#111;text-align:right;word-break:break-word;overflow-wrap:anywhere;">{charge_id}</td>
                          </tr>
                          <tr>
                            <td align="right" style="padding:12px 16px;background:#fafafa;font-size:14px;color:#666;text-align:right">סכום ששולם</td>
                            <td align="right" style="padding:12px 16px;font-size:14px;color:#111;text-align:right">₪{amount_nis:.2f}</td>
                          </tr>
                          <tr>
                            <td align="right" style="padding:12px 16px;background:#fafafa;font-size:14px;color:#666;text-align:right">מספר משתתפים</td>
                            <td align="right" style="padding:12px 16px;font-size:14px;color:#111;text-align:right">{participants}</td>
                          </tr>
                          {time_row}
                        </table>

                        <p style="margin:10px 0 12px 0;font-size:13.5px;color:#555;line-height:1.6;">
                          שמרו מייל זה. לביטול/החזר תזדקקו למספר העסקה <br>
                          זהו מייל אוטומטי – אין להשיב אליו.
                        </p>

                        <hr style="border:none;border-top:1px solid #eee;margin:18px 0">

                        <p style="margin:0;font-size:12px;color:#999;line-height:1.5;">
                          © חוות מסילת ציון · כל הזכויות שמורות
                        </p>
                      </td>
                    </tr>
                  </table>
                </center>
              </td>
            </tr>
          </table>
        </body>
      </html>
      """

  # ===== שליחת המייל עם Message-ID קבוע לשרשור =====

  root_msgid = session_msgid(booking)  # אותו יוצר מזהה לפי payment_ref

  email = EmailMultiAlternatives(
    subject,
    text_body,
    getattr(settings, "DEFAULT_FROM_EMAIL", None),
    [payment.email],
    headers={"Message-ID": root_msgid, "References": root_msgid},
  )
  if html_body:
    email.attach_alternative(html_body, "text/html")

  sent = email.send(fail_silently=True)
  return sent >= 1

def send_booking_deleted_email(booking):
  """שולח מייל על ביטול הזמנה (Booking) ללקוח, כולל פרטים כמו activity, start_dt, ו-ref. מחזיר True אם נשלח לפחות מייל אחד (לא משנה אם SMS הצליח או לא)."""
  logger.debug("send_booking_deleted_email called with booking: %s", booking)

  if not getattr(settings, "SEND_EMAIL", False):
    return
  email = (getattr(booking, "customer_email", "") or "").strip()
  if not email:
    return

  ref = (getattr(booking, "payment_ref", "") or f"#{booking.id}").strip()
  dt = getattr(booking, "start_dt", None)
  when = ""
  if dt:
    # אם dt נאיבי → נניח שזה זמן ישראל
    if timezone.is_naive(dt):
      dt = timezone.make_aware(dt, ZoneInfo("Asia/Jerusalem"))
    # ואם הוא כבר aware → נהפוך לזמן ישראל
    dt = timezone.localtime(dt, ZoneInfo("Asia/Jerusalem"))
    when = dt.strftime("%d.%m.%Y %H:%M")

  subject = f"ביטול הזמנה – חוות מסילת ציון · {ref}"
  body = (
      f"שלום {getattr(booking, 'customer_name', '') or 'לקוח/ה'}\n\n"
      + (f"הזמנתך בחוות מסילת ציון ב- {when}\n"
         "בוטלה וההחזר הכספי יכנס לחשבונך בימים הקרובים.\n\n"
         if when else
         "הזמנתך בחוות מסילת ציון בוטלה וההחזר הכספי יכנס לחשבונך בימים הקרובים.\n\n")
      + "לשאלות ניתן ליצור קשר.\n"
        "הודעה זו אוטומטית – אין להשיב."
  )

  transaction.on_commit(lambda: send_mail(
    subject=subject,
    message=body,
    from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
    recipient_list=[email],
    fail_silently=True,
  ))

#----------------------------------------------------------------------#

def notify_time_change_booking(_request, booking, _old_start_dt, _old_end_dt) -> bool:
  """
  שליחת עדכון שינוי שעה להזמנת Booking:
  - מייל (טקסט בלבד)
  - SMS דרך ntfy (אם SEND_SMS=True)
  """
  logger.debug("notify_time_change_booking called with booking: %s", booking)

  if not getattr(settings, "SEND_EMAIL", False):
    return False

  d = getattr(booking, "start_dt", None)
  e = getattr(booking, "end_dt", None)
  if d and e:
    new_txt = f"{d:%d.%m.%Y} {d:%H:%M}–{e:%H:%M}"
  elif d:
    new_txt = f"{d:%d.%m.%Y} {d:%H:%M}"
  else:
    new_txt = f"{d or ''} {e or ''}"

  # סכום ששולם (אם יש)
  tp = getattr(booking, "total_price", None)
  try:
    amount = Decimal(tp) if tp is not None else None
  except (InvalidOperation, TypeError, ValueError):
    amount = None

  return _notify_time_change_core(booking, new_txt, amount, "notify_time_change_booking")

def notify_time_change(_request, session, _old_date, _old_start, _old_end) -> bool:
  """
  שולח מייל (טקסט בלבד) + SMS על שינוי שעה.
  """
  logger.debug("notify_time_change called with session: %s", session)

  d = getattr(session, "date", None)
  st = getattr(session, "start_time", None)
  et = getattr(session, "end_time", None)
  if d and st and et:
    new_txt = f"{d:%d.%m.%Y} {st:%H:%M}–{et:%H:%M}"
  elif d and st:
    new_txt = f"{d:%d.%m.%Y} {st:%H:%M}"
  else:
    new_txt = f"{d or ''} {st or ''}–{et or ''}"

  return _notify_time_change_core(session, new_txt, None, "notify_time_change")
