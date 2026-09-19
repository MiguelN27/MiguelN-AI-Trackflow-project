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

/** Handles both FastAPI error shapes: `{ detail: string }` (404) and `{ detail: [...] }` (422). */
export function extractApiErrorMessage(payload: unknown, status: number): string {
  const detail =
    typeof payload === "object" && payload !== null ? (payload as { detail?: unknown }).detail : undefined;

  if (typeof detail === "string" && detail.trim()) {
    return detail.trim();
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

async function readErrorPayload(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

export async function requestApi(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(buildApiUrl(path), {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.headers ?? {}),
    },
  });

  if (!response.ok) {
    throw new Error(extractApiErrorMessage(await readErrorPayload(response), response.status));
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
