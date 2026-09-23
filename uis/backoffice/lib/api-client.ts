import { handleUnauthorized, readToken } from "@/lib/auth-storage";

const apiBaseUrl = process.env.NEXT_PUBLIC_API_URL?.trim().replace(/\/+$/, "") ?? "";

export function getApiBaseUrl(): string {
  return apiBaseUrl;
}

export function buildApiUrl(path: string): string {
  if (!apiBaseUrl) {
    throw new Error("NEXT_PUBLIC_API_URL is not configured");
  }

  return `${apiBaseUrl}${path.startsWith("/") ? path : `/${path}`}`;
}

type ValidationDetail = {
  loc?: unknown;
  msg?: unknown;
};

/** One FastAPI validation entry, reduced to what a form needs to render it. */
export type ApiFieldError = {
  field: string;
  message: string;
};

/**
 * Carries the decoded body alongside the flattened message, so a form can map
 * a 422 back onto its inputs. `message` keeps the shape every existing call
 * site already relies on.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(status: number, payload: unknown) {
    super(extractApiErrorMessage(payload, status));
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

/** A 401 from a protected route: the token is missing, expired or invalid. */
export class UnauthorizedError extends ApiError {
  constructor(payload: unknown = null) {
    super(401, payload);
    this.name = "UnauthorizedError";
  }
}

/**
 * FastAPI validation errors arrive as `{ loc: ["body", "name"], msg: "..." }`.
 * The first segment names the request part, so only the rest is useful as a field path.
 */
function formatValidationDetail(detail: ValidationDetail): string {
  const location = Array.isArray(detail.loc)
    ? detail.loc.filter((part): part is string | number => typeof part === "string" || typeof part === "number")
    : [];
  const fieldPath = location.slice(1).join(".");
  const message = typeof detail.msg === "string" && detail.msg.trim() ? detail.msg.trim() : "Invalid value";

  return fieldPath ? `${fieldPath}: ${message}` : message;
}

/** `{ detail: { field, message } }` - the shape every `/api/incidents` error uses. */
function readProblemDetail(detail: unknown): ApiFieldError | null {
  if (typeof detail !== "object" || detail === null || Array.isArray(detail)) {
    return null;
  }

  const record = detail as { field?: unknown; message?: unknown };
  if (typeof record.message !== "string" || !record.message.trim()) {
    return null;
  }

  return {
    field: typeof record.field === "string" ? record.field : "",
    message: record.message.trim(),
  };
}

/**
 * Handles every error shape this API surface produces: `{ detail: string }`
 * (FastAPI's 404), `{ detail: [...] }` (FastAPI's 422) and
 * `{ detail: { field, message } }` (the incident routes).
 */
export function extractApiErrorMessage(payload: unknown, status: number): string {
  const detail =
    typeof payload === "object" && payload !== null ? (payload as { detail?: unknown }).detail : undefined;

  if (typeof detail === "string" && detail.trim()) {
    return detail.trim();
  }

  const problem = readProblemDetail(detail);
  if (problem) {
    return problem.message;
  }

  if (Array.isArray(detail)) {
    const messages = detail
      .filter((item): item is ValidationDetail => typeof item === "object" && item !== null)
      .map(formatValidationDetail);

    if (messages.length > 0) {
      return messages.join(" · ");
    }
  }

  return `Request failed with status ${status}`;
}

/** Splits a 422 body into per-field entries. Returns `[]` for any other shape. */
export function extractApiFieldErrors(payload: unknown): ApiFieldError[] {
  const detail =
    typeof payload === "object" && payload !== null ? (payload as { detail?: unknown }).detail : undefined;

  if (!Array.isArray(detail)) {
    return [];
  }

  return detail
    .filter((item): item is ValidationDetail => typeof item === "object" && item !== null)
    .map((item) => {
      const location = Array.isArray(item.loc)
        ? item.loc.filter((part): part is string => typeof part === "string")
        : [];
      const message = typeof item.msg === "string" && item.msg.trim() ? item.msg.trim() : "Invalid value";

      return { field: location.slice(1).join("."), message };
    });
}

/**
 * Per-field errors from an `/api/incidents` response, in the order the API
 * listed them.
 *
 * A validation failure carries `errors` with every bad field plus `detail`
 * holding the first; a business-rule refusal or a 404 carries `detail` alone.
 * Reading `errors` first and falling back to `detail` covers both without the
 * caller having to know which it got.
 *
 * Separate from `extractApiFieldErrors`, which reads FastAPI's raw 422 array:
 * the supplier and auth screens depend on that one, and the two payload shapes
 * have nothing in common beyond the word "detail".
 */
export function extractApiProblems(payload: unknown): ApiFieldError[] {
  if (typeof payload !== "object" || payload === null) {
    return [];
  }

  const record = payload as { detail?: unknown; errors?: unknown };

  if (Array.isArray(record.errors)) {
    const problems = record.errors
      .map(readProblemDetail)
      .filter((problem): problem is ApiFieldError => problem !== null);

    if (problems.length > 0) {
      return problems;
    }
  }

  const single = readProblemDetail(record.detail);
  return single ? [single] : [];
}

async function readErrorPayload(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function withJsonHeaders(init?: RequestInit, token?: string | null): HeadersInit {
  return {
    Accept: "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(init?.headers ?? {}),
  };
}

/** Unauthenticated call. Use it only for `POST /users` and `POST /auth/login`. */
export async function requestApi(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(buildApiUrl(path), {
    ...init,
    headers: withJsonHeaders(init),
  });

  if (!response.ok) {
    throw new ApiError(response.status, await readErrorPayload(response));
  }

  return response;
}

/**
 * Call against a protected route. Reads the token, sends it as
 * `Authorization: Bearer <token>`, and on a 401 clears it and hands control to
 * the session layer before rethrowing so the caller stops.
 */
export async function requestAuthenticatedApi(path: string, init?: RequestInit): Promise<Response> {
  const token = readToken();

  if (!token) {
    handleUnauthorized();
    throw new UnauthorizedError({ detail: "Your session has expired. Please sign in again." });
  }

  const response = await fetch(buildApiUrl(path), {
    ...init,
    headers: withJsonHeaders(init, token),
  });

  if (response.status === 401) {
    const payload = await readErrorPayload(response);
    handleUnauthorized();
    throw new UnauthorizedError(payload);
  }

  if (!response.ok) {
    throw new ApiError(response.status, await readErrorPayload(response));
  }

  return response;
}

export async function parseResponseJson(response: Response): Promise<unknown | null> {
  if (response.status === 204) {
    return null;
  }

  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) {
    return null;
  }

  return response.json();
}
