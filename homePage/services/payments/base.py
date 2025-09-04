from abc import ABC, abstractmethod

class PaymentProvider(ABC):
    @abstractmethod
    def create_checkout_session(self, payment, request):
        """return {'redirect_url': ..., 'session_id': ...}"""
        raise NotImplementedError

    @abstractmethod
    def handle_webhook(self, request):
        """return {'ok':bool,'payment_id':int,'status':str,'charge_id':str,'raw':dict}"""
        raise NotImplementedError

def get_provider(name='mock'):
    if name == 'mock':
        from .mock import MockProvider
        return MockProvider()
    raise ValueError(f"Unknown provider: {name}")
