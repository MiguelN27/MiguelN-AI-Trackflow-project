import { Fragment, type ReactNode } from "react";
import { ApprovalControl } from "@/components/rfp/ApprovalControl";
import {
  APPROVAL_STATUS_LABELS,
  RULE_LABELS,
  sectionStatusLine,
  stageLabel,
  type RfpEvaluation,
  type RfpSection,
} from "@/lib/rfp";

const cardClass =
  "rounded-2xl border border-[color:var(--border-soft)] bg-[color:var(--surface)] p-5 shadow-[0_12px_32px_-24px_rgba(37,99,235,0.35)]";
const labelClass = "text-xs font-semibold uppercase tracking-wide text-slate-500";

// --- The draft, as it goes to approval ------------------------------------------------

type Block =
  | { kind: "heading"; text: string; level: number }
  | { kind: "paragraph"; text: string }
  | { kind: "list"; items: string[] }
  | { kind: "table"; header: string[]; rows: string[][]; caption: string };

function cells(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

/**
 * The section's (or the final document's) Markdown as blocks. The pipeline
 * writes a fixed, small subset (`proposal/render.py`, `approval/document.py`):
 * headings, paragraphs, "- " lists, pipe tables and `**bold**`. It is rendered
 * as React elements, never as HTML.
 */
export function parseSectionMarkdown(markdown: string): Block[] {
  const blocks: Block[] = [];
  const lines = markdown.split("\n").map((line) => line.trim());
  let heading = "";
  let index = 0;
  // The run of consecutive lines starting with `prefix`, from `index` on.
  const run = (prefix: string): string[] => {
    const found: string[] = [];
    while (index < lines.length && lines[index].startsWith(prefix)) {
      found.push(lines[index]);
      index += 1;
    }
    return found;
  };
  while (index < lines.length) {
    const line = lines[index];
    if (!line) {
      index += 1;
    } else if (line.startsWith("#")) {
      heading = line.replace(/^#+\s*/, "");
      blocks.push({ kind: "heading", text: heading, level: /^#+/.exec(line)?.[0].length ?? 1 });
      index += 1;
    } else if (line.startsWith("|")) {
      const [header = "", , ...rows] = run("|");
      blocks.push({ kind: "table", header: cells(header), rows: rows.map(cells), caption: heading });
    } else if (line.startsWith("- ")) {
      blocks.push({ kind: "list", items: run("- ").map((item) => item.slice(2)) });
    } else {
      blocks.push({ kind: "paragraph", text: line });
      index += 1;
    }
  }
  return blocks;
}

/** `**bold**` inside a line, as <strong>; everything else stays text. */
export function Inline({ text }: { text: string }) {
  const parts = text.split(/\*\*(.+?)\*\*/g);
  return (
    <>
      {parts.map((part, index) =>
        index % 2 === 1 ? <strong key={index}>{part}</strong> : <Fragment key={index}>{part}</Fragment>,
      )}
    </>
  );
}

const HEADING_TAGS = ["h3", "h4", "h5", "h6"] as const;

/**
 * Blocks as React elements. `headingBase`: the HTML level of a `#` heading
 * (null = every heading as an h5, as in a section card).
 */
export function MarkdownBlocks({ blocks, label, headingBase = null }: { blocks: Block[]; label: string; headingBase?: number | null }) {
  return (
    <div className="space-y-3 text-sm leading-relaxed">
      {blocks.map((block, index) => {
        const key = `${block.kind}-${index}`;
        if (block.kind === "heading") {
          const level = headingBase === null ? 5 : Math.min(headingBase + block.level - 1, 6);
          const Tag = HEADING_TAGS[Math.max(level - 3, 0)];
          const size = level <= 3 ? "text-lg" : level === 4 ? "text-base" : "text-sm";
          return (
            <Tag key={key} className={`pt-2 font-semibold ${size}`}>
              {block.text}
            </Tag>
          );
        }
        if (block.kind === "paragraph") {
          return (
            <p key={key}>
              <Inline text={block.text} />
            </p>
          );
        }
        if (block.kind === "list") {
          return (
            <ul key={key} className="list-disc space-y-1 pl-5">
              {block.items.map((item) => (
                <li key={item}>
                  <Inline text={item} />
                </li>
              ))}
            </ul>
          );
        }
        return (
          <div key={key} className="overflow-x-auto">
            <table className="w-full min-w-[28rem] border-collapse text-left text-sm">
              <caption className="sr-only">
                {label}: {block.caption}
              </caption>
              <thead>
                <tr className="border-b border-[color:var(--border-soft)]">
                  {block.header.map((cell) => (
                    <th key={cell} scope="col" className="py-1.5 pr-3 text-xs font-semibold uppercase tracking-wide text-slate-500">
                      {cell}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {block.rows.map((row, rowIndex) => (
                  <tr key={`${key}-${rowIndex}`} className="border-b border-[color:var(--border-soft)] last:border-0">
                    {row.map((cell, cellIndex) => (
                      <td key={`${cellIndex}-${cell}`} className="py-1.5 pr-3 align-top tabular-nums">
                        <Inline text={cell} />
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      })}
    </div>
  );
}

function DraftDocument({ markdown, label }: { markdown: string; label: string }) {
  // The title and its "For approval by ..." line are already in the card's header.
  const blocks = parseSectionMarkdown(markdown).filter(
    (block, index) =>
      !(index === 0 && block.kind === "heading") &&
      !(index === 1 && block.kind === "paragraph" && block.text.startsWith("For approval by")),
  );
  return <MarkdownBlocks blocks={blocks} label={label} />;
}

// --- The three checks ---------------------------------------------------------------------

function Verdict({ pass }: { pass: boolean }) {
  return pass ? (
    <span className="font-semibold text-emerald-800">✓ Passed</span>
  ) : (
    <span className="font-semibold text-red-700">✕ Failed</span>
  );
}

function Checks({ evaluation }: { evaluation: RfpEvaluation }) {
  const { readability, relevance, compliance } = evaluation;
  const details = readability.details;
  const rows: [string, boolean, ReactNode][] = [
    [
      "Readability",
      readability.pass,
      details.scored === false ? (
        details.note ?? "Not scored."
      ) : (
        <>
          Flesch reading ease {readability.score ?? "-"} (needs {details.min_ease ?? 30}+), grade{" "}
          {details.flesch_kincaid_grade ?? "-"} (needs {details.max_grade ?? 14} or less).
        </>
      ),
    ],
    [
      "Answers the RFP",
      relevance.pass,
      relevance.pass ? (
        `Answers all ${relevance.covered_aspects.length} requirements from the RFP and the briefing.`
      ) : (
        <ul className="list-disc space-y-1 pl-5">
          {relevance.missing_aspects.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      ),
    ],
    [
      "Company guidelines",
      compliance.pass,
      compliance.pass ? (
        `Meets the ${compliance.checked_rule_ids.length} guidelines that apply to this section.`
      ) : (
        <ul className="list-disc space-y-1 pl-5">
          {compliance.violations.map((violation, index) => (
            <li key={`${violation.rule_id}-${index}`}>
              <span className="font-semibold">{RULE_LABELS[violation.rule_id] ?? violation.rule_id}:</span>{" "}
              {violation.message}
              {violation.quote ? <span className="block text-xs italic text-slate-600">“{violation.quote}”</span> : null}
            </li>
          ))}
        </ul>
      ),
    ],
  ];
  return (
    <dl className="mt-3 space-y-3 text-sm">
      {rows.map(([label, pass, body]) => (
        <div key={label} className="grid gap-1 sm:grid-cols-[11rem_1fr]">
          <dt>
            <span className="block font-semibold">{label}</span>
            <Verdict pass={pass} />
          </dt>
          <dd className="leading-relaxed">{body}</dd>
        </div>
      ))}
    </dl>
  );
}

// --- The card ----------------------------------------------------------------------------------

function cardTitle(section: RfpSection): string {
  if (section.approval_status === "approved") return `Approved by ${section.approver ?? section.owner}`;
  if (section.approval_status === "rejected") return `Rejected: ${section.department_name}`;
  return `For approval by ${section.owner}`;
}

/** One department's section of the proposal: the draft, its checks, and its owner's approval. */
export function ProposalSectionCard({
  section,
  currentStage,
  ticketId,
  onDecided,
}: {
  section: RfpSection;
  currentStage: string | null;
  ticketId?: string;
  onDecided?: () => void;
}) {
  const headingId = `draft-${section.department_id}`;
  const evaluation = section.evaluation;
  const inLoop = section.draft_status === "drafting" || section.draft_status === "under_evaluation" || section.draft_status === "pending";
  const working = inLoop && currentStage?.includes(`:${section.department_id}`) ? stageLabel(currentStage) : null;
  return (
    // min-w-0: a grid item, so its wide tables scroll inside it instead of widening the page.
    <article aria-labelledby={headingId} className={`${cardClass} min-w-0`}>
      <p className={labelClass}>{section.department_name}</p>
      <h3 id={headingId} className="mt-1 text-lg font-semibold">
        {cardTitle(section)}
      </h3>
      <p className="mt-1 text-sm">
        <span className={section.needs_human_review ? "font-semibold text-orange-700" : section.draft_status === "passed" ? "font-semibold text-emerald-800" : section.draft_status === "failed" ? "font-semibold text-red-700" : "text-slate-700"}>
          {sectionStatusLine(section)}
        </span>
        {section.approval_status ? (
          <span className="text-slate-600"> · {APPROVAL_STATUS_LABELS[section.approval_status] ?? section.approval_status}</span>
        ) : null}
      </p>
      {section.is_sla_section ? (
        <p className="mt-1 text-xs text-slate-600">This section states the proposal&apos;s on-time delivery SLA.</p>
      ) : null}
      {working ? (
        <p className="mt-2 text-sm text-[color:var(--brand-primary)]" role="status">
          Now: {working}
        </p>
      ) : null}

      {section.needs_human_review && evaluation ? (
        <div className="mt-4 rounded-lg border border-orange-200 bg-orange-50 p-4 text-sm text-orange-900">
          <p className="font-semibold">
            The draft still fails after {section.draft_iterations} attempt{section.draft_iterations === 1 ? "" : "s"}. {section.owner} should
            review it before approving.
          </p>
          {section.review_note ? <p className="mt-1">{section.review_note}</p> : null}
          {evaluation.feedback_for_generator ? (
            <>
              <p className="mt-2 font-semibold">What is still wrong:</p>
              <ol className="mt-1 list-decimal space-y-1 pl-5">
                {evaluation.feedback_for_generator.split("\n").map((line) => (
                  <li key={line}>{line.replace(/^\d+\.\s*/, "")}</li>
                ))}
              </ol>
            </>
          ) : null}
        </div>
      ) : null}

      {evaluation ? (
        <section aria-label={`Checks of the ${section.department_name} draft`} className="mt-4">
          <h4 className="text-sm font-semibold">
            Automatic review of {section.review_rounds > 0 ? `revision ${section.review_rounds}, ` : ""}attempt {evaluation.iteration}
          </h4>
          <Checks evaluation={evaluation} />
        </section>
      ) : null}

      {section.draft_content ? (
        <details className="mt-4" open={section.draft_status === "passed" || section.needs_human_review}>
          <summary className="cursor-pointer text-sm font-semibold text-[color:var(--brand-primary)]">
            The draft{section.draft_status === "passed" || section.needs_human_review ? "" : " so far"}
          </summary>
          <div className="mt-3">
            <DraftDocument markdown={section.draft_content} label={section.department_name} />
          </div>
          <details className="mt-4">
            <summary className="cursor-pointer text-xs font-semibold text-slate-600">Markdown handed to approval</summary>
            <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-100 p-3 text-xs text-slate-700">
              {section.draft_content}
            </pre>
          </details>
        </details>
      ) : null}
      {section.guard_notes.length > 0 ? (
        <details className="mt-3">
          <summary className="cursor-pointer text-xs font-semibold text-slate-600">
            Removed from the draft ({section.guard_notes.length})
          </summary>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-xs text-slate-600">
            {section.guard_notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </details>
      ) : null}
      {ticketId && onDecided && section.approval_status ? (
        <ApprovalControl ticketId={ticketId} section={section} onDecided={onDecided} />
      ) : null}
    </article>
  );
}
