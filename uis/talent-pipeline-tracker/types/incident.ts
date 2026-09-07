export const INCIDENT_CATEGORIES = [
  "DAMAGE",
  "DELAYED_DELIVERY",
  "LOST_PARCEL",
  "RETURN_REQUEST",
  "WRONG_ADDRESS",
] as const;

export const INCIDENT_STATUSES = ["OPEN", "CLOSED", "DISCARDED"] as const;

export type IncidentCategory = (typeof INCIDENT_CATEGORIES)[number];
export type IncidentStatus = (typeof INCIDENT_STATUSES)[number];

export type InvalidIncidentRecord = {
  row_number: number;
  incident_id: string;
  reasons: string[];
};

export type IncidentAnalysis = {
  records_processed: number;
  records_valid: number;
  records_invalid: number;
  records_by_category: Record<IncidentCategory, number>;
  records_by_status: Record<IncidentStatus, number>;
  closed_satisfaction_average: number | null;
  closed_satisfaction_count: number;
  invalid_records: InvalidIncidentRecord[];
};

export type IncidentAnalysisResponse = {
  analysis: IncidentAnalysis;
  export_url: string;
};