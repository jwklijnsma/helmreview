import type { ReactNode } from "react";

// A tiny, safe Markdown renderer for the fix report: headings, lists, tables,
// paragraphs, `code` and **bold**. Everything ends up as React text nodes, never HTML.
// No _emphasis_: rule names such as ignore_errors would turn italic.

export function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(`[^`]+`|\*\*[^*]+\*\*)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let k = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const t = m[0];
    out.push(t.startsWith("`") ? <code key={k++}>{t.slice(1, -1)}</code> : <strong key={k++}>{t.slice(2, -2)}</strong>);
    last = m.index + t.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function Markdown({ src }: { src: string }) {
  const blocks: ReactNode[] = [];
  const lines = src.split("\n");
  let i = 0;
  let k = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (!l.trim()) {
      i++;
      continue;
    }
    const hm = l.match(/^(#{1,4})\s+(.*)$/);
    if (hm) {
      const level = Math.min(hm[1].length, 4);
      const H = (`h${level}` as unknown) as "h1";
      blocks.push(<H key={k++}>{inline(hm[2])}</H>);
      i++;
      continue;
    }
    if (l.startsWith("|")) {
      const rows: string[] = [];
      while (i < lines.length && lines[i].startsWith("|")) rows.push(lines[i++]);
      const cells = (r: string) => r.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const hasAlign = rows[1] !== undefined && /^\|?\s*:?-+/.test(rows[1]);
      const align = hasAlign ? cells(rows[1]).map((c) => c.endsWith(":")) : [];
      const body = rows.slice(hasAlign ? 2 : 1);
      blocks.push(
        <table key={k++}>
          <thead>
            <tr>{cells(rows[0]).map((c, j) => <th key={j} className={align[j] ? "num" : undefined}>{inline(c)}</th>)}</tr>
          </thead>
          <tbody>
            {body.map((r, n) => (
              <tr key={n}>{cells(r).map((c, j) => <td key={j} className={align[j] ? "num" : undefined}>{inline(c)}</td>)}</tr>
            ))}
          </tbody>
        </table>,
      );
      continue;
    }
    if (/^\s*[-*] /.test(l)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*] /.test(lines[i])) items.push(lines[i++].replace(/^\s*[-*] /, ""));
      blocks.push(<ul key={k++}>{items.map((t, n) => <li key={n}>{inline(t)}</li>)}</ul>);
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(#|\||\s*[-*] )/.test(lines[i])) para.push(lines[i++]);
    blocks.push(<p key={k++}>{inline(para.join(" "))}</p>);
  }
  return <>{blocks}</>;
}
