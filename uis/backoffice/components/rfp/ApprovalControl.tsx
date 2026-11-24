"use client";

import { type FormEvent, useId, useState } from "react";
import { StateMessage } from "@/components/common/StateMessage";
import { ApiError } from "@/lib/api-client";
import {
  DECISION_ACTION_LABELS,
  TRIGGER_LABELS,
  approvalLine,
  describeDecisionError,
  formatDate,
  submitDecision,
  type DecisionAction,
  type RfpApprovalDecision,
  type RfpSection,
} from "@/lib/rfp";

const COMMENT_MAX = 2000;

const ACTION_COPY: Record<DecisionAction, { label: string; hint: string }> = {
  approve: { label: "Approve", hint: "The section goes into the final document as it is." },
  request_changes: {
    label: "Request changes",
    hint: "The section is redrafted with your comment, checked again, and comes back to you.",
  },
  reject: {
    label: "Reject",
    hint: "Your department will not sign this proposal: it is closed and no document goes out.",
  },
};

function who(decision: RfpApprovalDecision): string {
  if (decision.source === "arbiter") return `${decision.decided_by_name} (arbitration)`;
  if (decision.source === "system") return "TrackFlow";
  return decision.decided_by_name;
}

/** Every decision on the section so far, oldest first. */
export function DecisionHistory({ decisions }: { decisions: RfpApprovalDecision[] }) {
  if (decisions.length === 0) return null;
  return (
    <details className="mt-3">
      <summary className="cursor-pointer text-xs font-semibold text-slate-600">Decisions so far ({decisions.length})</summary>
      <ol className="mt-2 space-y-2 text-xs text-slate-700">
        {decisions.map((decision) => (
          <li key={decision.decision_id}>
            <span className="font-semibold">
              Round {decision.review_round}: {who(decision)} {DECISION_ACTION_LABELS[decision.action] ?? decision.action}
            </span>
            <span className="text-slate-600"> · {formatDate(decision.created_at)}</span>
            {decision.trigger_id ? (
              <span className="block text-slate-600">{TRIGGER_LABELS[decision.trigger_id] ?? decision.trigger_id}</span>
            ) : null}
            {decision.comment ? <span className="block whitespace-pre-line">“{decision.comment}”</span> : null}
            {decision.status === "stale" || decision.status === "refused" ? (
              <span className="block text-orange-700">Not applied: {decision.status_note ?? decision.status}</span>
            ) : null}
          </li>
        ))}
      </ol>
    </details>
  );
}

/**
 * A section's approval: its state for everyone, and the decision form for its owner.
 *
 * The form shows only when the signed-in account is the department's owner and
 * the section waits for them (`can_decide`, decided by the API). A recorded
 * decision is applied in the background; `onDecided` re-reads the ticket.
 */
export function ApprovalControl({
  ticketId,
  section,
  onDecided,
}: {
  ticketId: string;
  section: RfpSection;
  onDecided: () => void;
}) {
  const formId = useId();
  const [action, setAction] = useState<DecisionAction | null>(null);
  const [comment, setComment] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [recorded, setRecorded] = useState<string | null>(null);

  const needsComment = action === "request_changes" || action === "reject";
  const status = section.approval_status;
  const tone = status === "approved" ? "text-emerald-800" : status === "rejected" ? "text-red-700" : "text-slate-800";

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!action || isSaving) return;
    if (needsComment && !comment.trim()) {
      setError("Say what needs to change: a comment is required when you request changes or reject.");
      return;
    }
    setIsSaving(true);
    setError(null);
    try {
      await submitDecision(ticketId, section.department_id, {
        action,
        comment: comment.trim() || null,
        review_round: section.review_round,
      });
      setRecorded(`Your decision (${ACTION_COPY[action].label.toLowerCase()}) is recorded. Applying it now…`);
      setAction(null);
      setComment("");
      onDecided();
    } catch (failure: unknown) {
      setError(describeDecisionError(failure));
      // The section moved on: show its current state.
      if (failure instanceof ApiError && failure.status === 409) onDecided();
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <section aria-labelledby={`${formId}-heading`} className="mt-4 rounded-xl border border-[color:var(--border-soft)] p-4">
      <h4 id={`${formId}-heading`} className="text-sm font-semibold">
        Approval by {section.owner}
      </h4>
      <p className={`mt-1 text-sm font-semibold ${tone}`} aria-live="polite">
        {approvalLine(section)}
      </p>
      {!section.approver_configured && status === "pending" ? (
        <p className="mt-1 text-xs text-orange-700">
          No account is configured for {section.owner} yet (RFP_APPROVERS), so nobody can decide on this section.
        </p>
      ) : null}

      {recorded && !section.can_decide ? (
        <StateMessage tone="success" className="mt-3">
          {recorded}
        </StateMessage>
      ) : null}

      {section.can_decide ? (
        // noValidate: the form's own check says what is missing, announced as an alert,
        // instead of the browser's tooltip.
        <form onSubmit={submit} noValidate className="mt-3 space-y-3" aria-describedby={`${formId}-round`}>
          <p id={`${formId}-round`} className="text-xs text-slate-600">
            You are deciding on round {section.review_round}
            {section.max_review_rounds != null
              ? `; ${Math.max(section.max_review_rounds - section.review_rounds, 0)} revision(s) left`
              : ""}
            .
          </p>
          <fieldset>
            <legend className="mb-2 text-sm font-medium">Your decision</legend>
            <div className="space-y-2">
              {section.allowed_actions.map((option) => (
                <label key={option} className="flex items-start gap-2 text-sm">
                  <input
                    type="radio"
                    name={`${formId}-action`}
                    value={option}
                    checked={action === option}
                    onChange={() => setAction(option)}
                    className="mt-1 h-4 w-4 accent-[color:var(--brand-primary)]"
                  />
                  <span>
                    <span className="font-semibold">{ACTION_COPY[option].label}</span>
                    <span className="block text-xs text-slate-600">{ACTION_COPY[option].hint}</span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>
          <label className="block text-sm">
            <span className="mb-1 block font-medium">
              Comment {needsComment ? "(required)" : "(optional)"}
            </span>
            <textarea
              value={comment}
              onChange={(event) => setComment(event.target.value)}
              rows={3}
              maxLength={COMMENT_MAX}
              aria-required={needsComment}
              aria-invalid={Boolean(error && needsComment && !comment.trim())}
              placeholder={needsComment ? "What needs to change, and why" : "Anything Sales should know"}
              className="w-full rounded-xl border border-[color:var(--border-soft)] bg-[color:var(--surface)] px-3 py-2 text-sm"
            />
          </label>
          {error ? (
            <StateMessage tone="error">
              <span role="alert">{error}</span>
            </StateMessage>
          ) : null}
          <button
            type="submit"
            disabled={!action || isSaving}
            className="inline-flex min-h-10 items-center rounded-xl bg-[color:var(--brand-primary)] px-4 py-2 text-sm font-semibold text-[color:var(--on-brand)] transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {isSaving ? "Recording…" : "Record decision"}
          </button>
        </form>
      ) : error ? (
        <StateMessage tone="error" className="mt-3">
          <span role="alert">{error}</span>
        </StateMessage>
      ) : null}

      <DecisionHistory decisions={section.decisions} />
    </section>
  );
}
