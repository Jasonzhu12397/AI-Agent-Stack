"""Idempotent entitlement reclaim path for refunds.

Mirrors ``_credit_entitlement``: the order row is locked, a unique refund
reference binds to one reclaim row, and an ``IntegrityError`` on a lost race
resolves to the existing row. The budget limit moves only through this ledger
record; there is no direct budget write outside it.
"""

from django.db import IntegrityError, transaction
from django.db.models import Sum

from core.billing_models import EntitlementReclaim, Order
from core.models import TenantTokenBudget


@transaction.atomic
def reclaim_for_refund(
    *,
    order: Order,
    refund_reference: str,
    refund_cents: int,
    reason: str = "refund",
) -> EntitlementReclaim:
    """Create (or reuse) the reclaim row for one refund.

    A replay of the same refund reference — a repeated active refund or a
    replayed provider refund callback — returns the existing reclaim row and
    never deducts the budget a second time.
    """

    # Serialize re-entry on the order row: a replay and a concurrent reclaim
    # path both take this lock, so only one reclaim row can ever be inserted.
    order = Order.objects.select_for_update().get(pk=order.pk)
    existing = EntitlementReclaim.objects.filter(
        refund_reference=refund_reference
    ).first()
    if existing is not None:
        return existing

    grant = getattr(order, "grant", None)
    if grant is not None:
        already_tokens = (
            EntitlementReclaim.objects.filter(grant=grant).aggregate(
                total=Sum("tokens")
            )["total"]
            or 0
        )
        raw_tokens = (
            grant.tokens * refund_cents // order.amount_cents
            if order.amount_cents > 0
            else 0
        )
        tokens = min(raw_tokens, max(0, grant.tokens - already_tokens))
    else:
        tokens = 0

    budget, _ = TenantTokenBudget.objects.select_for_update().get_or_create(
        tenant=order.tenant
    )
    limit_before = int(budget.monthly_limit)
    limit_after = max(0, limit_before - tokens)

    reclaim = EntitlementReclaim(
        tenant=order.tenant,
        grant=grant,
        order=order,
        refund_reference=refund_reference,
        refund_cents=refund_cents,
        tokens=tokens,
        monthly_limit_before=limit_before,
        monthly_limit_after=limit_after,
        reason=reason[:160],
    )
    try:
        with transaction.atomic():
            reclaim.save()
    except IntegrityError:
        # Another writer won the race; its row is the authoritative reclaim.
        return EntitlementReclaim.objects.get(refund_reference=refund_reference)

    budget.monthly_limit = limit_after
    budget.save(update_fields=["monthly_limit", "updated_at"])
    return reclaim
