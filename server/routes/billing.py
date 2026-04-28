from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from config import SITE_URL
from server.auth import AuthError, get_bearer_token, get_user_from_token
from server.stripe import get_stripe_client, price_id

router = APIRouter(prefix="/billing", tags=["billing"])
ACTIVE_SUBSCRIPTION_STATUSES = {"active", "trialing"}


class CheckoutSessionRequest(BaseModel):
    success_url: str | None = None
    cancel_url: str | None = None


class CheckoutSessionResponse(BaseModel):
    id: str
    url: str


class BillingUserResponse(BaseModel):
    user_id: str
    email: str | None = None
    has_active_subscription: bool
    subscription_status: str


def _get_current_user(authorization: str | None) -> tuple[str, str | None]:
    try:
        user = get_user_from_token(get_bearer_token(authorization))
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    return user.id, user.email


def _stripe_search_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _get_user_subscription_status(user_id: str) -> str:
    subscriptions = get_stripe_client().Subscription.search(
        query=f"metadata['supabase_user_id']:'{_stripe_search_value(user_id)}'",
        limit=10,
    )

    for subscription in subscriptions.auto_paging_iter():
        status = getattr(subscription, "status", None)

        if status in ACTIVE_SUBSCRIPTION_STATUSES:
            return "active"

    return "inactive"


@router.get("/user", response_model=BillingUserResponse)
async def get_billing_user(
    authorization: str | None = Header(default=None),
) -> BillingUserResponse:
    user_id, email = _get_current_user(authorization)
    subscription_status = _get_user_subscription_status(user_id)

    return BillingUserResponse(
        user_id=user_id,
        email=email,
        has_active_subscription=subscription_status == "active",
        subscription_status=subscription_status,
    )


@router.post("/checkout-session", response_model=CheckoutSessionResponse)
async def create_checkout_session(
    body: CheckoutSessionRequest,
    authorization: str | None = Header(default=None),
) -> CheckoutSessionResponse:
    user_id, email = _get_current_user(authorization)
    subscription_status = _get_user_subscription_status(user_id)

    if subscription_status == "active":
        raise HTTPException(status_code=409, detail="User already has an active subscription.")

    success_url = body.success_url or (
        f"{SITE_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}"
    )
    cancel_url = body.cancel_url or f"{SITE_URL}/billing/cancel"

    session = get_stripe_client().checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=user_id,
        customer_email=email,
        metadata={"supabase_user_id": user_id},
        subscription_data={"metadata": {"supabase_user_id": user_id}},
    )

    return CheckoutSessionResponse(id=session.id, url=session.url)
