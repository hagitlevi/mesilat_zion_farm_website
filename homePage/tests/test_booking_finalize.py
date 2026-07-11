"""
בדיקות בטחון קריטיות — תור שמור רק ללקוח שתשלומו עבר.

A) finalize_hold_to_paid_booking() — ממיר hold → הזמנה אמיתית לאחר תשלום
B) כישלון finalize — שגיאות נכונות, DB לא משתנה (אטומיות)
C) מרוץ בין שני לקוחות — רק אחד תופס
D) תשלום נכשל → release_hold → הסלוט פנוי לשני
E) Flow קצה-לקצה: hold → finalize → לקוח אחר חסום
"""
import threading
import uuid
from datetime import date, time, timedelta, datetime

from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from homePage.models import Activity, Appointment, Booking
from homePage.services.slot_hold import (
    finalize_hold_to_paid_booking,
    release_hold,
    try_hold_chain,
)

_DATE = date(2025, 7, 15)
_TIME = time(10, 0)
_BASE_DT = datetime.combine(_DATE, _TIME)


# ─── עזר ───────────────────────────────────────────────────────────

def _activity():
    return Activity.objects.create(name="רכיבה", description="", duration_minutes=30)


def _booking(activity):
    now = timezone.now()
    return Booking.objects.create(
        activity=activity,
        start_dt=now,
        end_dt=now + timedelta(hours=1),
        payment_ref=f"MZ-{uuid.uuid4().hex[:8].upper()}",
        status="pending",
    )


def _slots(count=4):
    return [
        Appointment.objects.create(
            date=_DATE,
            time=(_BASE_DT + timedelta(minutes=15 * i)).time(),
        )
        for i in range(count)
    ]


def _hold(token, minutes=30):
    return try_hold_chain(
        token=token, date=_DATE, start_dt=_BASE_DT, minutes_total_for_hold=minutes
    )


def _finalize(token, booking, activity, minutes_real=30):
    finalize_hold_to_paid_booking(
        token=token,
        booking=booking,
        activity=activity,
        date=_DATE,
        start_dt=_BASE_DT,
        minutes_real=minutes_real,
    )


# ─── A. finalize מוצלח ─────────────────────────────────────────────

class FinalizeSuccessTest(TestCase):
    """hold פעיל + תשלום עבר → סלוטים נעולים לצמיתות."""

    def setUp(self):
        self.activity = _activity()
        _slots(count=4)
        self.token = str(uuid.uuid4())
        _hold(self.token, minutes=30)  # 2 סלוטים
        self.booking = _booking(self.activity)
        _finalize(self.token, self.booking, self.activity, minutes_real=30)

    def _booked_slots(self):
        return Appointment.objects.filter(
            date=_DATE, time__in=[time(10, 0), time(10, 15)]
        )

    def test_is_booked_true(self):
        """כל סלוט בטווח חייב להיות is_booked=True."""
        for s in self._booked_slots():
            self.assertTrue(s.is_booked, f"סלוט {s.time} לא תפוס")

    def test_is_paid_true(self):
        """כל סלוט בטווח חייב להיות is_paid=True."""
        for s in self._booked_slots():
            self.assertTrue(s.is_paid, f"סלוט {s.time} לא משולם")

    def test_hold_fields_cleared(self):
        """hold_until ו-hold_token חייבים להתנקות לאחר finalize."""
        for s in self._booked_slots():
            self.assertIsNone(s.hold_until, f"hold_until לא נוקה ב-{s.time}")
            self.assertIsNone(s.hold_token, f"hold_token לא נוקה ב-{s.time}")

    def test_slots_linked_to_correct_booking(self):
        """כל הסלוטים מקושרים ל-Booking הנכון."""
        for s in self._booked_slots():
            self.assertEqual(s.booking_id, self.booking.id)

    def test_slot_outside_range_remains_free(self):
        """סלוט שמחוץ לטווח הבקשה חייב להישאר פנוי."""
        later = Appointment.objects.get(date=_DATE, time=time(10, 30))
        self.assertFalse(later.is_booked)
        self.assertFalse(later.is_paid)


# ─── B. finalize כישלון — שגיאות ואטומיות ─────────────────────────

class FinalizeFailureTest(TestCase):
    """finalize חייב לזרוק ValueError ולא לשנות DB."""

    def setUp(self):
        self.activity = _activity()
        _slots(count=4)

    def test_no_hold_at_all_raises_hold_expired(self):
        """אין hold בכלל → hold_expired (לקוח דיפק בלי לתפוס)."""
        with self.assertRaises(ValueError) as ctx:
            _finalize(str(uuid.uuid4()), _booking(self.activity), self.activity)
        self.assertIn("hold_expired", str(ctx.exception))

    def test_expired_hold_raises_hold_expired(self):
        """hold פג תוקף בזמן שהלקוח שילם → hold_expired."""
        token = str(uuid.uuid4())
        Appointment.objects.filter(date=_DATE, time=time(10, 0)).update(
            hold_token=uuid.UUID(token),
            hold_until=timezone.now() - timedelta(minutes=1),
        )
        with self.assertRaises(ValueError) as ctx:
            _finalize(token, _booking(self.activity), self.activity, minutes_real=15)
        self.assertIn("hold_expired", str(ctx.exception))

    def test_already_booked_slot_raises(self):
        """סלוט שכבר תפוס (is_booked=True) → already_booked."""
        token = str(uuid.uuid4())
        Appointment.objects.filter(date=_DATE, time=time(10, 0)).update(
            is_booked=True,
            hold_token=uuid.UUID(token),
            hold_until=timezone.now() + timedelta(minutes=10),
        )
        with self.assertRaises(ValueError) as ctx:
            _finalize(token, _booking(self.activity), self.activity, minutes_real=15)
        self.assertIn("already_booked", str(ctx.exception))

    def test_slot_held_by_other_raises(self):
        """הסלוט מוחזק ע״י טוקן אחר (ובתוקף) → held_by_other."""
        other = str(uuid.uuid4())
        my_token = str(uuid.uuid4())
        Appointment.objects.filter(date=_DATE, time=time(10, 0)).update(
            hold_token=uuid.UUID(other),
            hold_until=timezone.now() + timedelta(minutes=10),
        )
        with self.assertRaises(ValueError) as ctx:
            _finalize(my_token, _booking(self.activity), self.activity, minutes_real=15)
        self.assertIn("held_by_other", str(ctx.exception))

    def test_atomicity_first_slot_not_finalized_when_second_fails(self):
        """
        סלוט 1 תקין, סלוט 2 כבר תפוס → finalize נכשל על סלוט 2.
        בגלל אטומיות — סלוט 1 לא אמור להשתנות.
        """
        token = str(uuid.uuid4())
        Appointment.objects.filter(date=_DATE, time=time(10, 0)).update(
            hold_token=uuid.UUID(token),
            hold_until=timezone.now() + timedelta(minutes=10),
        )
        Appointment.objects.filter(date=_DATE, time=time(10, 15)).update(
            is_booked=True,
        )
        with self.assertRaises(ValueError):
            _finalize(token, _booking(self.activity), self.activity, minutes_real=30)

        first = Appointment.objects.get(date=_DATE, time=time(10, 0))
        self.assertFalse(first.is_booked, "סלוט 1 לא אמור להיתפס (rollback)")
        self.assertFalse(first.is_paid)


# ─── C. מרוץ בין שני לקוחות ────────────────────────────────────────

class RaceConditionSequentialTest(TestCase):
    """שני לקוחות מנסים לתפוס את אותו סלוט — רק אחד מנצח."""

    def setUp(self):
        _slots(count=2)

    def test_second_hold_fails_with_held_by_other(self):
        """קריאה שנייה לאותו סלוט (ע״י טוקן אחר) חייבת להיכשל."""
        token_a = str(uuid.uuid4())
        token_b = str(uuid.uuid4())

        result_a = _hold(token_a, minutes=15)
        result_b = _hold(token_b, minutes=15)

        self.assertTrue(result_a.ok, "לקוח A (ראשון) חייב לנצח")
        self.assertFalse(result_b.ok, "לקוח B (שני) חייב להיכשל")
        self.assertEqual(result_b.reason, "held_by_other")

    def test_winner_token_stored_in_db(self):
        """הטוקן של המנצח — ורק הוא — נשמר ב-DB."""
        token_a = str(uuid.uuid4())
        token_b = str(uuid.uuid4())

        _hold(token_a, minutes=15)
        _hold(token_b, minutes=15)

        appt = Appointment.objects.get(date=_DATE, time=time(10, 0))
        self.assertEqual(str(appt.hold_token), token_a)

    def test_after_first_hold_finalized_second_can_hold(self):
        """
        לקוח A מסיים תור (finalize) → לקוח B מנסה לתפוס slot אחר — מצליח.
        (מוודא שה-finalize לא חוסם סלוטים שאינם בטווח.)
        """
        activity = _activity()
        token_a = str(uuid.uuid4())
        _hold(token_a, minutes=15)  # תופס slot 10:00
        booking_a = _booking(activity)
        _finalize(token_a, booking_a, activity, minutes_real=15)

        # slot 10:15 חייב להיות פנוי
        token_b = str(uuid.uuid4())
        result_b = try_hold_chain(
            token=token_b,
            date=_DATE,
            start_dt=_BASE_DT + timedelta(minutes=15),
            minutes_total_for_hold=15,
        )
        self.assertTrue(result_b.ok)


class RaceConditionConcurrentTest(TransactionTestCase):
    """
    שני threads אמיתיים — select_for_update מבטיח שרק אחד יתפוס.
    (TransactionTestCase נדרש כדי שה-threads יראו commits אמיתיים.)
    """

    def setUp(self):
        _slots(count=2)

    def tearDown(self):
        Appointment.objects.all().delete()
        Booking.objects.all().delete()
        Activity.objects.all().delete()

    def test_concurrent_holds_only_one_wins(self):
        results = []
        errors = []

        def do_hold(token):
            try:
                r = try_hold_chain(
                    token=token, date=_DATE, start_dt=_BASE_DT, minutes_total_for_hold=15
                )
                results.append(r)
            except Exception as e:
                errors.append(e)

        token_a = str(uuid.uuid4())
        token_b = str(uuid.uuid4())

        t1 = threading.Thread(target=do_hold, args=(token_a,))
        t2 = threading.Thread(target=do_hold, args=(token_b,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(errors), 0, f"שגיאות בלתי צפויות: {errors}")
        winners = [r for r in results if r.ok]
        losers = [r for r in results if not r.ok]
        self.assertEqual(len(winners), 1, "בדיוק לקוח אחד חייב לנצח")
        self.assertEqual(len(losers), 1, "בדיוק לקוח אחד חייב להפסיד")


# ─── D. תשלום נכשל → שחרור ─────────────────────────────────────────

class PaymentFailureReleaseTest(TestCase):
    """כשתשלום נכשל — הסלוט חייב להשתחרר ולהיות זמין ללקוח הבא."""

    def setUp(self):
        _slots(count=2)

    def test_slot_available_to_next_customer_after_failed_payment(self):
        """
        זרימה: A תופס → תשלום נכשל → release_hold → B מצליח לתפוס.
        """
        token_a = str(uuid.uuid4())
        self.assertTrue(_hold(token_a, minutes=15).ok)

        release_hold(token_a)  # תשלום נכשל

        token_b = str(uuid.uuid4())
        result_b = _hold(token_b, minutes=15)
        self.assertTrue(result_b.ok, "B חייב לתפוס את הסלוט שהשתחרר")

    def test_slot_not_booked_after_failed_payment(self):
        """אחרי כשל תשלום — is_booked חייב להישאר False."""
        token = str(uuid.uuid4())
        _hold(token, minutes=15)
        release_hold(token)

        appt = Appointment.objects.get(date=_DATE, time=time(10, 0))
        self.assertFalse(appt.is_booked)
        self.assertFalse(appt.is_paid)

    def test_hold_token_cleared_after_failed_payment(self):
        """אחרי כשל תשלום — hold_token ו-hold_until חייבים להיות None."""
        token = str(uuid.uuid4())
        _hold(token, minutes=15)
        release_hold(token)

        appt = Appointment.objects.get(date=_DATE, time=time(10, 0))
        self.assertIsNone(appt.hold_token)
        self.assertIsNone(appt.hold_until)

    def test_original_booking_not_created_after_failed_payment(self):
        """
        אחרי כשל תשלום — לא אמור להיווצר Booking עם is_booked=True.
        (finalize לא נקרא כלל כשהתשלום נכשל.)
        """
        token = str(uuid.uuid4())
        _hold(token, minutes=15)
        release_hold(token)

        self.assertFalse(
            Appointment.objects.filter(date=_DATE, is_booked=True).exists()
        )


# ─── E. Flow קצה-לקצה ────────────────────────────────────────────────

class EndToEndFlowTest(TestCase):
    """זרימה מלאה — סלוט שמור חד-משמעית רק ללקוח שתשלומו עבר."""

    def setUp(self):
        self.activity = _activity()
        _slots(count=4)

    def test_second_customer_blocked_after_first_finalizes(self):
        """
        A: hold → finalize (תשלום הצליח).
        B: מנסה לתפוס → חייב להיכשל עם already_booked_or_break.
        """
        token_a = str(uuid.uuid4())
        _hold(token_a, minutes=15)
        _finalize(token_a, _booking(self.activity), self.activity, minutes_real=15)

        token_b = str(uuid.uuid4())
        result_b = _hold(token_b, minutes=15)
        self.assertFalse(result_b.ok)
        self.assertEqual(result_b.reason, "already_booked_or_break")

    def test_finalize_without_prior_hold_is_rejected(self):
        """
        לקוח מנסה לסיים תשלום בלי שתפס תור מראש → hold_expired.
        """
        with self.assertRaises(ValueError) as ctx:
            _finalize(str(uuid.uuid4()), _booking(self.activity), self.activity)
        self.assertIn("hold_expired", str(ctx.exception))

    def test_two_customers_different_slots_both_succeed(self):
        """
        שני לקוחות בשני סלוטים שונים — שניהם צריכים להצליח."""
        act = self.activity
        token_a = str(uuid.uuid4())
        token_b = str(uuid.uuid4())

        _hold(token_a, minutes=15)  # תופס 10:00

        result_b = try_hold_chain(
            token=token_b,
            date=_DATE,
            start_dt=_BASE_DT + timedelta(minutes=15),  # 10:15
            minutes_total_for_hold=15,
        )
        self.assertTrue(result_b.ok, "B אמור לתפוס סלוט אחר בהצלחה")

        booking_a = _booking(act)
        _finalize(token_a, booking_a, act, minutes_real=15)

        booking_b = _booking(act)
        finalize_hold_to_paid_booking(
            token=token_b,
            booking=booking_b,
            activity=act,
            date=_DATE,
            start_dt=_BASE_DT + timedelta(minutes=15),
            minutes_real=15,
        )

        slot_a = Appointment.objects.get(date=_DATE, time=time(10, 0))
        slot_b = Appointment.objects.get(date=_DATE, time=time(10, 15))
        self.assertTrue(slot_a.is_booked)
        self.assertEqual(slot_a.booking_id, booking_a.id)
        self.assertTrue(slot_b.is_booked)
        self.assertEqual(slot_b.booking_id, booking_b.id)

    def test_complete_booking_fields_after_finalize(self):
        """
        לאחר finalize מוצלח — כל השדות הקריטיים נכונים.
        """
        token = str(uuid.uuid4())
        _hold(token, minutes=15)
        booking = _booking(self.activity)
        _finalize(token, booking, self.activity, minutes_real=15)

        appt = Appointment.objects.get(date=_DATE, time=time(10, 0))
        self.assertTrue(appt.is_booked)
        self.assertTrue(appt.is_paid)
        self.assertFalse(appt.is_break)
        self.assertIsNone(appt.hold_until)
        self.assertIsNone(appt.hold_token)
        self.assertEqual(appt.booking_id, booking.id)
        self.assertEqual(appt.activity_id, self.activity.id)
