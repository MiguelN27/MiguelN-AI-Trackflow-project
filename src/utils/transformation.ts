type NumericSelector<T> = (item: T) => number;
type CategorySelector<T, K extends string> = (item: T) => K;

function isValidNumber(value: number): boolean {
  return Number.isFinite(value);
}

/**
 * 1) Count elements by category
 * Returns an object where keys are categories and values are counts.
 */
export function countByCategory<T, K extends string>(
  items: T[],
  selector: CategorySelector<T, K>
): Record<K, number> {
  const result = {} as Record<K, number>;

  for (const item of items) {
    const key = selector(item);
    result[key] = (result[key] ?? 0) + 1;
  }

  return result;
}

/**
 * 2) Calculate totals (sum)
 * Sums numeric values extracted from the collection.
 * Invalid numbers are ignored.
 */
export function calculateTotal<T>(items: T[], selector: NumericSelector<T>): number {
  let total = 0;

  for (const item of items) {
    const value = selector(item);
    if (isValidNumber(value)) total += value;
  }

  return total;
}

/**
 * 3) Calculate averages
 * Returns null when there are no valid numeric values.
 */
export function calculateAverage<T>(items: T[], selector: NumericSelector<T>): number | null {
  let total = 0;
  let count = 0;

  for (const item of items) {
    const value = selector(item);
    if (isValidNumber(value)) {
      total += value;
      count += 1;
    }
  }

  return count === 0 ? null : total / count;
}

/**
 * 4) Find maximum value
 * Returns null when there are no valid numeric values.
 */
export function findMaximum<T>(items: T[], selector: NumericSelector<T>): number | null {
  let maxValue: number | null = null;

  for (const item of items) {
    const value = selector(item);
    if (!isValidNumber(value)) continue;

    if (maxValue === null || value > maxValue) {
      maxValue = value;
    }
  }

  return maxValue;
}

/**
 * 5) Find minimum value
 * Returns null when there are no valid numeric values.
 */
export function findMinimum<T>(items: T[], selector: NumericSelector<T>): number | null {
  let minValue: number | null = null;

  for (const item of items) {
    const value = selector(item);
    if (!isValidNumber(value)) continue;

    if (minValue === null || value < minValue) {
      minValue = value;
    }
  }

  return minValue;
}

/**
 * Optional helper: build a full numeric report in one call.
 */
export interface NumericReport {
  total: number;
  average: number | null;
  maximum: number | null;
  minimum: number | null;
}

export function buildNumericReport<T>(items: T[], selector: NumericSelector<T>): NumericReport {
  return {
    total: calculateTotal(items, selector),
    average: calculateAverage(items, selector),
    maximum: findMaximum(items, selector),
    minimum: findMinimum(items, selector),
  };
}