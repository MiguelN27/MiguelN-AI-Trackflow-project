import type { Order, Return, Worker, Carrier, Warehouse, Department } from "../types/models";

/**
 * 1) Linear search (for unsorted arrays)
 * Returns the first matching element or null if not found.
 */
export function linearSearch<T>(items: T[], predicate: (item: T) => boolean): T | null {
  for (const item of items) {
    if (predicate(item)) return item;
  }
  return null;
}

/**
 * Linear search variant that returns the index.
 * Returns -1 if not found.
 */
export function linearSearchIndex<T>(items: T[], predicate: (item: T) => boolean): number {
  for (let index = 0; index < items.length; index += 1) {
    if (predicate(items[index])) return index;
  }
  return -1;
}

type CompareFn<T> = (left: T, right: T) => number;

/**
 * Default comparator for primitive sortable values.
 */
function defaultCompare<T>(left: T, right: T): number {
  if (left === right) return 0;
  return left < right ? -1 : 1;
}

/**
 * 2) Binary search (for previously sorted arrays)
 * Returns the index of target or -1 if not found.
 */
export function binarySearch<T>(
  sortedItems: T[],
  target: T,
  compare: CompareFn<T> = defaultCompare
): number {
  let low = 0;
  let high = sortedItems.length - 1;

  while (low <= high) {
    const mid = Math.floor((low + high) / 2);
    const comparison = compare(sortedItems[mid], target);

    if (comparison === 0) return mid;
    if (comparison < 0) low = mid + 1;
    else high = mid - 1;
  }

  return -1;
}

/**
 * Binary search by selector/key in a sorted array.
 * Array must be sorted ASC by the same selector.
 */
export function binarySearchBy<T, K>(
  sortedItems: T[],
  target: K,
  selector: (item: T) => K,
  compare: CompareFn<K> = defaultCompare
): number {
  let low = 0;
  let high = sortedItems.length - 1;

  while (low <= high) {
    const mid = Math.floor((low + high) / 2);
    const value = selector(sortedItems[mid]);
    const comparison = compare(value, target);

    if (comparison === 0) return mid;
    if (comparison < 0) low = mid + 1;
    else high = mid - 1;
  }

  return -1;
}

/* ---------------------------------------
   Model-specific linear search helpers
---------------------------------------- */

/** Search Order by orderID (case-insensitive) in unsorted arrays */
export function findOrderById(orders: Order[], orderID: string): Order | null {
  const target = orderID.trim().toLowerCase();
  return linearSearch(orders, (order) => order.orderID.trim().toLowerCase() === target);
}

/** Search Return by returnID (case-insensitive) in unsorted arrays */
export function findReturnById(returnsList: Return[], returnID: string): Return | null {
  const target = returnID.trim().toLowerCase();
  return linearSearch(
    returnsList,
    (returnItem) => returnItem.returnID.trim().toLowerCase() === target
  );
}

/** Search Worker by workerID (case-insensitive) in unsorted arrays */
export function findWorkerById(workers: Worker[], workerID: string): Worker | null {
  const target = workerID.trim().toLowerCase();
  return linearSearch(workers, (worker) => worker.workerID.trim().toLowerCase() === target);
}

/** Search Carrier by carrierID (case-insensitive) in unsorted arrays */
export function findCarrierById(carriers: Carrier[], carrierID: string): Carrier | null {
  const target = carrierID.trim().toLowerCase();
  return linearSearch(carriers, (carrier) => carrier.carrierID.trim().toLowerCase() === target);
}

/** Search Warehouse by warehouseID (case-insensitive) in unsorted arrays */
export function findWarehouseById(warehouses: Warehouse[], warehouseID: string): Warehouse | null {
  const target = warehouseID.trim().toLowerCase();
  return linearSearch(
    warehouses,
    (warehouse) => warehouse.warehouseID.trim().toLowerCase() === target
  );
}

/** Search Department by departmentID (case-insensitive) in unsorted arrays */
export function findDepartmentById(
  departments: Department[],
  departmentID: string
): Department | null {
  const target = departmentID.trim().toLowerCase();
  return linearSearch(
    departments,
    (department) => department.departmentID.trim().toLowerCase() === target
  );
}