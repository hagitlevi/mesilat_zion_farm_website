from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from homePage.models import Activity, Booking, Payment, Receipt
from homePage.services.receipts import create_receipt_for_payment


def _activity():
    return Activity.objects.create(name="רכיבה זוגית", description="", duration_minutes=60)


def _booking(activity):
    now = timezone.now()
    return Booking.objects.create(
        activity=activity,
        start_dt=now,
        end_dt=now + timedelta(hours=1),
        customer_name="ישראל ישראלי",
        status="paid",
    )


def _payment(booking):
    return Payment.objects.create(
        amount_agorot=25000,
        status="succeeded",
        customer_name="ישראל ישראלי",
        email="test@example.com",
        booking=booking,
    )


class CreateReceiptForPaymentTests(TestCase):
    def test_creates_receipt_with_sequential_prefixed_number_and_snapshot_fields(self):
        activity = _activity()
        booking = _booking(activity)
        payment = _payment(booking)

        receipt = create_receipt_for_payment(payment, booking)

        self.assertTrue(receipt.receipt_number.startswith("A-"))
        self.assertEqual(receipt.receipt_number, f"A-{receipt.pk:06d}")
        self.assertEqual(receipt.activity_name, "רכיבה זוגית")
        self.assertEqual(receipt.customer_name, "ישראל ישראלי")
        self.assertEqual(receipt.amount, 250)
        self.assertEqual(receipt.payment, payment)

    def test_numbers_increase_across_separate_receipts(self):
        activity = _activity()
        booking1 = _booking(activity)
        payment1 = _payment(booking1)
        booking2 = _booking(activity)
        payment2 = _payment(booking2)

        receipt1 = create_receipt_for_payment(payment1, booking1)
        receipt2 = create_receipt_for_payment(payment2, booking2)

        self.assertLess(receipt1.pk, receipt2.pk)
        self.assertNotEqual(receipt1.receipt_number, receipt2.receipt_number)

    def test_falls_back_to_booking_customer_name_when_payment_name_blank(self):
        activity = _activity()
        booking = _booking(activity)
        payment = Payment.objects.create(
            amount_agorot=10000,
            status="succeeded",
            customer_name="",
            email="test2@example.com",
            booking=booking,
        )

        receipt = create_receipt_for_payment(payment, booking)

        self.assertEqual(receipt.customer_name, "ישראל ישראלי")

    def test_receipt_number_is_unique_constrained(self):
        activity = _activity()
        booking = _booking(activity)
        payment = _payment(booking)
        receipt = create_receipt_for_payment(payment, booking)

        with self.assertRaises(Exception):
            Receipt.objects.create(
                receipt_number=receipt.receipt_number,
                payment=Payment.objects.create(amount_agorot=100, booking=_booking(activity)),
                customer_name="x",
                activity_name="y",
                amount=1,
            )
