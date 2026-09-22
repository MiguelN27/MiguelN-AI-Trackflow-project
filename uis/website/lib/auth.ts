import { ApiError, extractApiFieldErrors } from "@/lib/auth-api-client";
import type {
  AuthenticatedUser,
  FieldErrors,
  LoginField,
  LoginFormValues,
  Profile,
  ProfileField,
  ProfileFormValues,
  RegisterField,
  RegisterFormValues,
  UserRole,
} from "@/types/auth";

/** Mirrors the API contract in `services/users/models.py`. */
export const MIN_PASSWORD_LENGTH = 8;
export const MAX_NAME_LENGTH = 120;
export const MAX_PHONE_LENGTH = 40;
export const MAX_ADDRESS_LENGTH = 255;

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const USER_ROLES: readonly UserRole[] = ["admin", "manager", "user"];

export function emptyLoginFormValues(): LoginFormValues {
  return { email: "", password: "" };
}

export function emptyRegisterFormValues(): RegisterFormValues {
  return { email: "", password: "", confirmPassword: "", name: "", phone: "", address: "" };
}

export function profileFormValuesFrom(profile: Profile | null): ProfileFormValues {
  return {
    name: profile?.name ?? "",
    phone: profile?.phone ?? "",
    address: profile?.address ?? "",
  };
}

function validateEmail(email: string): string | null {
  const trimmed = email.trim();

  if (!trimmed) {
    return "Email is required.";
  }

  return EMAIL_PATTERN.test(trimmed) ? null : "Enter a valid email address.";
}

function validateLength(value: string, max: number, label: string): string | null {
  return value.trim().length > max ? `${label} must be ${max} characters or fewer.` : null;
}

function withoutEmptyEntries<TField extends string>(
  errors: FieldErrors<TField>,
): FieldErrors<TField> | null {
  return Object.keys(errors).length > 0 ? errors : null;
}

export function validateLoginForm(values: LoginFormValues): FieldErrors<LoginField> | null {
  const errors: FieldErrors<LoginField> = {};

  const emailError = validateEmail(values.email);
  if (emailError) {
    errors.email = emailError;
  }

  if (!values.password) {
    errors.password = "Password is required.";
  }

  return withoutEmptyEntries(errors);
}

export function validateRegisterForm(values: RegisterFormValues): FieldErrors<RegisterField> | null {
  const errors: FieldErrors<RegisterField> = {};

  const emailError = validateEmail(values.email);
  if (emailError) {
    errors.email = emailError;
  }

  if (!values.password) {
    errors.password = "Password is required.";
  } else if (values.password.length < MIN_PASSWORD_LENGTH) {
    errors.password = `Password must be at least ${MIN_PASSWORD_LENGTH} characters.`;
  }

  if (!values.confirmPassword) {
    errors.confirmPassword = "Confirm your password.";
  } else if (values.confirmPassword !== values.password) {
    errors.confirmPassword = "Passwords do not match.";
  }

  const nameError = validateLength(values.name, MAX_NAME_LENGTH, "Name");
  if (nameError) {
    errors.name = nameError;
  }

  const phoneError = validateLength(values.phone, MAX_PHONE_LENGTH, "Phone");
  if (phoneError) {
    errors.phone = phoneError;
  }

  const addressError = validateLength(values.address, MAX_ADDRESS_LENGTH, "Address");
  if (addressError) {
    errors.address = addressError;
  }

  return withoutEmptyEntries(errors);
}

export function validateProfileForm(values: ProfileFormValues): FieldErrors<ProfileField> | null {
  const errors: FieldErrors<ProfileField> = {};

  const nameError = validateLength(values.name, MAX_NAME_LENGTH, "Name");
  if (nameError) {
    errors.name = nameError;
  }

  const phoneError = validateLength(values.phone, MAX_PHONE_LENGTH, "Phone");
  if (phoneError) {
    errors.phone = phoneError;
  }

  const addressError = validateLength(values.address, MAX_ADDRESS_LENGTH, "Address");
  if (addressError) {
    errors.address = addressError;
  }

  return withoutEmptyEntries(errors);
}

function optionalField(value: string): string | undefined {
  const trimmed = value.trim();
  return trimmed ? trimmed : undefined;
}

/** `POST /users` body. Blank profile fields are omitted rather than sent empty. */
export function buildRegistrationPayload(values: RegisterFormValues) {
  return {
    email: values.email.trim().toLowerCase(),
    password: values.password,
    name: optionalField(values.name),
    phone: optionalField(values.phone),
    address: optionalField(values.address),
  };
}

/** `PUT /profiles/me` body. A cleared input is sent as an explicit null, which is how the API erases it. */
export function buildProfilePayload(values: ProfileFormValues) {
  return {
    name: optionalField(values.name) ?? null,
    phone: optionalField(values.phone) ?? null,
    address: optionalField(values.address) ?? null,
  };
}

function readNullableString(record: Record<string, unknown>, key: string): string | null {
  const value = record[key];
  return typeof value === "string" && value.trim() ? value : null;
}

export function normalizeProfile(payload: unknown): Profile {
  if (typeof payload !== "object" || payload === null) {
    throw new Error("Unexpected profile payload received from the API");
  }

  const record = payload as Record<string, unknown>;

  return {
    id: typeof record.id === "string" ? record.id : "",
    user_id: typeof record.user_id === "string" ? record.user_id : "",
    name: readNullableString(record, "name"),
    phone: readNullableString(record, "phone"),
    address: readNullableString(record, "address"),
  };
}

export function normalizeAuthenticatedUser(payload: unknown): AuthenticatedUser {
  if (typeof payload !== "object" || payload === null) {
    throw new Error("Unexpected account payload received from the API");
  }

  const record = payload as Record<string, unknown>;
  const role = typeof record.role === "string" && (USER_ROLES as readonly string[]).includes(record.role)
    ? (record.role as UserRole)
    : "user";

  return {
    id: typeof record.id === "string" ? record.id : "",
    email: typeof record.email === "string" ? record.email : "",
    role,
    is_active: record.is_active !== false,
    profile: record.profile ? normalizeProfile(record.profile) : null,
  };
}

export function extractAccessToken(payload: unknown): string {
  const token =
    typeof payload === "object" && payload !== null
      ? (payload as { access_token?: unknown }).access_token
      : undefined;

  if (typeof token !== "string" || !token.trim()) {
    throw new Error("The API did not return an access token");
  }

  return token;
}

/**
 * Turns an API failure into per-field messages. Anything the form has no input
 * for lands on `form`, so no message is ever silently dropped.
 */
export function toFieldErrors<TField extends string>(
  error: unknown,
  knownFields: readonly TField[],
): FieldErrors<TField> {
  const errors: FieldErrors<TField> = {};

  if (!(error instanceof ApiError)) {
    errors.form = error instanceof Error ? error.message : "Something went wrong. Please try again.";
    return errors;
  }

  for (const { field, message } of extractApiFieldErrors(error.payload)) {
    if ((knownFields as readonly string[]).includes(field)) {
      errors[field as TField] = message;
    } else {
      errors.form = errors.form ? `${errors.form} · ${message}` : message;
    }
  }

  if (Object.keys(errors).length === 0) {
    errors.form = error.message;
  }

  return errors;
}
