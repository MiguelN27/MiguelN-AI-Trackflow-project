from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from tinydb.table import Document

from services.suppliers.db import get_table
from services.suppliers.models import (
    Category,
    Country,
    RateUpdate,
    StatusUpdate,
    SupplierCreate,
    SupplierInDB,
    SupplierResponse,
)

app = FastAPI(
    title="TrackFlow Supplier Directory",
    description="Centralized supplier registry for TrackFlow USA and Spain operations.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:5500",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def to_response(doc: Document) -> SupplierResponse:
    return SupplierResponse(id=doc.doc_id, **doc)


def get_document_or_404(supplier_id: int) -> Document:
    doc = get_table().get(doc_id=supplier_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Supplier {supplier_id} not found",
        )
    return doc


@app.post("/suppliers", response_model=SupplierResponse, status_code=status.HTTP_201_CREATED)
def create_supplier(payload: SupplierCreate) -> SupplierResponse:
    record = SupplierInDB(**payload.model_dump(), updated_at=datetime.now(timezone.utc))
    doc_id = get_table().insert(record.model_dump(mode="json"))
    return SupplierResponse(id=doc_id, **record.model_dump())


@app.get("/suppliers", response_model=list[SupplierResponse])
def list_suppliers(
    country: Country | None = Query(default=None),
    category: Category | None = Query(default=None),
) -> list[SupplierResponse]:
    docs = get_table().all()
    if country is not None:
        docs = [doc for doc in docs if doc["country"] == country.value]
    if category is not None:
        docs = [doc for doc in docs if category.value in doc["categories"]]
    return [to_response(doc) for doc in docs]


@app.get("/suppliers/{supplier_id}", response_model=SupplierResponse)
def get_supplier(supplier_id: int) -> SupplierResponse:
    return to_response(get_document_or_404(supplier_id))


@app.patch("/suppliers/{supplier_id}/rate", response_model=SupplierResponse)
def update_rate(supplier_id: int, payload: RateUpdate) -> SupplierResponse:
    get_document_or_404(supplier_id)
    get_table().update(
        {
            "rate_per_shipment": payload.rate_per_shipment,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        doc_ids=[supplier_id],
    )
    return to_response(get_document_or_404(supplier_id))


@app.patch("/suppliers/{supplier_id}/status", response_model=SupplierResponse)
def update_status(supplier_id: int, payload: StatusUpdate) -> SupplierResponse:
    get_document_or_404(supplier_id)
    get_table().update({"status": payload.status.value}, doc_ids=[supplier_id])
    return to_response(get_document_or_404(supplier_id))


@app.delete("/suppliers/{supplier_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_supplier(supplier_id: int) -> Response:
    get_document_or_404(supplier_id)
    get_table().remove(doc_ids=[supplier_id])
    return Response(status_code=status.HTTP_204_NO_CONTENT)
