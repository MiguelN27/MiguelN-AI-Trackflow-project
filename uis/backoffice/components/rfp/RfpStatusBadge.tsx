import { STATUS_LABELS, isWorking, type RfpStatus } from "@/lib/rfp";

const WORKING = "border-blue-200 bg-blue-50 text-blue-800";

const STATUS_CLASSES: Partial<Record<RfpStatus, string>> = {
  analyzing: WORKING,
  intake_complete: "border-emerald-200 bg-emerald-50 text-emerald-800",
  discarded: "border-slate-300 bg-slate-100 text-slate-700",
  failed: "border-red-200 bg-red-50 text-red-700",
  drafting: WORKING,
  under_evaluation: WORKING,
  needs_human_review: "border-orange-200 bg-orange-50 text-orange-800",
  waiting_for_approval: "border-emerald-200 bg-emerald-50 text-emerald-800",
};

const LATER_PARTS = "border-blue-200 bg-blue-50 text-blue-800";

/** The ticket status as a labelled badge. The text carries the meaning; colour only repeats it. */
export function RfpStatusBadge({ status }: { status: RfpStatus }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-semibold ${
        STATUS_CLASSES[status] ?? LATER_PARTS
      }`}
    >
      {isWorking(status) ? (
        <span
          aria-hidden="true"
          className="h-3 w-3 animate-spin rounded-full border-2 border-blue-300 border-t-blue-700 motion-reduce:animate-none"
        />
      ) : null}
      {STATUS_LABELS[status]}
    </span>
  );
}
