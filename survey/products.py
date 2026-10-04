"""
Product / brand registry.

One backend serves two separately-sold, separately-branded products:
  - Cleanpulse — restroom / instant feedback (cleanpulse.app)
  - TapRate    — full customer feedback (taprate.app)

Each dashboard request says which product it is for via the X-Product header.
Requests without it are treated as TapRate (the original frontend sends none).
Public survey endpoints don't use the header — they derive the product from
the tag or location being surveyed.
"""
import os
from dataclasses import dataclass

from rest_framework.exceptions import ValidationError

CLEANPULSE = 'cleanpulse'
TAPRATE    = 'taprate'

DEFAULT_PRODUCT = TAPRATE

PRODUCT_CHOICES = [
    (CLEANPULSE, 'Cleanpulse'),
    (TAPRATE,    'TapRate'),
]
PRODUCTS = {value for value, _ in PRODUCT_CHOICES}


@dataclass(frozen=True)
class Brand:
    product:               str
    name:                  str
    frontend_url:          str
    alerts_from_email:     str   # sender for transactional / alert emails
    hello_email:           str   # sender + reply-to for prospect-facing email
    email_enabled:         bool  # False until the sending domain is verified in Resend
    alerts_dashboard_path: str   # deep link used in alert emails


def _env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ('1', 'true', 'yes')


def get_brand(product):
    """Brand config for a product. Read from env on each call so tests/env changes apply."""
    if product == CLEANPULSE:
        return Brand(
            product=CLEANPULSE,
            name='Cleanpulse',
            frontend_url=os.environ.get('CLEANPULSE_FRONTEND_URL', 'https://cleanpulse.app'),
            alerts_from_email=os.environ.get('CLEANPULSE_ALERTS_FROM_EMAIL', 'alerts@cleanpulse.app'),
            hello_email=os.environ.get('CLEANPULSE_HELLO_EMAIL', 'hello@cleanpulse.app'),
            # Off until cleanpulse.app is verified in Resend — then set CLEANPULSE_EMAIL_ENABLED=true.
            email_enabled=_env_bool('CLEANPULSE_EMAIL_ENABLED', False),
            alerts_dashboard_path='/dashboard',
        )
    return Brand(
        product=TAPRATE,
        name='TapRate',
        frontend_url=os.environ.get('FRONTEND_URL', 'https://taprate.app'),
        alerts_from_email=os.environ.get('ALERTS_FROM_EMAIL', 'alerts@taprate.app'),
        hello_email=os.environ.get('TAPRATE_HELLO_EMAIL', 'hello@taprate.app'),
        email_enabled=_env_bool('TAPRATE_EMAIL_ENABLED', True),
        alerts_dashboard_path='/dashboard/insights',
    )


def get_product(request):
    """Product a dashboard request is for, from the X-Product header (default TapRate)."""
    value = (request.META.get('HTTP_X_PRODUCT') or DEFAULT_PRODUCT).strip().lower()
    if value not in PRODUCTS:
        raise ValidationError({'detail': f'Unknown product "{value}".'})
    return value
