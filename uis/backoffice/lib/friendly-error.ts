/**
 * One place that decides what a user is allowed to read about a failure.
 *
 * The rule this file enforces: a message is shown only when it came back in the
 * API's documented `{ field, message }` shape **and** the status is one the API
 * writes plain language for. Everything else - a 5xx, an unrecognised body, an
 * HTML error page from a proxy, a `TypeError: Failed to fetch` from the network
 * layer, a thrown non-Error - is answered with text written here.
 *
 * Without that rule the fallback in `ApiError` ("Request failed with status
 * 500") and the browser's own network errors leak straight into the UI, which
 * is exactly what a form must never show.
 */

import { ApiError, UnauthorizedError, extractApiProblems } from "@/lib/api-client";

export type FriendlyError = {
  /** Safe to render as-is. Never empty. */
  message: string;
  /** Keyed by the API's field name, so a form can look up its own inputs. */
  fieldErrors: Record<string, string>;
};

const OFFLINE_MESSAGE =
  "Could not reach the incident service. Check your connection and try again.";

const SERVER_MESSAGE =
  "The incident service is having trouble right now. Nothing was lost - please try again in a moment.";

const SESSION_MESSAGE = "Your session has expired. Please sign in again.";

const NOT_FOUND_MESSAGE = "That incident no longer exists. Refresh the list to see the latest.";

/**
 * Statuses whose `message` the API authors for a reader.
 *
 * 400 covers both halves of `services/core/http_errors.py`: a field that failed
 * to validate and a lifecycle transition that was refused. 404 names the
 * missing id. A 5xx is deliberately absent - the body is generic by design, and
 * trusting it would also trust whatever a proxy or gateway put there instead.
 */
const READABLE_STATUSES = new Set([400, 404]);

/** Longer than any message the API writes; a runaway string is not a sentence. */
const MAX_TRUSTED_MESSAGE_LENGTH = 300;

/**
 * Shapes that betray a message was never meant for a reader.
 *
 * A last line of defence rather than a routine check: the API is not supposed
 * to be able to produce any of these, so if one appears the frontend's own text
 * is the right answer.
 */
const LEAKED_INTERNALS = [
  /traceback/i,
  /\bat [\w$.]+ \(/,
  /^\w*(Error|Exception):/,
  /\.(py|ts|tsx|js):\d+/,
  /<\/?[a-z]+[\s>]/i,
];

function isReadable(message: string): boolean {
  return (
    message.length > 0 &&
    message.length <= MAX_TRUSTED_MESSAGE_LENGTH &&
    !LEAKED_INTERNALS.some((pattern) => pattern.test(message))
  );
}

/**
 * Reduce any thrown value to something showable.
 *
 * `fallback` is the form-level message for a validation failure, where the real
 * information is next to the inputs and a repeat of the first field error at
 * the top of the form would only be noise.
 */
export function describeError(error: unknown, fallback: string): FriendlyError {
  if (error instanceof UnauthorizedError) {
    return { message: SESSION_MESSAGE, fieldErrors: {} };
  }

  if (!(error instanceof ApiError)) {
    // Either the fetch never completed or something threw that is not ours.
    // In both cases there is nothing here a user should read.
    return { message: OFFLINE_MESSAGE, fieldErrors: {} };
  }

  if (error.status >= 500) {
    return { message: SERVER_MESSAGE, fieldErrors: {} };
  }

  if (!READABLE_STATUSES.has(error.status)) {
    return { message: fallback, fieldErrors: {} };
  }

  const problems = extractApiProblems(error.payload).filter((problem) => isReadable(problem.message));

  if (problems.length === 0) {
    // A 400 or 404 whose body we could not read. The status is still known, so
    // the more specific of the two messages is safe to use.
    return {
      message: error.status === 404 ? NOT_FOUND_MESSAGE : fallback,
      fieldErrors: {},
    };
  }

  const fieldErrors: Record<string, string> = {};
  for (const problem of problems) {
    // First message per field wins: the API lists them in the order it found
    // them, and re-reporting the same input twice tells the user nothing.
    if (problem.field && !(problem.field in fieldErrors)) {
      fieldErrors[problem.field] = problem.message;
    }
  }

  // A problem with no field name - a refused transition, a 404, an unparseable
  // body - has nowhere to sit next to an input, so it becomes the form-level
  // message. When every problem did name a field, the fallback introduces them.
  const unattached = problems.find((problem) => !problem.field);

  return {
    message: unattached?.message ?? fallback,
    fieldErrors,
  };
}
