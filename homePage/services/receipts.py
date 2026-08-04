import logging
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from homePage.models import Receipt

logger = logging.getLogger(__name__)


def _assign_receipt_number(receipt: Receipt) -> None:
  """שומר את הקבלה פעמיים: שמירה ראשונה עם ערך זמני ייחודי כדי לקבל pk, ואז קביעת
  receipt_number הסופי (סדרה A- משותפת לקבלות אוטומטיות וידניות כאחד)."""
  receipt.receipt_number = f"TMP-{uuid.uuid4().hex}"
  receipt.save()
  receipt.receipt_number = f"A-{receipt.pk:06d}"
  receipt.save(update_fields=["receipt_number"])


def create_receipt_for_payment(payment, booking) -> Receipt:
  """יוצר קבלה דיגיטלית (snapshot) עבור תשלום שהצליח. לא נועד להיקרא יותר מפעם אחת לאותו payment."""
  activity_name = getattr(getattr(booking, "activity", None), "name", "") or ""
  customer_name = (getattr(payment, "customer_name", "") or getattr(booking, "customer_name", "") or "").strip()
  amount = Decimal(getattr(payment, "amount_agorot", 0) or 0) / 100

  receipt = Receipt(
      payment=payment,
      customer_name=customer_name,
      items=[{"description": activity_name, "amount": str(amount)}],
      amount=amount,
      payment_method="credit",
  )
  _assign_receipt_number(receipt)
  return receipt


def create_manual_receipt(*, customer_name, customer_email, items, payment_method,
                           cheque_bank="", cheque_branch="", cheque_due_date="", created_by=None) -> Receipt:
  """יוצר קבלה ידנית מהאדמין. `items` הוא רשימת dict-ים עם description/amount (טקסט/מחרוזת).
  זורק ValueError עם הודעה בעברית אם הקלט לא תקין."""
  cleaned_items = []
  total = Decimal("0")
  for item in items:
    description = (item.get("description") or "").strip()
    amount_raw = item.get("amount")
    if not description or amount_raw in (None, ""):
      continue
    try:
      amount = Decimal(str(amount_raw))
    except (InvalidOperation, ValueError):
      continue
    if amount <= 0:
      continue
    cleaned_items.append({"description": description, "amount": str(amount)})
    total += amount

  if not cleaned_items:
    raise ValueError("יש להזין לפחות פריט אחד עם תיאור וסכום תקין")

  if not (customer_name or "").strip():
    raise ValueError("יש להזין שם לקוח")

  if not (customer_email or "").strip():
    raise ValueError("יש להזין כתובת מייל")

  is_cheque = payment_method == "cheque"
  if is_cheque and not (cheque_bank.strip() and cheque_branch.strip() and cheque_due_date.strip()):
    raise ValueError("לתשלום בשיק יש למלא בנק, סניף וזמן פירעון")

  receipt = Receipt(
      customer_name=customer_name.strip(),
      customer_email=customer_email.strip(),
      items=cleaned_items,
      amount=total,
      payment_method=payment_method,
      cheque_bank=cheque_bank.strip() if is_cheque else "",
      cheque_branch=cheque_branch.strip() if is_cheque else "",
      cheque_due_date=cheque_due_date.strip() if is_cheque else "",
      created_by=created_by,
  )
  _assign_receipt_number(receipt)
  return receipt


def render_receipt_pdf(receipt: Receipt) -> bytes:
  """מרנדר את הקבלה כ-PDF. ה-import של weasyprint נמצא כאן (ולא בראש הקובץ) כי הוא תלוי בספריות
  מערכת (Pango/Cairo) שלא בהכרח מותקנות בסביבת פיתוח, ולא צריכות לחסום ייבוא/עבודה עם שאר האתר."""
  from weasyprint import HTML

  signature_path = finders.find("homePage/images/sign.png")
  signature_uri = Path(signature_path).as_uri() if signature_path else None

  logo_path = finders.find("homePage/images/logo.png")
  logo_uri = Path(logo_path).as_uri() if logo_path else None

  html_string = render_to_string(
      "homePage/receipt_pdf.html",
      {"receipt": receipt, "signature_uri": signature_uri, "logo_uri": logo_uri},
  )
  return HTML(string=html_string).write_pdf()


def send_manual_receipt_email(receipt: Receipt) -> bool:
  """שולח קבלה ידנית ללקוח במייל, עם ה-PDF מצורף. מחזיר True אם נשלח בפועל."""
  if not getattr(settings, "SEND_EMAIL", False):
    return False
  if not receipt.customer_email:
    return False

  subject = f"קבלה – חוות מסילת ציון · {receipt.receipt_number}"
  text_body = (
      f"שלום {receipt.customer_name},\n\n"
      f"מצורפת קבלה מספר {receipt.receipt_number} על סך ₪{receipt.amount}.\n\n"
      "זהו מייל אוטומטי – אין להשיב אליו."
  )

  email = EmailMultiAlternatives(
      subject,
      text_body,
      getattr(settings, "DEFAULT_FROM_EMAIL", None),
      [receipt.customer_email],
  )

  try:
    pdf_bytes = render_receipt_pdf(receipt)
    email.attach(f"{receipt.receipt_number}.pdf", pdf_bytes, "application/pdf")
  except Exception:
    logger.exception("send_manual_receipt_email: PDF rendering failed for receipt %s", receipt.receipt_number)
    return False

  try:
    sent = email.send(fail_silently=False)
  except Exception:
    logger.exception("send_manual_receipt_email: send failed for receipt %s", receipt.receipt_number)
    return False
  return sent >= 1
