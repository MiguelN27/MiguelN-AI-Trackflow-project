import { parseResponseJson, requestApi, requestAuthenticatedApi } from "@/lib/api-client";
import { clearToken, storeToken } from "@/lib/auth-storage";
import {
  buildProfilePayload,
  buildRegistrationPayload,
  extractAccessToken,
  normalizeAuthenticatedUser,
  normalizeProfile,
} from "@/lib/auth";
import type {
  AuthenticatedUser,
  LoginFormValues,
  Profile,
  ProfileFormValues,
  RegisterFormValues,
} from "@/types/auth";

const JSON_HEADERS = { "Content-Type": "application/json" };

/**
 * Exchanges credentials for a JWT and stores it. Storing here rather than at
 * the call site means no login path can end with an unsaved token.
 */
export async function login(values: LoginFormValues): Promise<string> {
  const response = await requestApi("/auth/login", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ email: values.email.trim().toLowerCase(), password: values.password }),
  });

  const token = extractAccessToken(await parseResponseJson(response));
  storeToken(token);

  return token;
}

/**
 * Raised when `POST /users` succeeded but the follow-up login did not. The
 * account exists, so the form must not report it as a registration failure.
 */
export class PostRegistrationLoginError extends Error {
  constructor() {
    super("Your account was created, but signing you in failed. Please sign in manually.");
    this.name = "PostRegistrationLoginError";
  }
}

/**
 * Creates the account, then signs in with the same credentials so the user
 * never has to type them twice. `POST /users` also creates the linked profile
 * from the optional name, phone and address.
 */
export async function register(values: RegisterFormValues): Promise<string> {
  await requestApi("/users", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(buildRegistrationPayload(values)),
  });

  try {
    return await login({ email: values.email, password: values.password });
  } catch {
    throw new PostRegistrationLoginError();
  }
}

export async function fetchCurrentUser(): Promise<AuthenticatedUser> {
  const response = await requestAuthenticatedApi("/auth/me");

  return normalizeAuthenticatedUser(await parseResponseJson(response));
}

export async function updateMyProfile(values: ProfileFormValues): Promise<Profile> {
  const response = await requestAuthenticatedApi("/profiles/me", {
    method: "PUT",
    headers: JSON_HEADERS,
    body: JSON.stringify(buildProfilePayload(values)),
  });

  return normalizeProfile(await parseResponseJson(response));
}

/** There is no server-side session to end, so logging out is discarding the token. */
export function logout(): void {
  clearToken();
}
