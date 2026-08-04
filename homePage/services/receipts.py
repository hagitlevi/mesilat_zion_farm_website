import logging
from decimal import Decimal
from pathlib import Path

from django.contrib.staticfiles import finders
from django.template.loader import render_to_string

from homePage.models import Receipt

logger = logging.getLogger(__name__)


def create_receipt_for_payment(payment, booking) -> Receipt:
  """יוצר קבלה דיגיטלית (snapshot) עבור תשלום שהצליח. לא נועד להיקרא יותר מפעם אחת לאותו payment."""
  activity_name = getattr(getattr(booking, "activity", None), "name", "") or ""
  customer_name = (getattr(payment, "customer_name", "") or getattr(booking, "customer_name", "") or "").strip()
  amount = Decimal(getattr(payment, "amount_agorot", 0) or 0) / 100

  receipt = Receipt(
      payment=payment,
      customer_name=customer_name,
      activity_name=activity_name,
      amount=amount,
      receipt_number=f"TMP-{payment.pk}",
  )
  receipt.save()
  receipt.receipt_number = f"A-{receipt.pk:06d}"
  receipt.save(update_fields=["receipt_number"])
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
