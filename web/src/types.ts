export const SEVS = ["critical", "high", "medium", "low", "info"] as const;
export type Severity = (typeof SEVS)[number];
export type Counts = Partial<Record<Severity, number>>;

export interface UploadInfo {
  filename: string;
  uploaded: number;
  files: number;
  bytes: number;
  zip_bytes: number;
  skipped: number;
  git: boolean;
}

export interface Repo {
  id: string;
  name: string;
  path: string;
  branch: string | null;
  report_time: number | null;
  fix_time: number | null;
  counts: Counts | null;
  model: string | null;
  upload: UploadInfo | null;
}

export interface Artifact {
  name: string;
  label: string;
  url: string;
  time?: number;
  primary?: boolean;
}

export interface RepoDetail extends Repo {
  dirty: boolean | null;
  changed_files: string[];
  config: { model: string; language: string; fail_on: string; min_severity: string; lint_strict: boolean; workers: number };
  config_warnings: string[];
  last_commit: string | null;
  artifacts: Artifact[];
}

export interface Finding {
  rule: string;
  severity: Severity;
  file: string;
  line: number | null;
  end_line?: number | null;
  message: string;
  suggestion?: string | null;
  category?: string;
  source: "static" | "helm-lint" | "llm" | string;
  confidence?: number | null;
  fingerprint: string;
}

export interface Report {
  findings: Finding[];
  counts: Counts;
  model: string | null;
  units: { name: string; kind: string; path?: string; files: string[] }[];
  summaries: Record<string, string>;
  errors: string[];
  stats?: { seconds?: number };
}

export interface FixResult {
  patch: string | null;
  report_md: string | null;
  time: number | null;
}

export type JobStatus = "queued" | "running" | "done" | "failed" | "cancelled";

export interface Job {
  id: string;
  repo: string;
  repo_name: string;
  action: "scan" | "fix";
  options: Record<string, unknown>;
  status: JobStatus;
  dry_run: boolean;
  exit_code: number | null;
  created: number;
  started: number | null;
  ended: number | null;
  seconds: number;
  downloads: Artifact[];
  position: number | null;
  progress?: { total: number; done: number; file: string | null } | null;
  lines?: string[];
  next?: number;
}

export interface Status {
  version: string;
  ollama_host: string;
  model: string;
  /** "ui" when picked in the web UI, "config" when it comes from the config/env. */
  model_source: "ui" | "config";
  default_model: string;
  ollama: string | null;
  ollama_error?: string;
  model_available: boolean | null;
  models: string[];
  helm: string | null;
  roots: string[];
  auth: boolean;
  uploads: { enabled: boolean; max_mb?: number; keep_days?: number };
}

export const isActive = (j: Job) => j.status === "queued" || j.status === "running";
