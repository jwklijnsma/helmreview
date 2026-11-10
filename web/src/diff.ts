export interface DiffFile {
  name: string;
  lines: string[];
  add: number;
  del: number;
}

/** Split a unified diff into files, with +/- counts per file. */
export function parseDiff(text: string): DiffFile[] {
  const files: (DiffFile & { header: boolean })[] = [];
  let cur: (DiffFile & { header: boolean }) | null = null;
  for (const line of text.split("\n")) {
    if (line.startsWith("--- ")) {
      cur = { name: line.slice(4).replace(/^a\//, "").split("\t")[0], lines: [], add: 0, del: 0, header: true };
      files.push(cur);
      continue;
    }
    if (!cur) continue;
    if (cur.header && line.startsWith("+++ ")) {
      const n = line.slice(4).replace(/^b\//, "").split("\t")[0];
      if (n !== "/dev/null") cur.name = n;
      cur.header = false;
      continue;
    }
    if (line.startsWith("diff --git") || line.startsWith("index ")) continue;
    if (line.startsWith("+")) cur.add++;
    else if (line.startsWith("-")) cur.del++;
    cur.lines.push(line);
  }
  return files.map(({ header: _h, ...f }) => {
    while (f.lines.length && f.lines[f.lines.length - 1] === "") f.lines.pop();
    return f;
  });
}
