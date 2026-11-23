"""Pro-rata refund of unearned premium.

Convention (documented in the README):
  * the cover runs from start_date to end_date, both days included;
  * the confirmed effective date is the first day the client is NOT covered;
  * unearned days = effective date .. end date, both included.

    refund = premium x unearned_days / total_days, rounded half-up to 2 decimals.

Insurers may apply short-period scales or fees; the figure here is the plain pro-rata
amount of unearned premium, which is what the brief asks for.
"""

from decimal import ROUND_HALF_UP, Decimal


def cover_days(start_date, end_date):
    return (end_date - start_date).days + 1


def unearned_days(start_date, end_date, effective_date):
    if effective_date <= start_date:
        return cover_days(start_date, end_date)
    if effective_date > end_date:
        return 0
    return (end_date - effective_date).days + 1


def pro_rata_refund(premium, start_date, end_date, effective_date):
    total = cover_days(start_date, end_date)
    if total <= 0:
        raise ValueError("Cover end date must not be before its start date.")
    days = unearned_days(start_date, end_date, effective_date)
    amount = Decimal(premium) * Decimal(days) / Decimal(total)
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def refund_breakdown(cover, effective_date):
    total = cover_days(cover.start_date, cover.end_date)
    days = unearned_days(cover.start_date, cover.end_date, effective_date)
    amount = pro_rata_refund(cover.premium, cover.start_date, cover.end_date, effective_date)
    return {"total_days": total, "unearned_days": days, "amount": amount}
