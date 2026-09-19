import { parseResponseJson, requestApi } from "@/lib/api-client";
import { normalizeSupplier, normalizeSuppliers } from "@/lib/supplier";
import type {
  Supplier,
  SupplierCreatePayload,
  SupplierFilters,
  SupplierStatus,
} from "@/types/supplier";

const JSON_HEADERS = { "Content-Type": "application/json" };

function buildSuppliersQuery(filters?: Partial<SupplierFilters>): string {
  const params = new URLSearchParams();

  if (filters?.country) {
    params.set("country", filters.country);
  }
  if (filters?.category) {
    params.set("category", filters.category);
  }

  const query = params.toString();
  return query ? `?${query}` : "";
}

export async function fetchSuppliers(filters?: Partial<SupplierFilters>): Promise<Supplier[]> {
  const response = await requestApi(`/suppliers${buildSuppliersQuery(filters)}`);

  return normalizeSuppliers(await parseResponseJson(response));
}

export async function fetchSupplierById(id: string): Promise<Supplier> {
  const response = await requestApi(`/suppliers/${encodeURIComponent(id)}`);

  return normalizeSupplier(await parseResponseJson(response));
}

export async function createSupplier(payload: SupplierCreatePayload): Promise<Supplier> {
  const response = await requestApi("/suppliers", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(payload),
  });

  return normalizeSupplier(await parseResponseJson(response));
}

export async function updateSupplierRate(id: number, ratePerShipment: number): Promise<Supplier> {
  const response = await requestApi(`/suppliers/${id}/rate`, {
    method: "PATCH",
    headers: JSON_HEADERS,
    body: JSON.stringify({ rate_per_shipment: ratePerShipment }),
  });

  return normalizeSupplier(await parseResponseJson(response));
}

export async function updateSupplierStatus(id: number, status: SupplierStatus): Promise<Supplier> {
  const response = await requestApi(`/suppliers/${id}/status`, {
    method: "PATCH",
    headers: JSON_HEADERS,
    body: JSON.stringify({ status }),
  });

  return normalizeSupplier(await parseResponseJson(response));
}
