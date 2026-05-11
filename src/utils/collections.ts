import type {
  Country,
  Order,
  OrderStatus,
  Return,
  ReturnStatus,
  Carrier,
  ServiceType,
  Warehouse,
  WarehouseManagementSystem,
} from "../types/models";

// Generic criteria shape (single or multiple criteria at the same time)
export interface SearchCriteria<TCategory = string, TStatus = string> {
  category?: TCategory;
  minPrice?: number;
  maxPrice?: number;
  status?: TStatus;
  fromDate?: Date;
  toDate?: Date;
  location?: string | Country;
}

// Accessors let us map criteria to each entity without mutating models
export interface SearchAccessors<T, TCategory = string, TStatus = string> {
  getCategory?: (item: T) => TCategory | undefined;
  getPrice?: (item: T) => number | undefined;
  getStatus?: (item: T) => TStatus | undefined;
  getDate?: (item: T) => Date | undefined;
  getLocation?: (item: T) => string | Country | Array<string | Country> | undefined;
}

const normalize = (value: string) => value.trim().toLowerCase();
const isValidDate = (d: Date) => !Number.isNaN(d.getTime());

// 1) Search by Category
export function filterByCategory<T, TCategory>(
  items: T[],
  category: TCategory,
  getCategory: (item: T) => TCategory | undefined
): T[] {
  return items.filter((item) => getCategory(item) === category);
}

// 2) Search by Price Range
export function filterByPriceRange<T>(
  items: T[],
  getPrice: (item: T) => number | undefined,
  minPrice?: number,
  maxPrice?: number
): T[] {
  if (minPrice === undefined && maxPrice === undefined) return [...items];

  let min = minPrice;
  let max = maxPrice;

  // Normalize inverted range (e.g. min=100, max=10)
  if (min !== undefined && max !== undefined && min > max) {
    [min, max] = [max, min];
  }

  return items.filter((item) => {
    const price = getPrice(item);
    if (price === undefined || Number.isNaN(price)) return false;
    if (min !== undefined && price < min) return false;
    if (max !== undefined && price > max) return false;
    return true;
  });
}

// 3) Search by Status
export function filterByStatus<T, TStatus>(
  items: T[],
  status: TStatus,
  getStatus: (item: T) => TStatus | undefined
): T[] {
  return items.filter((item) => getStatus(item) === status);
}

// 4) Search by Date (range)
export function filterByDateRange<T>(
  items: T[],
  getDate: (item: T) => Date | undefined,
  fromDate?: Date,
  toDate?: Date
): T[] {
  if (!fromDate && !toDate) return [...items];

  const from = fromDate && isValidDate(fromDate) ? fromDate.getTime() : undefined;
  const to = toDate && isValidDate(toDate) ? toDate.getTime() : undefined;

  return items.filter((item) => {
    const d = getDate(item);
    if (!d || !isValidDate(d)) return false;

    const t = d.getTime();
    if (from !== undefined && t < from) return false;
    if (to !== undefined && t > to) return false;
    return true;
  });
}

// 5) Search by Location
export function filterByLocation<T>(
  items: T[],
  location: string | Country,
  getLocation: (item: T) => string | Country | Array<string | Country> | undefined
): T[] {
  const target = normalize(String(location));

  return items.filter((item) => {
    const value = getLocation(item);
    if (value === undefined) return false;

    if (Array.isArray(value)) {
      return value.some((v) => normalize(String(v)) === target);
    }

    return normalize(String(value)) === target;
  });
}

// Combined search (multiple criteria at the same time)
export function searchByCriteria<T, TCategory = string, TStatus = string>(
  items: T[],
  criteria: SearchCriteria<TCategory, TStatus>,
  accessors: SearchAccessors<T, TCategory, TStatus>
): T[] {
  let result = [...items];

  if (criteria.category !== undefined && accessors.getCategory) {
    result = filterByCategory(result, criteria.category, accessors.getCategory);
  }

  if (
    (criteria.minPrice !== undefined || criteria.maxPrice !== undefined) &&
    accessors.getPrice
  ) {
    result = filterByPriceRange(
      result,
      accessors.getPrice,
      criteria.minPrice,
      criteria.maxPrice
    );
  }

  if (criteria.status !== undefined && accessors.getStatus) {
    result = filterByStatus(result, criteria.status, accessors.getStatus);
  }

  if ((criteria.fromDate || criteria.toDate) && accessors.getDate) {
    result = filterByDateRange(result, accessors.getDate, criteria.fromDate, criteria.toDate);
  }

  if (criteria.location !== undefined && accessors.getLocation) {
    result = filterByLocation(result, criteria.location, accessors.getLocation);
  }

  return result;
}

/* ---------------------------
   Model-specific helpers
---------------------------- */

// Orders: status, date, location (destination)
export function searchOrders(
  orders: Order[],
  criteria: SearchCriteria<never, OrderStatus>,
  dateField: "placedAt" | "expectedDelivery" = "placedAt"
): Order[] {
  return searchByCriteria(orders, criteria, {
    getStatus: (o) => o.status,
    getDate: (o) => (dateField === "placedAt" ? o.placedAt : o.expectedDelivery),
    getLocation: (o) => o.destination,
  });
}

// Returns: status, date, location (client as location proxy if needed)
export function searchReturns(
  returnsList: Return[],
  criteria: SearchCriteria<never, ReturnStatus>,
  dateField: "requestedAt" | "expectedArrival" = "requestedAt"
): Return[] {
  return searchByCriteria(returnsList, criteria, {
    getStatus: (r) => r.status,
    getDate: (r) => (dateField === "requestedAt" ? r.requestedAt : r.expectedArrival),
    getLocation: (r) => r.client,
  });
}

// Carriers: category = service, location = supported countries
export function searchCarriers(
  carriers: Carrier[],
  criteria: SearchCriteria<ServiceType, never>
): Carrier[] {
  return searchByCriteria(carriers, criteria, {
    getCategory: (c) => c.service,
    getLocation: (c) => c.location,
  });
}

// Warehouses: category = management system, location = country or city string
export function searchWarehouses(
  warehouses: Warehouse[],
  criteria: SearchCriteria<WarehouseManagementSystem, never>
): Warehouse[] {
  return searchByCriteria(warehouses, criteria, {
    getCategory: (w) => w.managementSystem,
    getLocation: (w) => [w.country, w.location],
  });
}

/* 
  |                   |
  |   SORTING ARRAYS  |
  |                   |

*/

// Sorting direction
export type SortDirection = "asc" | "desc";

// Supported sortable values
type SortValue = string | number | Date | boolean | null | undefined;

// Multiple-fields sorting definition
export interface SortField<T> {
  selector: (item: T) => SortValue;
  direction?: SortDirection;
}

// Internal value comparator (numbers, dates, booleans, strings/alphabetical)
function compareValues(left: SortValue, right: SortValue): number {
  // Keep null/undefined at the end in ascending mode
  if (left == null && right == null) return 0;
  if (left == null) return 1;
  if (right == null) return -1;

  // Date comparison
  if (left instanceof Date && right instanceof Date) {
    return left.getTime() - right.getTime();
  }

  // Number comparison
  if (typeof left === "number" && typeof right === "number") {
    return left - right;
  }

  // Boolean comparison (false < true)
  if (typeof left === "boolean" && typeof right === "boolean") {
    return Number(left) - Number(right);
  }

  // String comparison (alphabetical, case-insensitive)
  return String(left).localeCompare(String(right), undefined, {
    sensitivity: "base",
    numeric: true,
  });
}

/**
 * Sort by a single field in ascending or descending order.
 * Does not mutate the original array.
 */
export function sortByField<T>(
  items: T[],
  selector: (item: T) => SortValue,
  direction: SortDirection = "asc"
): T[] {
  const sorted = [...items].sort((a, b) => compareValues(selector(a), selector(b)));
  return direction === "asc" ? sorted : sorted.reverse();
}

/**
 * Sort by multiple fields (priority order).
 * Example: first by status asc, then by date desc.
 * Does not mutate the original array.
 */
export function sortByMultipleFields<T>(items: T[], fields: SortField<T>[]): T[] {
  if (fields.length === 0) return [...items];

  return [...items].sort((a, b) => {
    for (const field of fields) {
      const direction = field.direction ?? "asc";
      const baseResult = compareValues(field.selector(a), field.selector(b));
      if (baseResult !== 0) {
        return direction === "asc" ? baseResult : -baseResult;
      }
    }
    return 0;
  });
}

/**
 * Sort alphabetically by a string field.
 * Does not mutate the original array.
 */
export function sortAlphabetically<T>(
  items: T[],
  selector: (item: T) => string | null | undefined,
  direction: SortDirection = "asc"
): T[] {
  return sortByField(items, selector, direction);
}
