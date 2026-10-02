import * as XLSX from "xlsx";
import { readBytes, writeBytes } from "./files";
import type { Term } from "./glossary";

const HEADER_ALIASES: Record<string, "src" | "dst" | "info"> = {
  src: "src",
  source: "src",
  original: "src",
  key: "src",
  term: "src",
  "原文": "src",
  "源文": "src",
  "日文": "src",
  "名前": "src",
  dst: "dst",
  dest: "dst",
  translation: "dst",
  target: "dst",
  value: "dst",
  "译文": "dst",
  "翻译": "dst",
  "中文": "dst",
  "訳": "dst",
  info: "info",
  note: "info",
  notes: "info",
  comment: "info",
  description: "info",
  remark: "info",
  "描述": "info",
  "备注": "info",
  "说明": "info",
};

/** Read the first worksheet of an .xlsx file into Term rows. */
export async function readXlsxFile(path: string): Promise<Term[]> {
  const bytes = await readBytes(path);
  const wb = XLSX.read(bytes, { type: "array" });
  const sheet = wb.Sheets[wb.SheetNames[0]];
  if (!sheet) throw new Error("The workbook has no worksheets.");
  const rows = XLSX.utils.sheet_to_json<unknown[]>(sheet, { header: 1, defval: "" }) as unknown[][];
  if (!rows.length) return [];

  const header = rows[0].map((c) => String(c ?? "").trim());
  const mapping = new Map<string, number>();
  header.forEach((h, i) => {
    const role = HEADER_ALIASES[h.toLowerCase()];
    if (role && !mapping.has(role)) mapping.set(role, i);
  });

  const terms: Term[] = [];
  const body = mapping.has("src") && mapping.has("dst") ? rows.slice(1) : rows;
  for (const row of body) {
    const cell = (i: number | undefined) =>
      i === undefined || i >= row.length ? "" : String(row[i] ?? "").trim();
    const src = mapping.has("src") ? cell(mapping.get("src")) : cell(0);
    if (!src) continue;
    terms.push({
      src,
      dst: mapping.has("dst") ? cell(mapping.get("dst")) : cell(1),
      info: mapping.has("info") ? cell(mapping.get("info")) : cell(2),
      enabled: true,
      note: "",
    });
  }
  return terms;
}

/** Write terms as a single-sheet workbook with a bold header row. */
export async function writeXlsxFile(path: string, terms: Term[]): Promise<void> {
  const data: string[][] = [["src", "dst", "info", "enabled"]];
  for (const t of terms) data.push([t.src, t.dst, t.info, t.enabled ? "Yes" : "No"]);
  const ws = XLSX.utils.aoa_to_sheet(data);
  ws["!cols"] = [{ wch: 28 }, { wch: 28 }, { wch: 60 }, { wch: 9 }];
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, "Glossary");
  const bytes = XLSX.write(wb, { type: "array", bookType: "xlsx" }) as unknown as Uint8Array;
  await writeBytes(path, bytes);
}
