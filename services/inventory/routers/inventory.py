from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlmodel import Session

from services.auth.dependencies import CurrentUser
from services.inventory import service
from services.inventory.database import get_db
from services.inventory.models import Warehouse
from services.inventory.schemas import (
    InboundOrderCreate,
    InboundOrderResponse,
    OrderResponse,
    OutboundOrderCreate,
    OutboundOrderResponse,
    ProductCreate,
    ProductResponse,
)

router = APIRouter(prefix="/inventory", tags=["inventory"])
InventorySession = Annotated[Session, Depends(get_db)]


@router.get("/products", response_model=list[ProductResponse])
def list_products(
    user: CurrentUser,
    session: InventorySession,
    warehouse: Warehouse | None = None,
    sku: str | None = None,
) -> list[ProductResponse]:
    return service.list_products(session, warehouse=warehouse, sku=sku)


@router.post("/products", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
def create_product(
    payload: ProductCreate,
    user: CurrentUser,
    session: InventorySession,
) -> ProductResponse:
    return service.create_product(session, payload)


@router.post(
    "/products/inbound", response_model=InboundOrderResponse, status_code=status.HTTP_201_CREATED
)
def create_inbound(
    payload: InboundOrderCreate,
    user: CurrentUser,
    session: InventorySession,
) -> InboundOrderResponse:
    return service.create_inbound(session, payload, UUID(user.id))


@router.post(
    "/products/outbound", response_model=OutboundOrderResponse, status_code=status.HTTP_201_CREATED
)
def create_outbound(
    payload: OutboundOrderCreate,
    user: CurrentUser,
    session: InventorySession,
) -> OutboundOrderResponse:
    return service.create_outbound(session, payload, UUID(user.id))


@router.get("/products/{id}", response_model=ProductResponse)
def get_product(id: UUID, user: CurrentUser, session: InventorySession) -> ProductResponse:
    return service.get_product(session, id)


@router.get("/orders", response_model=list[OrderResponse])
def list_orders(
    user: CurrentUser,
    session: InventorySession,
    warehouse: Warehouse | None = None,
    product_id: UUID | None = None,
) -> list[InboundOrderResponse | OutboundOrderResponse]:
    return service.list_orders(session, warehouse=warehouse, product_id=product_id)
