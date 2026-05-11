import type {
  Country,
  ServiceType,
  WarehouseManagementSystem,
  OrderStatus,
  ReturnStatus,
  ProductCondition,
  ProductItem,
  Department,
  Warehouse,
  Worker,
  Carrier,
  Order,
  Return,
} from "../types/models";

/* -----------------------------
   Validation result structures
------------------------------ */

export interface ValidationError {
  field: string;
  message: string;
}

export interface ValidationResult {
  isValid: boolean;
  errors: ValidationError[];
}

/* -----------------------------
   Runtime guards / constants
------------------------------ */

const VALID_COUNTRIES: Country[] = ["Mexico", "Spain"];
const VALID_SERVICES: ServiceType[] = ["local", "international"];
const VALID_WMS: WarehouseManagementSystem[] = ["commercial software", "advanced spreadsheet"];
const VALID_ORDER_STATUS: OrderStatus[] = ["In Process", "Shipped", "Delivered", "Returned"];
const VALID_RETURN_STATUS: ReturnStatus[] = [
  "Under review",
  "Approved",
  "Rejected",
  "In transit",
  "Refunded",
  "Disposed",
];
const VALID_PRODUCT_CONDITION: ProductCondition[] = ["Poor", "Fair", "Excellent", "Like New"];

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isValidDate(value: unknown): value is Date {
  return value instanceof Date && !Number.isNaN(value.getTime());
}

function inRange(value: number, min?: number, max?: number): boolean {
  if (!Number.isFinite(value)) return false;
  if (min !== undefined && value < min) return false;
  if (max !== undefined && value > max) return false;
  return true;
}

function createResult(errors: ValidationError[]): ValidationResult {
  return { isValid: errors.length === 0, errors };
}

/* ----------------------------------------
   Generic business validation primitives
----------------------------------------- */

/** 1) Required fields validation */
export function validateRequiredFields(
  source: Record<string, unknown>,
  requiredFields: string[]
): ValidationResult {
  const errors: ValidationError[] = [];

  for (const field of requiredFields) {
    const value = source[field];

    if (value === undefined || value === null) {
      errors.push({ field, message: "Field is required." });
      continue;
    }

    if (typeof value === "string" && value.trim().length === 0) {
      errors.push({ field, message: "Field cannot be empty." });
    }
  }

  return createResult(errors);
}

/** 2) Numeric range validation */
export function validateNumericRange(
  field: string,
  value: number,
  min?: number,
  max?: number
): ValidationResult {
  const errors: ValidationError[] = [];

  if (!inRange(value, min, max)) {
    const minText = min !== undefined ? `${min}` : "-∞";
    const maxText = max !== undefined ? `${max}` : "+∞";
    errors.push({
      field,
      message: `Field must be a finite number in range [${minText}, ${maxText}].`,
    });
  }

  return createResult(errors);
}

/** 3) Date coherence validation */
export function validateDateCoherence(
  startField: string,
  startDate: Date,
  endField: string,
  endDate: Date
): ValidationResult {
  const errors: ValidationError[] = [];

  if (!isValidDate(startDate)) {
    errors.push({ field: startField, message: "Invalid date." });
  }

  if (!isValidDate(endDate)) {
    errors.push({ field: endField, message: "Invalid date." });
  }

  if (isValidDate(startDate) && isValidDate(endDate) && endDate.getTime() < startDate.getTime()) {
    errors.push({
      field: `${startField}-${endField}`,
      message: `${endField} must be greater than or equal to ${startField}.`,
    });
  }

  return createResult(errors);
}

/* -----------------------------
   Shared model helpers
------------------------------ */

function validateProductItems(items: ProductItem[]): ValidationError[] {
  const errors: ValidationError[] = [];

  if (!Array.isArray(items) || items.length === 0) {
    errors.push({ field: "products", message: "At least one product item is required." });
    return errors;
  }

  items.forEach((item, index) => {
    if (!isNonEmptyString(item.product)) {
      errors.push({ field: `products[${index}].product`, message: "Product name is required." });
    }

    if (!Number.isFinite(item.quantity) || item.quantity <= 0) {
      errors.push({
        field: `products[${index}].quantity`,
        message: "Quantity must be greater than 0.",
      });
    }
  });

  return errors;
}

/* -----------------------------
   Model-specific validations
------------------------------ */

export function validateDepartment(department: Department): ValidationResult {
  const errors: ValidationError[] = [];

  if (!isNonEmptyString(department.name)) {
    errors.push({ field: "name", message: "Name is required." });
  }

  if (!isNonEmptyString(department.departmentID)) {
    errors.push({ field: "departmentID", message: "Department ID is required." });
  }

  if (!Array.isArray(department.location) || department.location.length === 0) {
    errors.push({ field: "location", message: "At least one country is required." });
  } else {
    department.location.forEach((country, index) => {
      if (!VALID_COUNTRIES.includes(country)) {
        errors.push({ field: `location[${index}]`, message: "Invalid country." });
      }
    });
  }

  if (!Array.isArray(department.needs)) {
    errors.push({ field: "needs", message: "Needs must be an array." });
  }

  if (!isNonEmptyString(department.managerID)) {
    errors.push({ field: "managerID", message: "Manager ID is required." });
  }

  return createResult(errors);
}

export function validateWarehouse(warehouse: Warehouse): ValidationResult {
  const errors: ValidationError[] = [];

  if (!isNonEmptyString(warehouse.warehouseID)) {
    errors.push({ field: "warehouseID", message: "Warehouse ID is required." });
  }

  if (!isNonEmptyString(warehouse.location)) {
    errors.push({ field: "location", message: "Location is required." });
  }

  if (!VALID_COUNTRIES.includes(warehouse.country)) {
    errors.push({ field: "country", message: "Invalid country." });
  }

  if (!VALID_WMS.includes(warehouse.managementSystem)) {
    errors.push({ field: "managementSystem", message: "Invalid management system." });
  }

  return createResult(errors);
}

export function validateWorker(worker: Worker): ValidationResult {
  const errors: ValidationError[] = [];

  if (!isNonEmptyString(worker.name)) {
    errors.push({ field: "name", message: "Name is required." });
  }

  if (!isNonEmptyString(worker.workerID)) {
    errors.push({ field: "workerID", message: "Worker ID is required." });
  }

  if (!VALID_COUNTRIES.includes(worker.location)) {
    errors.push({ field: "location", message: "Invalid country." });
  }

  if (!isNonEmptyString(worker.departmentID)) {
    errors.push({ field: "departmentID", message: "Department ID is required." });
  }

  if (!isNonEmptyString(worker.position)) {
    errors.push({ field: "position", message: "Position is required." });
  }

  return createResult(errors);
}

export function validateCarrier(carrier: Carrier): ValidationResult {
  const errors: ValidationError[] = [];

  if (!isNonEmptyString(carrier.name)) {
    errors.push({ field: "name", message: "Name is required." });
  }

  if (!isNonEmptyString(carrier.carrierID)) {
    errors.push({ field: "carrierID", message: "Carrier ID is required." });
  }

  if (!Array.isArray(carrier.location) || carrier.location.length === 0) {
    errors.push({ field: "location", message: "At least one country is required." });
  } else {
    carrier.location.forEach((country, index) => {
      if (!VALID_COUNTRIES.includes(country)) {
        errors.push({ field: `location[${index}]`, message: "Invalid country." });
      }
    });
  }

  if (!VALID_SERVICES.includes(carrier.service)) {
    errors.push({ field: "service", message: "Invalid service type." });
  }

  return createResult(errors);
}

export function validateOrder(order: Order): ValidationResult {
  const errors: ValidationError[] = [];

  // Required fields
  if (!isNonEmptyString(order.orderID)) errors.push({ field: "orderID", message: "Order ID is required." });
  if (!isNonEmptyString(order.packageSize)) errors.push({ field: "packageSize", message: "Package size is required." });
  if (!isNonEmptyString(order.destination)) errors.push({ field: "destination", message: "Destination is required." });
  if (!isNonEmptyString(order.carrierID)) errors.push({ field: "carrierID", message: "Carrier ID is required." });
  if (!isNonEmptyString(order.warehouseID)) errors.push({ field: "warehouseID", message: "Warehouse ID is required." });
  if (!isNonEmptyString(order.client)) errors.push({ field: "client", message: "Client is required." });

  // Numeric ranges
  if (!inRange(order.packageQuantity, 1)) {
    errors.push({ field: "packageQuantity", message: "Package quantity must be >= 1." });
  }
  if (!inRange(order.weight, 0.000001)) {
    errors.push({ field: "weight", message: "Weight must be > 0." });
  }

  // Date coherence
  const dateCheck = validateDateCoherence(
    "placedAt",
    order.placedAt,
    "expectedDelivery",
    order.expectedDelivery
  );
  errors.push(...dateCheck.errors);

  // Enum checks
  if (!VALID_ORDER_STATUS.includes(order.status)) {
    errors.push({ field: "status", message: "Invalid order status." });
  }

  // Nested products
  errors.push(...validateProductItems(order.products));

  return createResult(errors);
}

export function validateReturn(returnItem: Return): ValidationResult {
  const errors: ValidationError[] = [];

  // Required fields
  if (!isNonEmptyString(returnItem.orderID)) errors.push({ field: "orderID", message: "Order ID is required." });
  if (!isNonEmptyString(returnItem.returnID)) errors.push({ field: "returnID", message: "Return ID is required." });
  if (!isNonEmptyString(returnItem.returningReason)) errors.push({ field: "returningReason", message: "Returning reason is required." });
  if (!isNonEmptyString(returnItem.packageSize)) errors.push({ field: "packageSize", message: "Package size is required." });
  if (!isNonEmptyString(returnItem.carrierID)) errors.push({ field: "carrierID", message: "Carrier ID is required." });
  if (!isNonEmptyString(returnItem.warehouseID)) errors.push({ field: "warehouseID", message: "Warehouse ID is required." });
  if (!isNonEmptyString(returnItem.client)) errors.push({ field: "client", message: "Client is required." });

  // Numeric ranges
  if (!inRange(returnItem.packageQuantity, 1)) {
    errors.push({ field: "packageQuantity", message: "Package quantity must be >= 1." });
  }
  if (!inRange(returnItem.weight, 0.000001)) {
    errors.push({ field: "weight", message: "Weight must be > 0." });
  }

  // Date coherence
  const dateCheck = validateDateCoherence(
    "requestedAt",
    returnItem.requestedAt,
    "expectedArrival",
    returnItem.expectedArrival
  );
  errors.push(...dateCheck.errors);

  // Enum checks
  if (!VALID_PRODUCT_CONDITION.includes(returnItem.productCondition)) {
    errors.push({ field: "productCondition", message: "Invalid product condition." });
  }

  if (!VALID_RETURN_STATUS.includes(returnItem.status)) {
    errors.push({ field: "status", message: "Invalid return status." });
  }

  // Nested products
  errors.push(...validateProductItems(returnItem.products));

  return createResult(errors);
}