/**
 * Token storage and the redirect contract around it.
 *
 * The API is stateless JWT with no auth cookie, so the token can only live in
 * `localStorage` and every read has to tolerate a browser that denies access to
 * it (private mode, blocked site data) as well as running on the server.
 */

/** Shared with `uis/website`: same product, same API, same token. */
export const TOKEN_STORAGE_KEY = "trackflow.access_token";

export const LOGIN_PATH = "/login";

/** The view a successful login lands on when no `next` path was requested. */
export const DEFAULT_AUTHENTICATED_PATH = "/";

/**
 * Fired when a protected call is rejected. `AuthProvider` listens for it and
 * routes to the login page, so no call site has to handle expiry itself.
 */
export const UNAUTHORIZED_EVENT = "trackflow:unauthorized";

export const FORGOT_PASSWORD_PATH = "/forgot-password";

/**
 * How `/reset-password` tells `/login` that it just succeeded. A query flag
 * rather than stored state, so the message belongs to the redirect that set it
 * and cannot resurface on a later visit.
 */
export const RESET_DONE_PARAM = "reset";
export const RESET_DONE_VALUE = "success";
export const LOGIN_AFTER_RESET_PATH = `${LOGIN_PATH}?${RESET_DONE_PARAM}=${RESET_DONE_VALUE}`;

/** Reads that flag out of a `window.location.search` string. */
export function hasResetSuccessFlag(search: string): boolean {
  return new URLSearchParams(search).get(RESET_DONE_PARAM) === RESET_DONE_VALUE;
}

/** Reads the reset token out of a `window.location.search` string. */
export function readResetToken(search: string): string | null {
  const token = new URLSearchParams(search).get("token");

  return token && token.trim() ? token : null;
}

export function readToken(): string | null {
  if (typeof window === "undefined") {
    return null;
  }

  try {
    const token = window.localStorage.getItem(TOKEN_STORAGE_KEY);
    return token && token.trim() ? token : null;
  } catch {
    return null;
  }
}

export function storeToken(token: string): void {
  if (typeof window === "undefined") {
    return;
  }

  try {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, token);
  } catch {
    // A browser that refuses storage still gets a working session for this
    // page load; the next protected call will simply send it back to login.
  }
}

export function clearToken(): void {
  if (typeof window === "undefined") {
    return;
  }

  try {
    window.localStorage.removeItem(TOKEN_STORAGE_KEY);
  } catch {
    // Nothing to do: the token was already unreachable.
  }
}

/** Clears the token and asks the session layer to redirect. */
export function handleUnauthorized(): void {
  clearToken();

  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  }
}

/**
 * Only same-origin absolute paths survive, so a crafted `?next=` cannot bounce
 * a freshly authenticated user to another site.
 */
export function sanitizeNextPath(value: string | null | undefined): string | null {
  if (!value || !value.startsWith("/") || value.startsWith("//")) {
    return null;
  }

  return value === LOGIN_PATH || value.startsWith(`${LOGIN_PATH}?`) ? null : value;
}

export function buildLoginUrl(nextPath?: string | null): string {
  const safeNextPath = sanitizeNextPath(nextPath);

  return safeNextPath ? `${LOGIN_PATH}?next=${encodeURIComponent(safeNextPath)}` : LOGIN_PATH;
}
