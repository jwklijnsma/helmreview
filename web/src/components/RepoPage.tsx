import { useCallback, useEffect, useState } from "react";
import { api, enc } from "../api";
import { go, useApp } from "../store";
import { isActive, SEVS, type FixResult, type Job, type RepoDetail, type Report } from "../types";
import { ago, dur, exitText, jobTitle, plural, shortPath, size, store, stored } from "../util";
import { Downloads, JobLog, Progress } from "./JobsPanel";
import { clearFileCache, DownloadsTab, FindingsTab, FixesTab, NotesTab } from "./Tabs";

type Opts = { scan: Record<string, any>; fix: Record<string, any> };
const optsFor = (id: string): Opts => {
  const o = stored<Partial<Opts>>("opts:" + id, {});
  return { scan: { diff_scope: "lines", ...(o.scan || {}) }, fix: { ...(o.fix || {}) } };
};

function Options({ id, model }: { id: string; model: string }) {
  const [o, setO] = useState<Opts>(() => optsFor(id));
  const [open, setOpen] = useState(() => stored("opts-open", false));
  useEffect(() => setO(optsFor(id)), [id]);
  const set = (group: keyof Opts, key: string, value: unknown) => {
    const n = { ...o, [group]: { ...o[group], [key]: value } };
    setO(n);
    store("opts:" + id, n);
  };
  const text = (group: keyof Opts, key: string, label: string, placeholder = "", area = false) => (
    <div className="field">
      <label htmlFor={`o-${group}-${key}`}>{label}</label>
      {area ? (
        <textarea id={`o-${group}-${key}`} value={o[group][key] || ""} onChange={(e) => set(group, key, e.target.value)} />
      ) : (
        <input id={`o-${group}-${key}`} type="text" placeholder={placeholder} value={o[group][key] || ""} onChange={(e) => set(group, key, e.target.value)} />
      )}
    </div>
  );
  const select = (group: keyof Opts, key: string, label: string, choices: [string, string][]) => (
    <div className="field">
      <label htmlFor={`o-${group}-${key}`}>{label}</label>
      <select id={`o-${group}-${key}`} value={o[group][key] || ""} onChange={(e) => set(group, key, e.target.value)}>
        {choices.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
      </select>
    </div>
  );
  const check = (group: keyof Opts, key: string, label: string) => (
    <label className="check"><input type="checkbox" checked={!!o[group][key]} onChange={(e) => set(group, key, e.target.checked)} />{label}</label>
  );
  const sevChoices: [string, string][] = [["", "default"], ...[...SEVS].reverse().map((s): [string, string] => [s, s])];
  const active = [o.scan.diff && "diff vs " + o.scan.diff, o.scan.only && "paths limited", o.scan.no_llm && "no model", o.scan.model].filter(Boolean);
  return (
    <details className="card options" open={open} onToggle={(e) => { const v = (e.target as HTMLDetailsElement).open; setOpen(v); store("opts-open", v); }}>
      <summary>Options <span className="hint">{active.length ? "· " + active.join(" · ") : "· defaults from .helmreview.yml"}</span></summary>
      <div className="opt-grid">
        <fieldset>
          <legend>Scope</legend>
          {text("scan", "diff", "Only changes vs git ref", "e.g. origin/main")}
          {select("scan", "diff_scope", "Report on", [["lines", "changed lines"], ["files", "whole changed files"]])}
          {text("scan", "only", "Only these charts / paths (one per line)", "", true)}
        </fieldset>
        <fieldset>
          <legend>Scan</legend>
          {text("scan", "model", "Model override", model)}
          {select("scan", "min_severity", "Hide findings below", sevChoices)}
          {check("scan", "no_llm", "Skip the model (fast: lint + HR checks only)")}
          {check("scan", "strict", "helm lint --strict")}
          {check("scan", "no_lint", "Skip helm lint")}
          {check("scan", "no_static", "Skip HR checks")}
          {check("scan", "no_cache", "Ignore the cache, ask the model again")}
        </fieldset>
        <fieldset>
          <legend>Fix</legend>
          {select("fix", "min_severity", "Model fixes from severity", sevChoices)}
          {text("fix", "min_confidence", "Model fixes from confidence", "0.8")}
          {check("fix", "allow_dirty", "Allow uncommitted changes")}
          {check("fix", "no_llm", "Skip model fixes")}
          {check("fix", "no_static_fix", "Don't list HR findings to fix by hand")}
        </fieldset>
      </div>
    </details>
  );
}

/** This repo's newest job: live progress while it runs, downloads when it is done. */
function RepoJob({ job }: { job: Job }) {
  const { cancelJob, openJob } = useApp();
  const [showLog, setShowLog] = useState(false);
  const active = isActive(job);
  useEffect(() => setShowLog(false), [job.id]);
  const label = { queued: "Waiting", running: job.action === "scan" ? "Scanning" : "Fixing", done: "Finished", failed: "Failed", cancelled: "Cancelled" }[job.status];
  return (
    <div className={"card job-card " + job.status}>
      <div className="card-head">
        <div className="job-status">
          <span className={"dot " + (job.status === "running" ? "run" : job.status === "queued" ? "wait" : job.status === "done" ? "ok" : "bad")} />
          <strong>{jobTitle(job)} · {label}</strong>
          <span className="muted">{job.started ? dur(job.seconds) : ""}{!active && job.ended ? ` · ${ago(job.ended)}` : ""}</span>
        </div>
        <div className="actions">
          <button className="btn small" onClick={() => setShowLog(!showLog)}>{showLog ? "Hide log" : "Log"}</button>
          {active && <button className="btn small danger" onClick={() => cancelJob(job.id)}>{job.status === "queued" ? "Remove from queue" : "Cancel"}</button>}
          <button className="btn small" onClick={() => openJob(job.id)}>All jobs</button>
        </div>
      </div>
      <div className="card-body">
        {active && <Progress job={job} />}
        <div className="progress-meta"><span>{exitText(job)}</span>{active && job.status === "running" && <span>runs in the background · you can leave this page</span>}</div>
        <Downloads job={job} />
        {showLog && <JobLog job={job} />}
      </div>
    </div>
  );
}

export function RepoPage({ repoId, tab }: { repoId: string; tab: string }) {
  const { jobs, repoVersion, settingsVersion, startJob, handleError, refreshRepos, toast } = useApp();
  const [detail, setDetail] = useState<RepoDetail | null>(null);
  const [report, setReport] = useState<{ report: Report | null; time: number | null }>({ report: null, time: null });
  const [fix, setFix] = useState<FixResult | null>(null);
  const [error, setError] = useState("");
  const version = repoVersion[repoId] || 0;

  const load = useCallback(async () => {
    try {
      const [d, r, f] = await Promise.all([
        api<RepoDetail>(`repos/${enc(repoId)}`),
        api<{ report: Report | null; time?: number }>(`repos/${enc(repoId)}/report`),
        api<FixResult>(`repos/${enc(repoId)}/fix`),
      ]);
      setDetail(d);
      setReport({ report: r.report, time: r.time ?? null });
      setFix(f);
      setError("");
    } catch (err) {
      if ((err as { status?: number }).status === 404) setError("This repository is gone (deleted, or the server was restarted with other --root folders).");
      else handleError(err);
    }
  }, [repoId, handleError]);

  useEffect(() => {
    setDetail(null);
    clearFileCache();
  }, [repoId]);
  useEffect(() => {
    load();
    clearFileCache();
  }, [load, version, settingsVersion]);

  if (error) return <div className="empty"><h3>Could not open this repository</h3><p>{error}</p></div>;
  if (!detail) return <div className="empty"><p>Loading…</p></div>;

  const repoJobs = jobs.filter((j) => j.repo === repoId);
  const latest = repoJobs[0];
  const busy = repoJobs.some(isActive);
  const up = detail.upload;
  const o = optsFor(repoId);
  const only = String(o.scan.only || "").split("\n").map((s) => s.trim()).filter(Boolean);

  const scan = () => startJob(repoId, "scan", { ...o.scan, only });
  const fixJob = (dryRun: boolean) => {
    const body: Record<string, unknown> = { ...o.fix, only, dry_run: dryRun || undefined };
    if (o.scan.model && !body.model) body.model = o.scan.model;
    if (!dryRun) {
      if (up) body.allow_dirty = true; // fixes on top of earlier fixes on the server copy
      else if (detail.dirty && !body.allow_dirty) {
        toast({ text: "This repo has uncommitted changes. Commit or stash them, or tick “Allow uncommitted changes” in Options.", kind: "bad" });
        return;
      } else if (!confirm(`Apply fixes to ${detail.name}?\n\nChanges are written to the working tree and never committed. Review them with git diff; undo with git checkout -- .`)) return;
    }
    startJob(repoId, "fix", body);
  };
  const del = async () => {
    if (!confirm(`Delete the uploaded ${detail.name} and its reports from this server?`)) return;
    try {
      await api(`repos/${enc(repoId)}/delete`, { method: "POST" });
      toast({ text: `${detail.name} deleted.` });
      await refreshRepos();
      location.hash = "";
    } catch (err) {
      handleError(err);
    }
  };

  const nF = report.report ? report.report.findings.length : null;
  const nNotes = report.report ? Object.keys(report.report.summaries || {}).length + (report.report.errors || []).length : null;
  const tabs: [string, string, number | null][] = [["findings", "Findings", nF], ["fixes", "Fixes", null], ["notes", "Notes", nNotes], ["downloads", "Downloads", detail.artifacts.length || null]];

  return (
    <div>
      <div className="repo-head">
        <div className="repo-title">
          <h1>{detail.name}</h1>
          {up ? (
            <div className="repo-sub">
              <span className="zip-badge">ZIP</span>
              <span title={up.filename}>{up.filename}</span>
              <span>{up.files} files · {size(up.bytes)}</span>
              <span>uploaded {ago(up.uploaded)}</span>
              {detail.dirty && <span className="badge dirty" title={detail.changed_files.join("\n")}>{plural(detail.changed_files.length, "fixed file")}</span>}
              <span>model <code>{detail.config.model}</code></span>
            </div>
          ) : (
            <div className="repo-sub">
              {detail.branch && <span>⎇ <code>{detail.branch}</code></span>}
              {detail.last_commit && <span className="mono">{detail.last_commit}</span>}
              {detail.dirty === null ? <span>not a git repo</span> : (
                <span className={"badge " + (detail.dirty ? "dirty" : "clean")} title={detail.changed_files.join("\n")}>{detail.dirty ? `${detail.changed_files.length} uncommitted` : "clean"}</span>
              )}
              <span>model <code>{detail.config.model}</code></span>
              <span className="path" title={detail.path}>{shortPath(detail.path)}</span>
            </div>
          )}
        </div>
        <div className="actions">
          <button className="btn primary" onClick={scan}>Scan</button>
          <button className="btn" onClick={() => fixJob(true)} title="Write fix.patch only, change nothing">Preview fixes</button>
          {up ? (
            <button className="btn" onClick={() => fixJob(false)} title="Fix the server's copy in the background, then download it">Fix &amp; download</button>
          ) : (
            <button className="btn" onClick={() => fixJob(false)} title="Write fixes to the working tree (never committed)">Apply fixes</button>
          )}
          {up && <a className="btn" href={`/api/repos/${enc(repoId)}/download`} download>{detail.dirty ? "Download fixed ZIP" : "Download ZIP"}</a>}
          {up && <button className="btn danger" disabled={busy} onClick={del}>Delete</button>}
        </div>
      </div>
      {detail.config_warnings.length > 0 && <div className="callout warn">{detail.config_warnings.map((w) => <div key={w}>{w}</div>)}</div>}
      <Options id={repoId} model={detail.config.model} />
      {latest && <RepoJob job={latest} />}
      {repoJobs.filter(isActive).length > 1 && <p className="muted small">{plural(repoJobs.filter(isActive).length - 1, "more job")} for this repo waiting. See all jobs in the top bar.</p>}
      <div className="tabs" role="tablist">
        {tabs.map(([k, l, n]) => (
          <button key={k} role="tab" aria-selected={tab === k} onClick={() => go(repoId, k)}>
            {l}{n !== null && <span className="count">{n}</span>}
          </button>
        ))}
      </div>
      {tab === "fixes" ? <FixesTab detail={detail} fix={fix} hasReport={!!report.report} />
        : tab === "notes" ? <NotesTab report={report.report} />
          : tab === "downloads" ? <DownloadsTab detail={detail} />
            : <FindingsTab key={repoId} repoId={repoId} report={report.report} reportTime={report.time} />}
    </div>
  );
}
