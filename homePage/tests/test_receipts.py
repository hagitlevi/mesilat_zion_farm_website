from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from homePage.models import Activity, Booking, Payment, Receipt
from homePage.services.receipts import create_receipt_for_payment, create_manual_receipt


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
        self.assertEqual(receipt.items, [{"description": "רכיבה זוגית", "amount": "250"}])
        self.assertEqual(receipt.customer_name, "ישראל ישראלי")
        self.assertEqual(receipt.customer_email, "test@example.com")
        self.assertEqual(receipt.amount, 250)
        self.assertEqual(receipt.payment, payment)
        self.assertEqual(receipt.payment_method, "credit")

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

    def test_falls_back_to_booking_customer_email_when_payment_email_blank(self):
        activity = _activity()
        booking = _booking(activity)
        booking.customer_email = "from-booking@example.com"
        booking.save(update_fields=["customer_email"])
        payment = Payment.objects.create(
            amount_agorot=10000,
            status="succeeded",
            customer_name="ישראל ישראלי",
            email="",
            booking=booking,
        )

        receipt = create_receipt_for_payment(payment, booking)

        self.assertEqual(receipt.customer_email, "from-booking@example.com")

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
                items=[{"description": "y", "amount": "1"}],
                amount=1,
            )


class CreateManualReceiptTests(TestCase):
    def test_creates_receipt_with_multiple_items_and_computed_total(self):
        receipt = create_manual_receipt(
            customer_name="דנה כהן",
            customer_email="dana@example.com",
            items=[
                {"description": "רכיבה זוגית", "amount": "250"},
                {"description": "תוספת פיקניק", "amount": "80.5"},
            ],
            payment_method="cash",
        )

        self.assertTrue(receipt.receipt_number.startswith("A-"))
        self.assertEqual(receipt.amount, Decimal("330.5"))
        self.assertEqual(len(receipt.items), 2)
        self.assertIsNone(receipt.payment_id)
        self.assertEqual(receipt.payment_method, "cash")

    def test_shares_number_series_with_automatic_receipts(self):
        activity = _activity()
        booking = _booking(activity)
        payment = _payment(booking)
        auto_receipt = create_receipt_for_payment(payment, booking)

        manual_receipt = create_manual_receipt(
            customer_name="דנה כהן",
            customer_email="dana@example.com",
            items=[{"description": "רכיבה", "amount": "100"}],
            payment_method="bit",
        )

        self.assertTrue(auto_receipt.receipt_number.startswith("A-"))
        self.assertTrue(manual_receipt.receipt_number.startswith("A-"))
        self.assertGreater(manual_receipt.pk, auto_receipt.pk)

    def test_skips_rows_with_missing_description_or_amount(self):
        receipt = create_manual_receipt(
            customer_name="דנה כהן",
            customer_email="dana@example.com",
            items=[
                {"description": "רכיבה זוגית", "amount": "100"},
                {"description": "", "amount": "50"},
                {"description": "בלי סכום", "amount": ""},
            ],
            payment_method="credit",
        )

        self.assertEqual(receipt.items, [{"description": "רכיבה זוגית", "amount": "100"}])
        self.assertEqual(receipt.amount, Decimal("100"))

    def test_raises_when_no_valid_items(self):
        with self.assertRaises(ValueError):
            create_manual_receipt(
                customer_name="דנה כהן",
                customer_email="dana@example.com",
                items=[{"description": "", "amount": ""}],
                payment_method="cash",
            )

    def test_cheque_requires_bank_details(self):
        with self.assertRaises(ValueError):
            create_manual_receipt(
                customer_name="דנה כהן",
                customer_email="dana@example.com",
                items=[{"description": "רכיבה", "amount": "100"}],
                payment_method="cheque",
            )

    def test_cheque_with_details_succeeds(self):
        receipt = create_manual_receipt(
            customer_name="דנה כהן",
            customer_email="dana@example.com",
            items=[{"description": "רכיבה", "amount": "100"}],
            payment_method="cheque",
            cheque_bank="בנק הפועלים",
            cheque_branch="123",
            cheque_due_date="01.09.2026",
        )

        self.assertEqual(receipt.cheque_bank, "בנק הפועלים")
        self.assertEqual(receipt.cheque_branch, "123")
        self.assertEqual(receipt.cheque_due_date, "01.09.2026")
