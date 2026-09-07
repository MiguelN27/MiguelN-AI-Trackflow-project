import type { IncidentAnalysisResponse } from "@/types/incident";

type ErrorResponse = {
  error?: unknown;
};

function isIncidentAnalysisResponse(payload: unknown): payload is IncidentAnalysisResponse {
  return (
    typeof payload === "object" &&
    payload !== null &&
    "analysis" in payload &&
    "export_url" in payload &&
    typeof payload.export_url === "string"
  );
}

async function responseError(response: Response): Promise<string> {
  const payload = (await response.json().catch(() => null)) as ErrorResponse | null;

  if (typeof payload?.error === "string") {
    return payload.error;
  }

  return `Request failed with status ${response.status}`;
}

export async function analyzeIncidentFile(file: File): Promise<IncidentAnalysisResponse> {
  const formData = new FormData();
  formData.append("file", file);

  const response = await fetch("/api/incidents/analyze", {
    method: "POST",
    headers: {
      Accept: "application/json",
    },
    body: formData,
  });

  if (!response.ok) {
    throw new Error(await responseError(response));
  }

  const payload: unknown = await response.json();
  if (!isIncidentAnalysisResponse(payload)) {
    throw new Error("The incident analysis API returned an invalid response.");
  }

  return payload;
}

export function incidentExportUrl(exportUrl: string): string {
  return exportUrl.startsWith("/") ? exportUrl : `/${exportUrl}`;
}