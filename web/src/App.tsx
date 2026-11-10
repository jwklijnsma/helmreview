import { useCallback, useEffect, useRef, useState } from "react";
import { AuthError, uploadZip } from "./api";
import { Login, Toasts, TopBar } from "./components/Chrome";
import { JobsPanel } from "./components/JobsPanel";
import { RepoPage } from "./components/RepoPage";
import { Sidebar } from "./components/Sidebar";
import { AppProvider, go, useApp, useRoute } from "./store";
import { size } from "./util";

function Welcome() {
  const { repos, status } = useApp();
  const n = repos.length;
  return (
    <div className="empty">
      <h3>{n ? "Pick a repository" : "No repositories yet"}</h3>
      <p>
        {n ? `${n} ${n === 1 ? "repository" : "repositories"} with Helm charts or manifests found. Choose one on the left to scan it or browse its last report.` : "Restart helmreview serve with --root pointing at a chart repo or a folder that contains them."}
        {status?.uploads.enabled ? " Or upload a ZIP: it is scanned in the background straight away." : ""}
      </p>
    </div>
  );
}

function useUploader() {
  const { status, toast, refreshRepos, startJob, setAuthed, handleError } = useApp();
  const [progress, setProgress] = useState<number | null>(null);
  const onFile = useCallback(
    async (file: File) => {
      if (!status?.uploads.enabled) return toast({ text: "Uploads are switched off on this server.", kind: "bad" });
      if (!/\.zip$/i.test(file.name)) return toast({ text: "Choose a .zip file of a Helm chart or chart repository.", kind: "bad" });
      const max = (status.uploads.max_mb || 0) * 1048576;
      if (max && file.size > max) return toast({ text: `${file.name} is ${size(file.size)}; the limit is ${status.uploads.max_mb} MB.`, kind: "bad" });
      setProgress(0);
      try {
        const up = await uploadZip(file, setProgress);
        await refreshRepos();
        go(up.id, "findings");
        // Uploading is asking for a review: queue the scan right away.
        startJob(up.id, "scan", {}, `${up.filename}: ${up.files} files unpacked${up.skipped ? ` (${up.skipped} skipped)` : ""}.`);
      } catch (err) {
        if (err instanceof AuthError) setAuthed(false);
        else handleError(new Error("Upload refused: " + (err as Error).message));
      } finally {
        setProgress(null);
      }
    },
    [status, toast, refreshRepos, startJob, setAuthed, handleError],
  );
  return { busy: progress !== null, progress, onFile };
}

function DropZone({ onFile }: { onFile: (f: File) => void }) {
  const { status } = useApp();
  const [show, setShow] = useState(false);
  const depth = useRef(0);
  useEffect(() => {
    if (!status?.uploads.enabled) return;
    const hasFiles = (e: DragEvent) => [...(e.dataTransfer?.types || [])].includes("Files");
    const enter = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth.current++;
      setShow(true);
    };
    const over = (e: DragEvent) => hasFiles(e) && e.preventDefault();
    const leave = () => {
      if (--depth.current <= 0) {
        depth.current = 0;
        setShow(false);
      }
    };
    const drop = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth.current = 0;
      setShow(false);
      const f = e.dataTransfer?.files?.[0];
      if (f) onFile(f);
    };
    window.addEventListener("dragenter", enter);
    window.addEventListener("dragover", over);
    window.addEventListener("dragleave", leave);
    window.addEventListener("drop", drop);
    return () => {
      window.removeEventListener("dragenter", enter);
      window.removeEventListener("dragover", over);
      window.removeEventListener("dragleave", leave);
      window.removeEventListener("drop", drop);
    };
  }, [status, onFile]);
  return show ? <div className="dropzone"><div>Drop the ZIP to upload it for review</div></div> : null;
}

function Shell() {
  const { authed, repos } = useApp();
  const { repoId, tab } = useRoute();
  const upload = useUploader();
  useEffect(() => {
    if (authed && !repoId && repos.length === 1 && !location.hash) go(repos[0].id);
  }, [authed, repoId, repos]);

  if (authed === null) return null;
  if (!authed) {
    return (
      <>
        <header className="topbar"><div className="brand"><img src="/favicon.svg" alt="" /> helmreview</div></header>
        <Login />
      </>
    );
  }
  return (
    <>
      <TopBar />
      <div className="layout">
        <Sidebar repoId={repoId} tab={tab} upload={upload} />
        <main>{repoId ? <RepoPage key={repoId} repoId={repoId} tab={tab} /> : <Welcome />}</main>
      </div>
      <JobsPanel />
      <DropZone onFile={upload.onFile} />
      <Toasts />
    </>
  );
}

export default function App() {
  return (
    <AppProvider>
      <Shell />
    </AppProvider>
  );
}
