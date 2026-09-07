"use client";

import { StateMessage } from "@/components/common/StateMessage";
import { analyzeIncidentFile, incidentExportUrl } from "@/services/incidents-service";
import {
  INCIDENT_CATEGORIES,
  INCIDENT_STATUSES,
  type IncidentAnalysisResponse,
} from "@/types/incident";
import type { ChangeEvent, DragEvent, FormEvent } from "react";
import { useState } from "react";

const MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024;

function formatSatisfaction(value: number | null): string {
  return value === null ? "No scored closed cases" : `${value.toFixed(2)} / 5`;
}

export default function IncidentAnalysisPage() {
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [analysisResponse, setAnalysisResponse] = useState<IncidentAnalysisResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [isUploading, setIsUploading] = useState(false);

  const invalidReasonCounts = new Map<string, number>();
  for (const invalidRecord of analysisResponse?.analysis.invalid_records ?? []) {
    for (const reason of invalidRecord.reasons) {
      invalidReasonCounts.set(reason, (invalidReasonCounts.get(reason) ?? 0) + 1);
    }
  }

  function selectFile(file: File | null): void {
    setError(null);
    setAnalysisResponse(null);

    if (!file) {
      setSelectedFile(null);
      return;
    }

    if (!file.name.toLowerCase().endsWith(".csv")) {
      setSelectedFile(null);
      setError("Select a file with the .csv extension.");
      return;
    }

    if (file.size > MAX_FILE_SIZE_BYTES) {
      setSelectedFile(null);
      setError("File is too large. Maximum upload size is 10 MB.");
      return;
    }

    setSelectedFile(file);
  }

  function handleFileChange(event: ChangeEvent<HTMLInputElement>): void {
    selectFile(event.target.files?.[0] ?? null);
    event.target.value = "";
  }

  function handleDragOver(event: DragEvent<HTMLLabelElement>): void {
    event.preventDefault();
    setIsDragging(true);
  }

  function handleDrop(event: DragEvent<HTMLLabelElement>): void {
    event.preventDefault();
    setIsDragging(false);
    selectFile(event.dataTransfer.files[0] ?? null);
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!selectedFile) {
      setError("Select a CSV file before starting the analysis.");
      return;
    }

    setIsUploading(true);
    setError(null);
    setAnalysisResponse(null);

    try {
      setAnalysisResponse(await analyzeIncidentFile(selectedFile));
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : "Unable to analyze the uploaded CSV file.");
    } finally {
      setIsUploading(false);
    }
  }

  const analysis = analysisResponse?.analysis;

  return (
    <main className="px-4 py-8 sm:px-6 sm:py-10">
      <div className="mx-auto w-full max-w-6xl space-y-6">
        <section className="overflow-hidden rounded-3xl border border-[color:var(--border-soft)] bg-[color:var(--surface)]/90 p-6 shadow-[0_24px_60px_-36px_rgba(37,99,235,0.45)] sm:p-8">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-[color:var(--flow-blue)]">Customer Experience</p>
          <h1 className="mt-2 font-brand-display text-3xl font-bold leading-tight text-[color:var(--text-strong)] sm:text-4xl">
            Incident File Analysis
          </h1>
          <p className="mt-3 max-w-3xl text-sm leading-relaxed text-[color:var(--text-muted)] sm:text-base">
            Upload an incident CSV to validate its records and review operational metrics without exposing the source file.
          </p>
        </section>

        <section className="rounded-2xl border border-[color:var(--border-soft)] bg-[color:var(--surface)] p-6 shadow-[0_16px_38px_-28px_rgba(37,99,235,0.35)] sm:p-8">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <h2 className="font-brand-display text-2xl font-semibold text-[color:var(--text-strong)]">Upload incident CSV</h2>
              <p className="mt-1 text-sm text-[color:var(--text-muted)]">CSV files only, up to 10 MB.</p>
            </div>
            {selectedFile ? <p className="text-sm font-medium text-[color:var(--flow-blue)]">{selectedFile.name}</p> : null}
          </div>

          <form className="mt-5" onSubmit={handleSubmit}>
            <label
              onDragOver={handleDragOver}
              onDragLeave={() => setIsDragging(false)}
              onDrop={handleDrop}
              className={`flex min-h-40 cursor-pointer flex-col items-center justify-center border-2 border-dashed px-5 text-center transition ${
                isDragging
                  ? "border-[color:var(--flow-blue)] bg-[color:var(--flow-blue)]/10"
                  : "border-[color:var(--border-soft)] bg-[color:var(--background)]/50 hover:border-[color:var(--flow-soft-blue)]"
              }`}
            >
              <span className="font-brand-display text-lg font-semibold text-[color:var(--text-strong)]">Drop your CSV here</span>
              <span className="mt-1 text-sm text-[color:var(--text-muted)]">or select a file from your device</span>
              <input className="sr-only" type="file" accept=".csv,text/csv" onChange={handleFileChange} />
            </label>

            <div className="mt-4 flex flex-wrap items-center gap-3">
              <button
                type="submit"
                disabled={!selectedFile || isUploading}
                className="rounded-xl bg-[color:var(--flow-blue)] px-4 py-2 text-sm font-semibold text-white transition hover:bg-[color:var(--flow-soft-blue)] disabled:cursor-not-allowed disabled:bg-[color:var(--flow-soft-blue)]/70"
              >
                {isUploading ? "Analyzing..." : "Analyze file"}
              </button>
              {selectedFile ? (
                <button
                  type="button"
                  onClick={() => selectFile(null)}
                  disabled={isUploading}
                  className="rounded-xl border border-[color:var(--border-soft)] px-4 py-2 text-sm font-medium text-[color:var(--text-muted)] transition hover:bg-[color:var(--flow-blue)]/5 disabled:cursor-not-allowed"
                >
                  Clear selection
                </button>
              ) : null}
            </div>
          </form>

          {error ? <StateMessage className="mt-4" tone="error">{error}</StateMessage> : null}
          {analysis ? (
            <StateMessage className="mt-4" tone="success">
              Analysis complete. {analysis.records_processed} records processed.
            </StateMessage>
          ) : null}
        </section>

        {analysis ? (
          <section className="space-y-6" aria-live="polite">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <h2 className="font-brand-display text-2xl font-semibold text-[color:var(--text-strong)]">Analysis summary</h2>
                <p className="mt-1 text-sm text-[color:var(--text-muted)]">Metrics include valid records only where applicable.</p>
              </div>
              <a
                href={incidentExportUrl(analysisResponse.export_url)}
                className="rounded-xl bg-[color:var(--flow-accent)] px-4 py-2 text-center text-sm font-semibold text-white transition hover:brightness-95"
              >
                Download results CSV
              </a>
            </div>

            <DashboardSection title="General metrics">
              <MetricCard label="Records processed" value={analysis.records_processed} />
              <MetricCard label="Valid records" value={analysis.records_valid} />
              <MetricCard label="Invalid records" value={analysis.records_invalid} />
            </DashboardSection>

            <DashboardSection title="Category breakdown">
              {INCIDENT_CATEGORIES.map((category) => (
                <MetricCard key={category} label={category} value={analysis.records_by_category[category]} />
              ))}
            </DashboardSection>

            <DashboardSection title="Status breakdown">
              {INCIDENT_STATUSES.map((status) => (
                <MetricCard key={status} label={status} value={analysis.records_by_status[status]} />
              ))}
            </DashboardSection>

            <DashboardSection title="Satisfaction index">
              <MetricCard label="Closed-case average" value={formatSatisfaction(analysis.closed_satisfaction_average)} />
              <MetricCard label="Scored closed cases" value={analysis.closed_satisfaction_count} />
            </DashboardSection>

            {analysis.invalid_records.length > 0 ? (
              <section className="rounded-2xl border border-[color:var(--border-soft)] bg-[color:var(--surface)] p-6 shadow-[0_16px_38px_-28px_rgba(37,99,235,0.35)] sm:p-8">
                <h2 className="font-brand-display text-2xl font-semibold text-[color:var(--text-strong)]">Data quality issues</h2>
                <p className="mt-1 text-sm text-[color:var(--text-muted)]">
                  {analysis.records_invalid} invalid records were excluded from category, status, and satisfaction metrics.
                </p>

                <dl className="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                  {Array.from(invalidReasonCounts.entries()).map(([reason, count]) => (
                    <div key={reason} className="border border-[color:var(--border-soft)] bg-[color:var(--background)]/50 p-4">
                      <dt className="text-sm text-[color:var(--text-muted)]">{reason}</dt>
                      <dd className="mt-2 font-brand-display text-2xl font-semibold text-[color:var(--flow-blue)]">{count}</dd>
                    </div>
                  ))}
                </dl>

                <div className="mt-6 overflow-x-auto border border-[color:var(--border-soft)]">
                  <table className="w-full min-w-[600px] text-left text-sm">
                    <thead className="bg-[color:var(--flow-blue)]/10 text-[color:var(--text-strong)]">
                      <tr>
                        <th scope="col" className="px-4 py-3 font-semibold">Row</th>
                        <th scope="col" className="px-4 py-3 font-semibold">Incident ID</th>
                        <th scope="col" className="px-4 py-3 font-semibold">Validation issues</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-[color:var(--border-soft)]">
                      {analysis.invalid_records.map((record) => (
                        <tr key={`${record.row_number}-${record.incident_id}`}>
                          <td className="px-4 py-3 text-[color:var(--text-strong)]">{record.row_number}</td>
                          <td className="px-4 py-3 text-[color:var(--text-strong)]">{record.incident_id || "Missing incident ID"}</td>
                          <td className="px-4 py-3 text-[color:var(--text-muted)]">{record.reasons.join("; ")}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            ) : null}
          </section>
        ) : null}
      </div>
    </main>
  );
}

function DashboardSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h2 className="mb-3 font-brand-display text-xl font-semibold text-[color:var(--text-strong)]">{title}</h2>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{children}</div>
    </section>
  );
}

function MetricCard({ label, value }: { label: string; value: number | string }) {
  return (
    <article className="border border-[color:var(--border-soft)] bg-[color:var(--surface)] p-5 shadow-[0_12px_28px_-24px_rgba(37,99,235,0.55)]">
      <p className="text-xs font-semibold uppercase tracking-[0.12em] text-[color:var(--text-muted)]">{label}</p>
      <p className="mt-2 font-brand-display text-2xl font-bold text-[color:var(--flow-blue)]">{value}</p>
    </article>
  );
}