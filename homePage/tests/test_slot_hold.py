"""
בדיקות שירות slot_hold — תפיסת סלוטים זמנית לפני תשלום.

הלוגיקה: לקוח בוחר שעה → המערכת "תופסת" את הסלוטים ל-TTL דקות (ברירת מחדל 15).
אם התשלום מצליח — הסלוט עובר לתפוס קבוע. אם לא — השחרור אוטומטי.
"""
from datetime import date, time, timedelta, datetime
import uuid

from django.test import TestCase
from django.utils import timezone

from homePage.models import Appointment
from homePage.services.slot_hold import (
    try_hold_chain, release_hold, release_expired_holds,
)

# קבועים לכל הבדיקות — יום שלישי קיץ (נקי מסוגיות DST)
_DATE = date(2025, 7, 15)
_TIME = time(10, 0)
_BASE_DT = datetime.combine(_DATE, _TIME)


def _make_slots(count=6):
    """יוצר רצף של `count` סלוטים של 15 דקות כל אחד, החל מ-_TIME."""
    return [
        Appointment.objects.create(date=_DATE, time=(_BASE_DT + timedelta(minutes=15 * i)).time())
        for i in range(count)
    ]


class TryHoldChainTest(TestCase):
    """
    try_hold_chain() מנסה לתפוס רצף סלוטים.
    כל setUp מתחיל מ-6 סלוטים פנויים (= 90 דקות אפשריות).
    """

    def setUp(self):
        _make_slots(count=6)

    def _hold(self, token, minutes=15):
        return try_hold_chain(
            token=token,
            date=_DATE,
            start_dt=_BASE_DT,
            minutes_total_for_hold=minutes,
        )

    # ── מקרי הצלחה ──────────────────────────────────────────────────

    def test_hold_single_slot_succeeds(self):
        """בקשת 15 דקות (סלוט אחד) חייבת להצליח."""
        self.assertTrue(self._hold(str(uuid.uuid4()), minutes=15).ok)

    def test_hold_two_slots_succeeds(self):
        """בקשת 30 דקות (2 סלוטים) חייבת להצליח."""
        self.assertTrue(self._hold(str(uuid.uuid4()), minutes=30).ok)

    def test_successful_hold_is_persisted(self):
        """אחרי hold מוצלח — hold_until חייב להיכתב ל-DB."""
        token = str(uuid.uuid4())
        self._hold(token, minutes=15)
        appt = Appointment.objects.get(date=_DATE, time=_TIME)
        self.assertIsNotNone(appt.hold_until)

    # ── מקרי כישלון ─────────────────────────────────────────────────

    def test_empty_token_fails(self):
        """טוקן ריק → כישלון עם reason='missing_token'."""
        r = self._hold("")
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "missing_token")

    def test_booked_slot_blocks_hold(self):
        """סלוט שתפוס (is_booked=True) → כישלון עם 'already_booked_or_break'."""
        Appointment.objects.filter(date=_DATE, time=_TIME).update(is_booked=True)
        r = self._hold(str(uuid.uuid4()))
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "already_booked_or_break")

    def test_break_slot_blocks_hold(self):
        """סלוט שמסומן כהפסקה (is_break=True) → כישלון."""
        Appointment.objects.filter(date=_DATE, time=_TIME).update(is_break=True)
        r = self._hold(str(uuid.uuid4()))
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "already_booked_or_break")

    def test_slot_held_by_other_token_fails(self):
        """סלוט שמוחזק ע״י טוקן אחר (ועוד בתוקף) → כישלון עם 'held_by_other'."""
        Appointment.objects.filter(date=_DATE, time=_TIME).update(
            hold_token=uuid.uuid4(),
            hold_until=timezone.now() + timedelta(minutes=10),
        )
        r = self._hold(str(uuid.uuid4()))
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "held_by_other")

    def test_not_enough_slots_fails(self):
        """דרושים 8 סלוטים (120 דק׳) אבל קיימים רק 6 → missing_slots."""
        r = self._hold(str(uuid.uuid4()), minutes=120)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "missing_slots")

    def test_expired_hold_on_slot_is_cleared_before_check(self):
        """
        סלוט עם hold שפג תוקפו — release_expired_holds רץ בתחילת try_hold_chain,
        לכן הסלוט אמור להיחשב פנוי ולהיתפס בהצלחה.
        """
        Appointment.objects.filter(date=_DATE, time=_TIME).update(
            hold_token=uuid.uuid4(),
            hold_until=timezone.now() - timedelta(minutes=5),  # פג
        )
        r = self._hold(str(uuid.uuid4()), minutes=15)
        self.assertTrue(r.ok)


# ─── release_hold ──────────────────────────────────────────────────

class ReleaseHoldTest(TestCase):
    """release_hold(token) — מנקה hold לפי טוקן."""

    def test_release_clears_hold_fields(self):
        token = uuid.uuid4()
        appt = Appointment.objects.create(
            date=_DATE, time=_TIME,
            hold_token=token,
            hold_until=timezone.now() + timedelta(minutes=10),
        )
        release_hold(str(token))
        appt.refresh_from_db()
        self.assertIsNone(appt.hold_until)
        self.assertIsNone(appt.hold_token)

    def test_release_empty_token_is_noop(self):
        """קריאה עם טוקן ריק לא אמורה לזרוק שגיאה."""
        release_hold("")

    def test_release_unknown_token_is_noop(self):
        """טוקן שלא קיים ב-DB — לא אמור לזרוק שגיאה."""
        release_hold(str(uuid.uuid4()))


# ─── release_expired_holds ─────────────────────────────────────────

class ReleaseExpiredHoldsTest(TestCase):
    """release_expired_holds() — מנקה את כל ה-hold שפג תוקפם."""

    def test_expired_hold_is_cleared(self):
        token = uuid.uuid4()
        appt = Appointment.objects.create(
            date=_DATE, time=_TIME,
            hold_token=token,
            hold_until=timezone.now() - timedelta(minutes=5),
        )
        release_expired_holds()
        appt.refresh_from_db()
        self.assertIsNone(appt.hold_until)
        self.assertIsNone(appt.hold_token)

    def test_active_hold_is_preserved(self):
        """hold שעוד בתוקף לא אמור להיות מנוקה."""
        token = uuid.uuid4()
        appt = Appointment.objects.create(
            date=_DATE, time=_TIME,
            hold_token=token,
            hold_until=timezone.now() + timedelta(minutes=10),
        )
        release_expired_holds()
        appt.refresh_from_db()
        self.assertIsNotNone(appt.hold_until)

    def test_only_expired_are_cleared(self):
        """כשיש גם hold פג וגם hold פעיל — רק הפג נמחק."""
        expired = Appointment.objects.create(
            date=_DATE, time=time(10, 0),
            hold_token=uuid.uuid4(),
            hold_until=timezone.now() - timedelta(minutes=1),
        )
        active = Appointment.objects.create(
            date=_DATE, time=time(10, 15),
            hold_token=uuid.uuid4(),
            hold_until=timezone.now() + timedelta(minutes=10),
        )
        release_expired_holds()
        expired.refresh_from_db()
        active.refresh_from_db()
        self.assertIsNone(expired.hold_until)
        self.assertIsNotNone(active.hold_until)
