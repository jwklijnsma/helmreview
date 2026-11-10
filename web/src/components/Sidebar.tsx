import { useRef, useState } from "react";
import { go, useApp } from "../store";
import { SEVS, type Counts, type Repo } from "../types";
import { ago, plural, total } from "../util";

export function SevBar({ counts }: { counts: Counts | null }) {
  if (!counts) return null;
  const t = total(counts);
  if (!t) return <div className="sevbar"><span style={{ width: "100%", background: "var(--ok)" }} /></div>;
  return (
    <div className="sevbar" title={SEVS.map((s) => `${s}: ${counts[s] || 0}`).join(" · ")}>
      {SEVS.filter((s) => counts[s]).map((s) => <span key={s} style={{ width: `${(100 * counts[s]!) / t}%`, background: `var(--sev-${s})` }} />)}
    </div>
  );
}

function RepoItem({ r, current, tab }: { r: Repo; current: boolean; tab: string }) {
  const { jobs } = useApp();
  const job = jobs.find((j) => j.repo === r.id && (j.status === "running" || j.status === "queued"));
  const n = total(r.counts);
  return (
    <li>
      <button aria-current={current} onClick={() => go(r.id, tab)}>
        <div className="repo-name">
          {r.name}
          {r.upload && <span className="zip-badge">ZIP</span>}
          {job && <span className={"dot " + (job.status === "running" ? "run" : "wait")} title={job.status === "running" ? `${job.action} running` : `${job.action} waiting`} />}
        </div>
        <div className="repo-meta">
          {r.upload ? `uploaded ${ago(r.upload.uploaded)} · ` : r.branch ? r.branch + " · " : ""}
          {r.report_time ? plural(n, "finding") + (r.upload ? "" : " · " + ago(r.report_time)) : "not scanned yet"}
        </div>
        <SevBar counts={r.counts} />
      </button>
    </li>
  );
}

export function UploadBox({ busy, progress, onFile }: { busy: boolean; progress: number | null; onFile: (f: File) => void }) {
  const { status } = useApp();
  const input = useRef<HTMLInputElement>(null);
  if (!status?.uploads.enabled) return null;
  return (
    <div className="upload-box">
      <button className="btn primary upload-btn" type="button" disabled={busy} onClick={() => input.current?.click()}>
        Upload ZIP for review
      </button>
      <input
        ref={input}
        type="file"
        accept=".zip,application/zip"
        hidden
        onChange={(e) => {
          const f = e.target.files?.[0];
          e.target.value = "";
          if (f) onFile(f);
        }}
      />
      {progress === null ? (
        <div className="upload-hint">or drop it on the page · max {status.uploads.max_mb} MB</div>
      ) : (
        <>
          <div className="progress"><div style={{ width: `${progress}%` }} /></div>
          <div className="upload-hint" aria-live="polite">{progress < 100 ? `Uploading… ${progress}%` : "Unpacking and checking…"}</div>
        </>
      )}
    </div>
  );
}

export function Sidebar({ repoId, tab, upload }: { repoId: string | null; tab: string; upload: { busy: boolean; progress: number | null; onFile: (f: File) => void } }) {
  const { repos, status } = useApp();
  const [q, setQ] = useState("");
  const shown = repos.filter((r) => !q || r.name.toLowerCase().includes(q.toLowerCase()));
  const local = shown.filter((r) => !r.upload);
  const uploads = shown.filter((r) => r.upload);
  return (
    <aside className="sidebar">
      <UploadBox {...upload} />
      <h2>Repositories</h2>
      <input className="search" type="search" placeholder="Filter…" aria-label="Filter repositories" value={q} onChange={(e) => setQ(e.target.value)} />
      <ul className="repo-list">
        {!repos.length && (
          <li className="empty-roots">
            No Helm charts or manifests found under {(status?.roots || []).join(", ")}.{" "}
            {status?.uploads.enabled ? "Upload a ZIP above, or restart the server with --root pointing at a repo or a folder of repos." : "Start the server with --root pointing at a repo or a folder of repos."}
          </li>
        )}
        {local.map((r) => <RepoItem key={r.id} r={r} current={r.id === repoId} tab={tab} />)}
        {uploads.length > 0 && <li className="list-head">Uploads</li>}
        {uploads.map((r) => <RepoItem key={r.id} r={r} current={r.id === repoId} tab={tab} />)}
      </ul>
    </aside>
  );
}
