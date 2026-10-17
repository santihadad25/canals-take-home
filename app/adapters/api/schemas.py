from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.domain.models import Order, OrderStatus


class OrderItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=1_000_000)


class PaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # SecretStr keeps the number out of reprs, logs and tracebacks. The payment provider is the
    # one that validates it (format, funds, ...): we only check that it is there.
    card_number: SecretStr = Field(min_length=1, max_length=32)


class CreateOrderRequest(BaseModel):
    # extra="forbid": the amount is computed by the server, so a client that sends one is wrong.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    customer_id: str = Field(min_length=1, max_length=64)
    shipping_address: str = Field(min_length=1, max_length=300)
    items: list[OrderItemRequest] = Field(min_length=1, max_length=100)
    payment: PaymentRequest

    @field_validator("items")
    @classmethod
    def each_product_appears_once(cls, items: list[OrderItemRequest]) -> list[OrderItemRequest]:
        product_ids = [item.product_id for item in items]
        if len(set(product_ids)) != len(product_ids):
            raise ValueError("a product can only appear once per order")
        return items


class OrderItemResponse(BaseModel):
    product_id: int
    quantity: int
    unit_price_cents: int


class OrderResponse(BaseModel):
    id: UUID
    status: OrderStatus
    customer_id: str
    shipping_address: str
    warehouse_id: int
    items: list[OrderItemResponse]
    total_cents: int
    payment_id: str | None
    created_at: datetime
    expires_at: datetime  # the deadline for the payment to get authorized

    @classmethod
    def from_order(cls, order: Order) -> "OrderResponse":
        return cls(
            id=order.id,
            status=order.status,
            customer_id=order.customer_id,
            shipping_address=order.shipping_address,
            warehouse_id=order.warehouse_id,
            items=[
                OrderItemResponse(
                    product_id=item.product_id,
                    quantity=item.quantity,
                    unit_price_cents=item.unit_price.cents,
                )
                for item in order.items
            ],
            total_cents=order.total.cents,
            payment_id=order.payment_id,
            created_at=order.created_at,
            expires_at=order.expires_at,
        )
