"""סיכום הכנסות: קבלות עצמאיות נספרות, קבלות מבוטלות/קבלות ההזמנות לא, ותמיכה ברצף חודשים."""
from datetime import datetime
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from homePage.admin import _period_summary
from homePage.services.receipts import create_manual_receipt

TZ = ZoneInfo("Asia/Jerusalem")


def _receipt(amount, standalone):
    r = create_manual_receipt(
        customer_name="דנה", customer_email="d@example.com",
        items=[{"description": "x", "amount": str(amount)}],
        payment_method="cash", standalone=standalone,
    )
    r.save()
    return r


class MonthlySummaryTests(TestCase):
    def test_only_standalone_non_void_receipts_counted(self):
        _receipt(100, standalone=True)
        void = _receipt(40, standalone=True)
        void.is_void = True
        void.save()
        _receipt(999, standalone=False)  # נוצרה עם סימון הזמנה - כבר נספרת דרך ההזמנה
        s = _period_summary(datetime(2000, 1, 1, tzinfo=TZ), datetime(2100, 1, 1, tzinfo=TZ))
        self.assertEqual((s["receipts_count"], s["receipts_revenue"], s["total_revenue"]), (1, "₪ 100", "₪ 100"))
        self.assertEqual((s["other_manual_count"], s["other_manual_total"]), (1, "₪ 999"))

    def test_view_renders_for_single_month_range_and_legacy_params(self):
        user = get_user_model().objects.create_superuser("a", "a@example.com", "pw")
        self.client.force_login(user)
        url = reverse("admin:homePage_monthlysummary_changelist")
        for qs in ("", "?year=2026&month=3", "?from=2026-01&to=2026-03", "?from=2026-03&to=2026-01", "?from=junk"):
            self.assertEqual(self.client.get(url + qs).status_code, 200, qs)
        self.assertContains(self.client.get(url + "?from=2026-01&to=2026-03"), "לפי חודש")
