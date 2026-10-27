from decimal import Decimal

from django import template

register = template.Library()

_SYMBOLS = {'USD': '$', 'EUR': '€', 'GBP': '£', 'NGN': '₦', 'CAD': 'CA$', 'AUD': 'A$'}


@register.filter
def money(value, currency='USD'):
    """Format a Decimal as e.g. $12,345.60 (or 'CHF 1,000.00' for unmapped codes)."""
    if value is None or value == '':
        return ''
    value = Decimal(value)
    sign = '-' if value < 0 else ''
    body = f'{abs(value):,.2f}'
    symbol = _SYMBOLS.get(currency)
    return f'{sign}{symbol}{body}' if symbol else f'{sign}{currency} {body}'


@register.filter
def signed_pct(value):
    value = Decimal(value)
    return f'{"+" if value > 0 else ""}{value:.2f}%'


@register.filter
def trend(value):
    """CSS class for a gain/loss figure."""
    return 'up' if Decimal(value) > 0 else ('down' if Decimal(value) < 0 else 'flat')
