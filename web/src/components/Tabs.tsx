import { useEffect, useMemo, useState } from "react";
import { api, enc } from "../api";
import { parseDiff } from "../diff";
import { Markdown } from "../markdown";
import { SEVS, type FixResult, type Finding, type RepoDetail, type Report, type Severity } from "../types";
import { ago, dur, plural, size } from "../util";

const SOURCES: [string, string][] = [["all", "All"], ["static", "HR checks"], ["helm-lint", "helm lint"], ["llm", "Model"]];

// Files are fetched once per page and shared by every expanded finding.
const fileCache = new Map<string, Promise<string>>();
function fileContent(repoId: string, path: string): Promise<string> {
  const key = repoId + "|" + path;
  if (!fileCache.has(key)) {
    const p = api<{ content: string }>(`repos/${enc(repoId)}/file?path=${enc(path)}`).then((d) => d.content);
    p.catch(() => fileCache.delete(key));
    fileCache.set(key, p);
  }
  return fileCache.get(key)!;
}
export function clearFileCache() {
  fileCache.clear();
}

function Snippet({ repoId, f }: { repoId: string; f: Finding }) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    fileContent(repoId, f.file).then(setText, (e) => setError(e.message));
  }, [repoId, f.file]);
  if (error) return <div className="code"><div className="code-msg">Could not load the file: {error}</div></div>;
  if (text === null || !f.line) return <div className="code"><div className="code-msg">Loading code…</div></div>;
  const lines = text.split("\n");
  const line = f.line;
  const hiEnd = f.end_line && f.end_line >= line ? Math.min(f.end_line, line + 40) : line;
  const start = Math.max(1, line - 4);
  const end = Math.min(lines.length, Math.max(hiEnd, line) + 4, start + 80);
  const rows = [];
  for (let n = start; n <= end; n++) {
    rows.push(
      <tr key={n} className={n >= line && n <= hiEnd ? "hl" : undefined}>
        <td className="ln">{n}</td>
        <td>{lines[n - 1] ?? ""}</td>
      </tr>,
    );
  }
  return <div className="code"><table><tbody>{rows}</tbody></table></div>;
}

function FindingRow({ repoId, f, showFile }: { repoId: string; f: Finding; showFile: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="finding">
      <button aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className={"sev sev-" + f.severity}>{f.severity}</span>
        <span className="lineno">{f.line ? "L" + f.line : "—"}</span>
        <span className="f-main">
          <span className="f-rule">{f.rule}</span>
          {showFile && <span className="f-rule">{f.file + (f.line ? ":" + f.line : "")}</span>}
          <span className="f-src">{f.source === "static" ? "HR" : f.source === "llm" ? "model" : f.source}</span>
          <span className="f-msg">{f.message}</span>
        </span>
      </button>
      {open && (
        <div className="f-detail">
          {f.suggestion && <div className="sugg"><b>Suggested fix</b>{f.suggestion}</div>}
          <div className="facts">
            <span>category: {f.category || "general"}</span>
            {f.confidence != null && <span>confidence: {Math.round(f.confidence * 100)}%</span>}
            <span className="mono">{f.file + (f.line ? ":" + f.line : "")}</span>
          </div>
          {f.line ? <Snippet repoId={repoId} f={f} /> : null}
        </div>
      )}
    </div>
  );
}

export function FindingsTab({ repoId, report, reportTime }: { repoId: string; report: Report | null; reportTime: number | null }) {
  const [sev, setSev] = useState<Set<Severity>>(() => new Set(SEVS));
  const [source, setSource] = useState("all");
  const [q, setQ] = useState("");
  const [group, setGroup] = useState<"file" | "rule">("file");

  const groups = useMemo(() => {
    if (!report) return [];
    const needle = q.trim().toLowerCase();
    const items = report.findings.filter(
      (x) => sev.has(x.severity) && (source === "all" || x.source === source) && (!needle || `${x.rule} ${x.file} ${x.message} ${x.category}`.toLowerCase().includes(needle)),
    );
    const m = new Map<string, Finding[]>();
    for (const x of items) {
      const key = group === "rule" ? x.rule : x.file;
      if (!m.has(key)) m.set(key, []);
      m.get(key)!.push(x);
    }
    const rank = (arr: Finding[]) => Math.min(...arr.map((x) => SEVS.indexOf(x.severity)));
    return [...m.entries()].sort((a, b) => rank(a[1]) - rank(b[1]) || a[0].localeCompare(b[0]));
  }, [report, sev, source, q, group]);

  if (!report) {
    return (
      <div className="empty">
        <h3>No report yet</h3>
        <p>Run a scan to review this repository. Tip: tick “Skip the model” in Options for a quick first pass.</p>
      </div>
    );
  }
  const counts = report.counts || {};
  const toggle = (s: Severity) => {
    const n = new Set(sev);
    n.has(s) ? n.delete(s) : n.add(s);
    setSev(n);
  };
  return (
    <div>
      <div className="summary-row">
        {SEVS.map((s) => (
          <button key={s} className="sev-chip" aria-pressed={sev.has(s)} title="Click to toggle, double-click to show only this severity" onClick={() => toggle(s)} onDoubleClick={() => setSev(new Set([s]))}>
            <span className="n">{counts[s] || 0}</span>
            <span className="l"><span className="sw" style={{ background: `var(--sev-${s})` }} />{s}</span>
          </button>
        ))}
        <div className="meta-chip">
          <div>{plural((report.units || []).length, "unit")} · scanned {ago(reportTime)}</div>
          <div>{[report.model ? "model " + report.model : "no model", report.stats?.seconds != null ? dur(report.stats.seconds) : null].filter(Boolean).join(" · ")}</div>
        </div>
      </div>
      <div className="filters">
        <input type="search" placeholder="Search rule, file or message…" value={q} onChange={(e) => setQ(e.target.value)} />
        <div className="seg" role="group" aria-label="Source">
          {SOURCES.map(([k, l]) => <button key={k} aria-pressed={source === k} onClick={() => setSource(k)}>{l}</button>)}
        </div>
        <div className="seg" role="group" aria-label="Group by">
          {(["file", "rule"] as const).map((k) => <button key={k} aria-pressed={group === k} onClick={() => setGroup(k)}>{k === "file" ? "By file" : "By rule"}</button>)}
        </div>
      </div>
      {!groups.length ? (
        <div className="empty">{report.findings.length ? <p>No findings match these filters.</p> : <><h3>No findings</h3><p>Nothing to fix here.</p></>}</div>
      ) : (
        groups.slice(0, 400).map(([key, arr]) => (
          <div className="file-group" key={key}>
            <div className="file-head">{key}<span className="n">{plural(arr.length, "finding")}</span></div>
            {arr.map((x) => <FindingRow key={x.fingerprint} repoId={repoId} f={x} showFile={group === "rule"} />)}
          </div>
        ))
      )}
    </div>
  );
}

export function FixesTab({ detail, fix, hasReport }: { detail: RepoDetail; fix: FixResult | null; hasReport: boolean }) {
  if (!fix || (!fix.patch && !fix.report_md)) {
    return (
      <div className="empty">
        <h3>No fix run yet</h3>
        <p>{hasReport ? "“Preview fixes” shows what helmreview would change without touching anything." : "Scan first, or start a fix straight away: it scans when there is no report yet."}</p>
      </div>
    );
  }
  const files = parseDiff(fix.patch || "");
  return (
    <div>
      <div className="summary-row"><div className="meta-chip left">Last fix run {ago(fix.time)}</div></div>
      {detail.upload && (
        <div className="callout">
          Fixes on an upload change only this server's copy (ignore the git hints in the report).{" "}
          <a href={`/api/repos/${enc(detail.id)}/download`} download>Download the fixed ZIP</a> to take them with you.
        </div>
      )}
      {fix.report_md && <div className="card"><div className="card-body md"><Markdown src={fix.report_md} /></div></div>}
      {files.length ? (
        <>
          <h3 className="section-title">Diff · {plural(files.length, "file")}</h3>
          {files.map((f) => (
            <div className="diff-file" key={f.name}>
              <h4>{f.name}<span className="stat"><span className="plus">+{f.add}</span> <span className="minus">−{f.del}</span></span></h4>
              <div className="diff-body">
                {f.lines.map((l, i) => <div key={i} className={l.startsWith("@@") ? "hunk" : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : undefined}>{l || " "}</div>)}
              </div>
            </div>
          ))}
        </>
      ) : (
        <p className="empty">The last fix run changed no files.</p>
      )}
    </div>
  );
}

export function NotesTab({ report }: { report: Report | null }) {
  if (!report) return <div className="empty"><p>Run a scan first.</p></div>;
  const sums = Object.entries(report.summaries || {}).sort((a, b) => a[0].localeCompare(b[0]));
  const kinds: Record<string, number> = {};
  for (const u of report.units || []) kinds[u.kind] = (kinds[u.kind] || 0) + 1;
  return (
    <div>
      <div className="card">
        <div className="card-head"><h3>Reviewer notes per unit</h3></div>
        <div className="card-body">
          {sums.length ? (
            <ul className="notes">{sums.map(([k, v]) => <li key={k}><b>{k}</b><div>{v}</div></li>)}</ul>
          ) : (
            <p className="muted">{report.model ? "The model left no notes." : "No model review in the last scan."}</p>
          )}
        </div>
      </div>
      {(report.errors || []).length > 0 && (
        <div className="card">
          <div className="card-head"><h3>Problems during the scan</h3></div>
          <div className="card-body"><ul className="errors">{report.errors.map((e, i) => <li key={i}>{e}</li>)}</ul></div>
        </div>
      )}
      <div className="card">
        <div className="card-head"><h3>Scanned</h3></div>
        <div className="card-body">
          <p className="tight">{Object.entries(kinds).map(([k, n]) => plural(n, k)).join(" · ") || "nothing"}</p>
          <ul className="notes">{(report.units || []).slice(0, 300).map((u) => <li key={u.name}><b>{u.name}</b> <span className="muted">{u.kind} · {plural(u.files.length, "file")}</span></li>)}</ul>
        </div>
      </div>
    </div>
  );
}

export function DownloadsTab({ detail }: { detail: RepoDetail }) {
  const list = detail.artifacts || [];
  if (!list.length) return <div className="empty"><h3>Nothing to download yet</h3><p>Scan or fix this repository; its reports and patches show up here.</p></div>;
  return (
    <div className="card">
      <ul className="dl-list">
        {list.map((a) => (
          <li key={a.name}>
            <div>
              <strong>{a.label}</strong>
              <div className="muted small">{a.name === "fixed.zip" ? `${detail.upload?.files ?? ""} files${detail.upload ? " · " + size(detail.upload.bytes) : ""}${detail.dirty ? " · includes applied fixes" : ""}` : `${a.name} · ${ago(a.time)}`}</div>
            </div>
            <a className="btn small" href={a.url} download>↓ Download</a>
          </li>
        ))}
      </ul>
    </div>
  );
}
