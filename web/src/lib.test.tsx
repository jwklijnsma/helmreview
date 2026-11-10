import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { parseDiff } from "./diff";
import { Markdown } from "./markdown";
import type { Job } from "./types";
import { ago, dur, exitText, jobTitle, progressPct, size } from "./util";

const job = (over: Partial<Job>): Job => ({
  id: "abc", repo: "0:infra", repo_name: "infra", action: "scan", options: {}, status: "running",
  dry_run: false, exit_code: null, created: 0, started: 1, ended: null, seconds: 3, downloads: [],
  position: null, ...over,
});

describe("markdown", () => {
  it("renders tables, lists and inline code", () => {
    const html = renderToStaticMarkup(<Markdown src={"# Fixes\n\n| a | n |\n|---|--:|\n| x | 1 |\n\n- `no_log` **added**"} />);
    expect(html).toContain("<h1>Fixes</h1>");
    expect(html).toContain('<td class="num">1</td>');
    expect(html).toContain("<code>no_log</code>");
    expect(html).toContain("<strong>added</strong>");
  });
  it("never turns input into HTML", () => {
    const html = renderToStaticMarkup(<Markdown src={'<img src=x onerror=alert(1)> and `<script>`'} />);
    expect(html).not.toContain("<img");
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;img");
  });
  it("leaves underscores alone", () => {
    expect(renderToStaticMarkup(<Markdown src="ignore_errors and failed_when" />)).toBe("<p>ignore_errors and failed_when</p>");
  });
});

describe("diff", () => {
  it("splits files and counts lines", () => {
    const files = parseDiff("--- a/x.yml\n+++ b/x.yml\n@@ -1 +1 @@\n-a\n+b\n+c\n--- a/new.yml\n+++ b/new.yml\n@@ -0,0 +1 @@\n+z\n");
    expect(files.map((f) => [f.name, f.add, f.del])).toEqual([["x.yml", 2, 1], ["new.yml", 1, 0]]);
  });
});

describe("jobs", () => {
  it("names jobs the way the UI shows them", () => {
    expect(jobTitle(job({}))).toBe("Scan");
    expect(jobTitle(job({ action: "fix", dry_run: true }))).toBe("Fix preview");
    expect(jobTitle(job({ action: "fix", repo: "u:0123456789ab" }))).toBe("Fix & download");
    expect(jobTitle(job({ action: "fix" }))).toBe("Apply fixes");
  });
  it("describes progress and queue position", () => {
    expect(exitText(job({ status: "queued", position: 2 }))).toBe("waiting · #2 in the queue");
    expect(exitText(job({ progress: { total: 4, done: 1, file: "site.yml" } }))).toBe("model pass 1/4 · site.yml");
    expect(progressPct(job({ progress: { total: 4, done: 1, file: null } }))).toBe(25);
    expect(progressPct(job({}))).toBeNull();
    expect(progressPct(job({ status: "queued" }))).toBe(0);
    expect(exitText(job({ status: "cancelled", started: null }))).toBe("cancelled before it started");
  });
  it("formats", () => {
    expect(dur(75)).toBe("1m 15s");
    expect(size(2048)).toBe("2 KB");
    expect(ago(1000, 1000 + 7200)).toBe("2 h ago");
  });
});
