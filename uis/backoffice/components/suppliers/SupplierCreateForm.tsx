"use client";

import { StateMessage } from "@/components/common/StateMessage";
import {
  buildSupplierPayload,
  currencyForCountry,
  emptySupplierFormValues,
  formatCategoryLabel,
  formatStatusLabel,
  isSupplierCountry,
  isSupplierStatus,
  validateSupplierForm,
} from "@/lib/supplier";
import { createSupplier } from "@/services/suppliers-service";
import {
  SUPPLIER_CATEGORIES,
  SUPPLIER_COUNTRIES,
  SUPPLIER_STATUSES,
  type Supplier,
  type SupplierCategory,
  type SupplierFormValues,
} from "@/types/supplier";
import { FormEvent, useState } from "react";

type SupplierCreateFormProps = {
  onCreated: (supplier: Supplier) => void;
  onCancel: () => void;
};

const fieldClassName =
  "w-full rounded-xl border border-[color:var(--border-soft)] bg-[color:var(--surface)] px-3 py-2 text-sm text-[color:var(--foreground)] outline-none transition focus:border-[color:var(--brand-primary)]";

export function SupplierCreateForm({ onCreated, onCancel }: SupplierCreateFormProps) {
  const [values, setValues] = useState<SupplierFormValues>(emptySupplierFormValues());
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const derivedCurrency = currencyForCountry(values.country);

  function updateValues(patch: Partial<SupplierFormValues>): void {
    setError(null);
    setSuccess(null);
    setValues((currentValues) => ({ ...currentValues, ...patch }));
  }

  function toggleCategory(category: SupplierCategory): void {
    setError(null);
    setSuccess(null);
    setValues((currentValues) => ({
      ...currentValues,
      categories: currentValues.categories.includes(category)
        ? currentValues.categories.filter((item) => item !== category)
        : [...currentValues.categories, category],
    }));
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    const validationError = validateSupplierForm(values);
    if (validationError) {
      setError(validationError);
      setSuccess(null);
      return;
    }

    setIsSubmitting(true);
    setError(null);
    setSuccess(null);

    try {
      const created = await createSupplier(buildSupplierPayload(values));
      setValues(emptySupplierFormValues());
      setSuccess(`${created.name} was added to the directory.`);
      onCreated(created);
    } catch (createError) {
      setError(createError instanceof Error ? createError.message : "Unable to create the supplier");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <form className="grid gap-4 sm:grid-cols-2" onSubmit={handleSubmit} noValidate>
      <label className="block text-sm text-slate-600 sm:col-span-2">
        <span className="mb-1 block font-medium text-[color:var(--foreground)]">Name</span>
        <input
          value={values.name}
          onChange={(event) => updateValues({ name: event.target.value })}
          placeholder="Supplier legal or trading name"
          className={fieldClassName}
        />
      </label>

      <label className="block text-sm text-slate-600">
        <span className="mb-1 block font-medium text-[color:var(--foreground)]">Country</span>
        <select
          value={values.country}
          onChange={(event) => {
            const nextCountry = event.target.value;
            if (isSupplierCountry(nextCountry)) {
              updateValues({ country: nextCountry });
            }
          }}
          className={fieldClassName}
        >
          {SUPPLIER_COUNTRIES.map((country) => (
            <option key={country} value={country}>
              {country}
            </option>
          ))}
        </select>
      </label>

      <label className="block text-sm text-slate-600">
        <span className="mb-1 block font-medium text-[color:var(--foreground)]">Currency</span>
        <input value={derivedCurrency} readOnly aria-readonly="true" className={`${fieldClassName} bg-slate-50 text-slate-500`} />
        <span className="mt-1 block text-xs text-slate-500">Derived from the country and enforced by the API.</span>
      </label>

      <label className="block text-sm text-slate-600">
        <span className="mb-1 block font-medium text-[color:var(--foreground)]">Rate per shipment</span>
        <input
          type="number"
          inputMode="decimal"
          min="0.01"
          step="0.01"
          value={values.ratePerShipment}
          onChange={(event) => updateValues({ ratePerShipment: event.target.value })}
          placeholder="0.00"
          className={fieldClassName}
        />
      </label>

      <label className="block text-sm text-slate-600">
        <span className="mb-1 block font-medium text-[color:var(--foreground)]">Status</span>
        <select
          value={values.status}
          onChange={(event) => {
            const nextStatus = event.target.value;
            if (isSupplierStatus(nextStatus)) {
              updateValues({ status: nextStatus });
            }
          }}
          className={fieldClassName}
        >
          {SUPPLIER_STATUSES.map((status) => (
            <option key={status} value={status}>
              {formatStatusLabel(status)}
            </option>
          ))}
        </select>
      </label>

      <fieldset className="sm:col-span-2">
        <legend className="mb-2 text-sm font-medium text-[color:var(--foreground)]">Categories</legend>
        <div className="grid gap-2 sm:grid-cols-2">
          {SUPPLIER_CATEGORIES.map((category) => (
            <label key={category} className="flex items-center gap-2 text-sm text-slate-600">
              <input
                type="checkbox"
                checked={values.categories.includes(category)}
                onChange={() => toggleCategory(category)}
                className="h-4 w-4 rounded border-[color:var(--border-soft)] accent-[color:var(--brand-primary)]"
              />
              {formatCategoryLabel(category)}
            </label>
          ))}
        </div>
      </fieldset>

      <label className="block text-sm text-slate-600">
        <span className="mb-1 block font-medium text-[color:var(--foreground)]">Service zone</span>
        <input
          value={values.serviceZone}
          onChange={(event) => updateValues({ serviceZone: event.target.value })}
          placeholder="Optional, e.g. West Coast"
          className={fieldClassName}
        />
      </label>

      <label className="block text-sm text-slate-600">
        <span className="mb-1 block font-medium text-[color:var(--foreground)]">Contact email</span>
        <input
          type="email"
          value={values.contactEmail}
          onChange={(event) => updateValues({ contactEmail: event.target.value })}
          placeholder="Optional, e.g. business@supplier.com"
          className={fieldClassName}
        />
      </label>

      <label className="block text-sm text-slate-600 sm:col-span-2">
        <span className="mb-1 block font-medium text-[color:var(--foreground)]">Notes</span>
        <textarea
          value={values.notes}
          onChange={(event) => updateValues({ notes: event.target.value })}
          rows={3}
          placeholder="Optional context for the operations team"
          className={fieldClassName}
        />
      </label>

      {error ? (
        <StateMessage tone="error" className="sm:col-span-2">
          {error}
        </StateMessage>
      ) : null}

      {success ? (
        <StateMessage tone="success" className="sm:col-span-2">
          {success}
        </StateMessage>
      ) : null}

      <div className="flex items-center justify-end gap-3 sm:col-span-2">
        <button
          type="button"
          onClick={onCancel}
          className="rounded-xl border border-[color:var(--border-soft)] px-4 py-2 text-sm font-semibold text-slate-600 transition hover:bg-[color:var(--brand-primary)]/5"
        >
          Close
        </button>
        <button
          type="submit"
          disabled={isSubmitting}
          className="rounded-xl bg-[color:var(--brand-primary)] px-4 py-2 text-sm font-semibold text-white transition hover:bg-[color:var(--brand-secondary)] disabled:cursor-not-allowed disabled:opacity-60"
        >
          {isSubmitting ? "Creating..." : "Create supplier"}
        </button>
      </div>
    </form>
  );
}
