from django.urls import reverse
from .base import PaymentProvider

class MockProvider(PaymentProvider):
    def create_checkout_session(self, payment, request):
        url = request.build_absolute_uri(
            reverse('mock_checkout', kwargs={'payment_id': payment.id})
        )
        return {"redirect_url": url, "session_id": f"mock-session-{payment.id}"}

    def handle_webhook(self, request):
        pid = int(request.POST.get('payment_id'))
        outcome = request.POST.get('outcome')
        status_map = {'success': 'succeeded', 'fail': 'failed', 'cancel': 'canceled'}
        return {
            "ok": True,
            "payment_id": pid,
            "status": status_map.get(outcome, 'failed'),
            "charge_id": f"mock-charge-{pid}" if outcome == 'success' else "",
            "raw": {"post": dict(request.POST.items())}
        }
