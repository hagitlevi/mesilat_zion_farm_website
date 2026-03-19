from homePage.models import TermsConsent
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from django.conf import settings
from homePage.services.ntfy_gateway import normalize_phone_il
import logging

logger = logging.getLogger(__name__)

def _save_consent_by_phone(request, phone: str, full_name: str = ""):
  """שומר הסכמה לתנאים ולפרטיות לפי הטלפון"""
  logger.debug("_save_consent_by_phone called with phone: %s, full_name: %s", phone, full_name)

  sid = normalize_phone_il(phone)
  if not sid:
    return
  ip = request.META.get("REMOTE_ADDR")
  ua = (request.META.get("HTTP_USER_AGENT") or "")[:255]
  tv = getattr(settings, "TERMS_VERSION", "1.0")
  pv = getattr(settings, "PRIVACY_VERSION", "1.0")

  TermsConsent.objects.get_or_create(
    policy="terms", version=tv, subject_id=sid,
    defaults={"full_name": full_name, "ip": ip, "user_agent": ua}
  )
  TermsConsent.objects.get_or_create(
    policy="privacy", version=pv, subject_id=sid,
    defaults={"full_name": full_name, "ip": ip, "user_agent": ua}
  )

def _has_consent_by_phone(phone: str) -> bool:
    """בודק אם יש כבר הסכמה לתנאים ולפרטיות לפי הטלפון (ללא קוקיז)"""
    logger.debug("_has_consent_by_phone called with phone: %s", phone)

    sid = normalize_phone_il(phone)
    if not sid:
      return False
    tv = getattr(settings, "TERMS_VERSION", "1.0")
    pv = getattr(settings, "PRIVACY_VERSION", "1.0")
    return (
        TermsConsent.objects.filter(policy="terms", version=tv, subject_id=sid).exists() and
        TermsConsent.objects.filter(policy="privacy", version=pv, subject_id=sid).exists()
    )

@require_GET
def consent_status(request):
  """API: מחזיר אם צריך צ'קבוקס בהתאם לטלפון ולגרסאות הנוכחיות (בלי קוקיז)"""
  logger.debug("consent_status called with method: %s", request.method)

  phone = request.GET.get("phone") or ""
  needs = not _has_consent_by_phone(phone)
  return JsonResponse({
    "needs_consent": needs,
    "versions": {"terms": settings.TERMS_VERSION, "privacy": settings.PRIVACY_VERSION}
  })
