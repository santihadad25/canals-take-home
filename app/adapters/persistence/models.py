"""The tables as the queries see them: their columns and types.

The schema itself (constraints, indexes, ...) is created by db/init/01-schema.sql. These classes
only describe the columns that the queries use.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Float, Identity, Integer, String, Text, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ProductRow(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    unit_price_cents: Mapped[int] = mapped_column(BigInteger)


class WarehouseRow(Base):
    __tablename__ = "warehouses"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)


class StockRow(Base):
    __tablename__ = "stock"

    warehouse_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer)


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    customer_id: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[UUID] = mapped_column(Uuid)
    request_hash: Mapped[str] = mapped_column(String(64))
    shipping_address: Mapped[str] = mapped_column(Text)
    shipping_latitude: Mapped[float] = mapped_column(Float)
    shipping_longitude: Mapped[float] = mapped_column(Float)
    warehouse_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32))
    total_cents: Mapped[int] = mapped_column(BigInteger)
    payment_id: Mapped[str | None] = mapped_column(Text)
    failure_code: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The lease a reconciler instance takes while it works on the order.
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrderItemRow(Base):
    __tablename__ = "order_items"

    order_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    product_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_cents: Mapped[int] = mapped_column(BigInteger)


class OrderEventRow(Base):
    """Append-only history of status changes, written in the same transaction as the change."""

    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    order_id: Mapped[UUID] = mapped_column(Uuid)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
