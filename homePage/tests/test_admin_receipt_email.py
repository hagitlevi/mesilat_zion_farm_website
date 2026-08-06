"""
בדיקות לעריכת כתובת המייל של קבלה קיימת דרך עמוד הקבלה באדמין (ReceiptAdmin -
customer_email הוא השדה היחיד שאינו readonly, ראו get_readonly_fields), ולשליחה חוזרת
של קבלה במייל (action resend_receipt_email) - בלי לגעת בקבלה עצמה (סכום/פריטים/מספור)
ובלי לפתוח שדות אחרים לעריכה.
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from homePage.models import Receipt
from homePage.services.receipts import create_manual_receipt


def _receipt(email="dana@example.com"):
    return create_manual_receipt(
        customer_name="דנה כהן",
        customer_email=email,
        items=[{"description": "רכיבה זוגית", "amount": "250"}],
        payment_method="cash",
    )


@override_settings(SEND_EMAIL=True)
class ChangeReceiptEmailFieldTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_superuser(
            username="staff1", email="staff1@example.com", password="pw12345!"
        )
        self.client.force_login(self.staff)
        self.receipt = _receipt()
        self.url = reverse("admin:homePage_receipt_change", args=[self.receipt.pk])

    def test_get_shows_editable_email_field(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.receipt.receipt_number)
        self.assertContains(
            resp, f'value="{self.receipt.customer_email}"'
        )

    def test_receipt_number_field_is_not_an_editable_input(self):
        resp = self.client.get(self.url)
        self.assertNotContains(
            resp, f'name="receipt_number" value="{self.receipt.receipt_number}"'
        )

    def test_post_updates_email(self):
        data = self._form_data(customer_email="new@example.com")
        resp = self.client.post(self.url, data)

        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.customer_email, "new@example.com")
        self.assertRedirects(resp, reverse("admin:homePage_receipt_changelist"))

    def test_does_not_change_other_receipt_fields(self):
        original_items = self.receipt.items
        original_amount = self.receipt.amount
        original_number = self.receipt.receipt_number
        original_name = self.receipt.customer_name

        data = self._form_data(customer_email="new@example.com")
        self.client.post(self.url, data)

        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.items, original_items)
        self.assertEqual(self.receipt.amount, original_amount)
        self.assertEqual(self.receipt.receipt_number, original_number)
        self.assertEqual(self.receipt.customer_name, original_name)

    def test_rejects_invalid_email(self):
        data = self._form_data(customer_email="not-an-email")
        resp = self.client.post(self.url, data)

        self.receipt.refresh_from_db()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.receipt.customer_email, "dana@example.com")

    def test_no_delete_button_or_permission(self):
        resp = self.client.get(self.url)
        self.assertNotContains(resp, 'class="deletelink"')

        delete_url = reverse("admin:homePage_receipt_delete", args=[self.receipt.pk])
        resp = self.client.post(delete_url, {"post": "yes"})
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Receipt.objects.filter(pk=self.receipt.pk).exists())

    def _form_data(self, **overrides):
        # דורש להעביר את כל שדות הטופס כי כל השדות (חוץ מ-customer_email) הם readonly
        # ולכן לא באמת חלק מה-form, אבל customer_email עצמו כן צריך POST תקין.
        data = {"customer_email": self.receipt.customer_email}
        data.update(overrides)
        return data


@override_settings(SEND_EMAIL=True)
class ResendReceiptEmailActionTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_superuser(
            username="staff2", email="staff2@example.com", password="pw12345!"
        )
        self.client.force_login(self.staff)
        self.changelist_url = reverse("admin:homePage_receipt_changelist")

    def _post_action(self, action, pks):
        return self.client.post(
            self.changelist_url,
            {
                "action": action,
                "_selected_action": [str(pk) for pk in pks],
            },
            follow=True,
        )

    def test_resend_action_sends_email_for_each_selected_receipt(self):
        r1 = _receipt(email="a@example.com")
        r2 = _receipt(email="b@example.com")

        with patch("homePage.services.receipts.render_receipt_pdf", return_value=b"%PDF-fake"):
            self._post_action("resend_receipt_email", [r1.pk, r2.pk])

        self.assertEqual(len(mail.outbox), 2)
        sent_to = sorted(m.to[0] for m in mail.outbox)
        self.assertEqual(sent_to, ["a@example.com", "b@example.com"])

    def test_resend_action_does_not_modify_receipt_fields(self):
        r1 = _receipt(email="a@example.com")
        original_amount = r1.amount
        original_items = r1.items

        with patch("homePage.services.receipts.render_receipt_pdf", return_value=b"%PDF-fake"):
            self._post_action("resend_receipt_email", [r1.pk])

        r1.refresh_from_db()
        self.assertEqual(r1.amount, original_amount)
        self.assertEqual(r1.items, original_items)

    def test_resend_action_reports_failure_for_receipt_without_email(self):
        activity_receipt = Receipt.objects.create(
            receipt_number="A-999999",
            customer_name="ללא מייל",
            customer_email="",
            items=[{"description": "x", "amount": "10"}],
            amount=Decimal("10"),
            payment_method="cash",
        )

        resp = self._post_action("resend_receipt_email", [activity_receipt.pk])

        self.assertEqual(len(mail.outbox), 0)
        messages_text = " ".join(m.message for m in resp.context["messages"])
        self.assertIn(activity_receipt.receipt_number, messages_text)