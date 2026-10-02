import type { VndbCharacter, Vn } from "./vndb";

export interface Term {
  src: string;
  dst: string;
  info: string;
  enabled: boolean;
  /** Local bookkeeping (e.g. `vndb:c15061`); never exported. */
  note: string;
}

export type IssueLevel = "error" | "warning" | "info";
export interface Issue {
  level: IssueLevel;
  message: string;
  index: number | null;
}

export interface BuildOptions {
  includeAliases: boolean;
  aliasMinLen: number;
  onlyJapaneseOriginal: boolean;
  stripSpaces: boolean;
  /** Fill dst with VNDB's romanized name (e.g. 河野 貴明 -> Kouno Takaaki). */
  prefillDst: boolean;
  includeTraits: boolean;
  maxTraits: number;
  includeSpoilerInfo: boolean;
  /** VNDB reports gender as [apparent, real]; prefer the real (spoiler) value. */
  preferSpoilerValues: boolean;
  includeReading: boolean;
}

export const DEFAULT_OPTIONS: BuildOptions = {
  includeAliases: true,
  aliasMinLen: 2,
  onlyJapaneseOriginal: true,
  stripSpaces: false,
  prefillDst: true,
  includeTraits: true,
  maxTraits: 4,
  includeSpoilerInfo: true,
  preferSpoilerValues: true,
  includeReading: false,
};

export const GENDER_LABELS: Record<string, string> = {
  m: "Male",
  f: "Female",
  o: "Non-binary",
  a: "Ambiguous",
};

export const ROLE_LABELS: Record<string, string> = {
  main: "Protagonist",
  primary: "Main character",
  side: "Supporting character",
  appears: "Cameo only",
};

const JAPANESE_RE =
  /[぀-ミ゠-ヿㇰ-ㇿ㌀-㏿぀-ヿ一-鿿豈-﫿ｦ-ﾟ]/;
const LATIN_RE = /[A-Za-z]/;

export const hasJapanese = (t: string) => JAPANESE_RE.test(t ?? "");
export const hasLatin = (t: string) => LATIN_RE.test(t ?? "");

/** Case/width/whitespace-insensitive key for duplicate detection. */
export function normalizeKey(text: string): string {
  return (text ?? "")
    .normalize("NFKC")
    .replace(/　|\u00a0/g, " ")
    .replace(/\s+/g, "")
    .toLowerCase();
}

export function stripSpaces(text: string): string {
  return (text ?? "").normalize("NFKC").replace(/\s+/g, "");
}

// ---------------------------------------------------------------------------
// VNDB -> terms
// ---------------------------------------------------------------------------

function pickRole(c: VndbCharacter, vnId: string): { role: string; spoiler: number } {
  let best: { spoiler: number; role: string } | null = null;
  for (const v of c.vns ?? []) {
    if (v.id !== vnId) continue;
    const cand = { spoiler: Number(v.spoiler ?? 0), role: v.role ?? "" };
    if (!best || cand.spoiler < best.spoiler) best = cand;
  }
  return best ?? { role: "", spoiler: 0 };
}

function roleRank(c: VndbCharacter, vnId: string): [number, number] {
  const { role, spoiler } = pickRole(c, vnId);
  const order: Record<string, number> = { main: 0, primary: 1, side: 2, appears: 3 };
  return [order[role] ?? 4, spoiler];
}

/** Round-robin across trait groups so one colour doesn't eat all N slots. */
function pickTraits(
  traits: { name?: string; group_name?: string }[] | undefined,
  limit: number,
): { shown: string[]; total: number } {
  const buckets = new Map<string, string[]>();
  let total = 0;
  for (const t of traits ?? []) {
    const name = (t.name ?? "").trim();
    if (!name) continue;
    total += 1;
    const key = (t.group_name ?? "").trim();
    const bucket = buckets.get(key) ?? [];
    if (!bucket.includes(name)) bucket.push(name);
    buckets.set(key, bucket);
  }
  if (buckets.size === 0 || limit <= 0) return { shown: [], total };
  const shown: string[] = [];
  while (shown.length < limit && [...buckets.values()].some((b) => b.length > 0)) {
    for (const [key, names] of buckets) {
      if (!names.length) continue;
      const value = names.shift()!;
      shown.push(key ? `${key}: ${value}` : value);
      if (shown.length >= limit) break;
    }
  }
  return { shown, total };
}

function characterInfo(
  c: VndbCharacter,
  role: string,
  spoiler: number,
  o: BuildOptions,
): string {
  const parts: string[] = [];
  const genders = c.gender ?? [null];
  const gender =
    o.preferSpoilerValues && genders.length > 1 && genders[1] ? genders[1] : genders[0];
  const g = gender ? GENDER_LABELS[gender] : "";
  if (g) parts.push(g);
  if (role) parts.push(ROLE_LABELS[role] ?? role);
  if (spoiler >= 2 && o.includeSpoilerInfo) parts.push("[Spoiler]");
  if (o.includeTraits) {
    const { shown, total } = pickTraits(c.traits, o.maxTraits);
    if (shown.length > 0) {
      if (total > shown.length) shown.push(`... ${total} total`);
      parts.push("Traits: " + shown.join(" / "));
    }
  }
  if (o.includeReading) {
    const name = (c.name ?? "").trim();
    if (name) parts.push(`Romanization: ${name}`);
  }
  return parts.join(", ");
}

export function termsFromCharacter(
  c: VndbCharacter,
  vnId: string,
  o: BuildOptions = { ...DEFAULT_OPTIONS },
): Term[] {
  const name = (c.name ?? "").trim();
  const original = (c.original ?? "").trim();
  const { role, spoiler } = pickRole(c, vnId);
  const baseInfo = characterInfo(c, role, spoiler, o);
  const out: Term[] = [];

  const make = (src: string, aliasOf = ""): Term | null => {
    const s = o.stripSpaces ? stripSpaces(src) : src.trim();
    if (!s) return null;
    if (o.onlyJapaneseOriginal && !hasJapanese(s)) return null;
    const info = aliasOf
      ? baseInfo
        ? `${baseInfo}, Alias of ${aliasOf}`
        : `Alias of ${aliasOf}`
      : baseInfo;
    return { src: s, dst: o.prefillDst ? name : "", info, enabled: true, note: `vndb:${c.id}` };
  };

  if (original) {
    const t = make(original);
    if (t) out.push(t);
  }
  if (o.includeAliases) {
    for (const raw of c.aliases ?? []) {
      const alias = (raw ?? "").trim();
      if (!alias) continue;
      if (normalizeKey(alias) === normalizeKey(original)) continue;
      if (normalizeKey(alias) === normalizeKey(name)) continue;
      if (stripSpaces(alias).length < Math.max(1, o.aliasMinLen)) continue;
      const t = make(alias, name);
      if (t) out.push(t);
    }
  }
  return out;
}

export function termsFromVn(
  vn: Vn,
  characters: VndbCharacter[],
  o: BuildOptions = { ...DEFAULT_OPTIONS },
): { terms: Term[]; stats: { characters: number; terms: number; noOriginal: number } } {
  const vnId = vn.id ?? "";
  const stats = { characters: characters.length, terms: 0, noOriginal: 0 };
  const ordered = [...characters].sort((a, b) => {
    const [ra, sa] = roleRank(a, vnId);
    const [rb, sb] = roleRank(b, vnId);
    return ra - rb || sa - sb || (a.name ?? "").localeCompare(b.name ?? "");
  });
  const all: Term[] = [];
  for (const c of ordered) {
    if (!((c.original ?? "").trim())) stats.noOriginal += 1;
    const produced = termsFromCharacter(c, vnId, o);
    stats.terms += produced.length;
    all.push(...produced);
  }
  const seen = new Set<string>();
  const deduped = all.filter((t) => {
    const k = normalizeKey(t.src);
    if (!k || seen.has(k)) return false;
    seen.add(k);
    return true;
  });
  stats.terms = deduped.length;
  return { terms: deduped, stats };
}

// ---------------------------------------------------------------------------
// validation
// ---------------------------------------------------------------------------

export function validateTerms(terms: Term[], maxSrcLen = 20): Issue[] {
  const issues: Issue[] = [];
  const seen = new Map<string, number>();
  terms.forEach((t, index) => {
    if (!t.enabled) return;
    const src = t.src.trim();
    const dst = t.dst.trim();
    if (!src) {
      issues.push({ level: "error", message: `Row ${index + 1}: source term is empty.`, index });
      return;
    }
    const key = normalizeKey(src);
    if (seen.has(key)) {
      issues.push({
        level: "warning",
        message: `Row ${index + 1}: source "${src}" duplicates row ${(seen.get(key) ?? 0) + 1}.`,
        index,
      });
    } else {
      seen.set(key, index);
    }
    if (!dst) {
      issues.push({ level: "warning", message: `Row ${index + 1}: "${src}" has no translation yet.`, index });
    } else if (dst === src) {
      issues.push({ level: "info", message: `Row ${index + 1}: "${src}" is translated as itself.`, index });
    }
    if (src.length > maxSrcLen) {
      issues.push({
        level: "info",
        message: `Row ${index + 1}: source is long (${src.length} chars); glossaries usually only hold proper nouns.`,
        index,
      });
    }
    if (/\s/.test(src)) {
      issues.push({
        level: "info",
        message: `Row ${index + 1}: "${src}" contains a space; the game may use the unspaced form.`,
        index,
      });
    }
    if (!hasJapanese(src) && hasLatin(src)) {
      issues.push({
        level: "info",
        message: `Row ${index + 1}: "${src}" is not Japanese. Ignore this if the source language is not Japanese.`,
        index,
      });
    }
    if (/[，。！？、；：\n\r]/.test(src)) {
      issues.push({
        level: "warning",
        message: `Row ${index + 1}: "${src}" contains punctuation or a line break; consider splitting it into separate terms.`,
        index,
      });
    }
  });
  const order: Record<IssueLevel, number> = { error: 0, warning: 1, info: 2 };
  return issues.sort((a, b) => order[a.level] - order[b.level] || (a.index ?? 0) - (b.index ?? 0));
}

// ---------------------------------------------------------------------------
// import
// ---------------------------------------------------------------------------

export type GlossaryShape = "list" | "dict" | "actors";
export const SHAPE_LABELS: Record<GlossaryShape, string> = {
  list: "standard dictionary list",
  dict: "key/value dictionary",
  actors: "RPG Maker Actors.json",
};

const SRC_KEYS = ["src", "source", "original", "key", "term", "原文", "源文", "日文", "名前"];
const DST_KEYS = ["dst", "dest", "destination", "translation", "target", "value", "译文", "翻译", "中文", "訳"];
const INFO_KEYS = ["info", "note", "notes", "comment", "commentary", "description", "remark", "描述", "备注", "说明"];
const NICK_KEYS = ["nickname", "nick", "别名", "昵称"];
const NAME_KEYS = ["name", "姓名", "名字", "名前"];

function first(obj: Record<string, unknown>, keys: string[]): string {
  for (const k of keys) {
    const v = obj[k];
    if (v !== null && v !== undefined && v !== "") return String(v);
  }
  return "";
}

function termsFromRecords(records: unknown[]): { terms: Term[]; shape: GlossaryShape } {
  const rows = records.filter((r) => r !== null && r !== undefined);
  if (!rows.length) return { terms: [], shape: "list" };
  const head = rows[0];
  if (Array.isArray(head)) {
    const terms: Term[] = [];
    for (const r of rows) {
      if (!Array.isArray(r) || r.length < 2) continue;
      const src = String(r[0] ?? "").trim();
      if (src) {
        terms.push({
          src,
          dst: String(r[1] ?? "").trim(),
          info: r.length > 2 ? String(r[2] ?? "").trim() : "",
          enabled: true,
          note: "",
        });
      }
    }
    return { terms, shape: "list" };
  }
  if (typeof head !== "object") {
    throw new Error("Unrecognised structure: list items are neither objects nor arrays.");
  }
  const dicts = rows.filter((r): r is Record<string, unknown> => typeof r === "object" && !Array.isArray(r));
  const h = dicts[0] as Record<string, unknown>;
  if (!("src" in h) && NAME_KEYS.some((k) => k in h)) {
    return {
      terms: dicts.flatMap((r) => {
        const src = first(r, NAME_KEYS).trim();
        return src
          ? [{ src, dst: first(r, NICK_KEYS).trim(), info: first(r, INFO_KEYS), enabled: true, note: "" }]
          : [];
      }),
      shape: "actors",
    };
  }
  return {
    terms: dicts.flatMap((r) => {
      const src = first(r, SRC_KEYS).trim();
      return src
        ? [{ src, dst: first(r, DST_KEYS).trim(), info: first(r, INFO_KEYS), enabled: true, note: "" }]
        : [];
    }),
    shape: "list",
  };
}

export function parseGlossaryText(text: string): { terms: Term[]; shape: GlossaryShape } {
  const stripped = text.replace(/^﻿/, "").trim();
  if (!stripped) throw new Error("The file is empty.");
  let data: unknown;
  try {
    data = JSON.parse(stripped);
  } catch (e) {
    throw new Error(`Could not parse JSON: ${e instanceof Error ? e.message : e}`);
  }
  if (Array.isArray(data)) return termsFromRecords(data);
  if (data && typeof data === "object") {
    const obj = data as Record<string, unknown>;
    if (Object.values(obj).every((v) => typeof v === "string")) {
      return {
        terms: Object.entries(obj).map(([src, dst]) => ({
          src,
          dst: dst as string,
          info: "",
          enabled: true,
          note: "",
        })),
        shape: "dict",
      };
    }
    for (const k of ["glossary", "terms", "entries", "data"]) {
      if (Array.isArray(obj[k])) return termsFromRecords(obj[k] as unknown[]);
    }
    const terms: Term[] = [];
    for (const [src, v] of Object.entries(obj)) {
      if (typeof v === "string") terms.push({ src, dst: v, info: "", enabled: true, note: "" });
      else if (v && typeof v === "object") {
        const r = v as Record<string, unknown>;
        terms.push({ src, dst: first(r, DST_KEYS), info: first(r, INFO_KEYS), enabled: true, note: "" });
      }
    }
    return { terms, shape: "dict" };
  }
  throw new Error("Unrecognised file structure.");
}

// ---------------------------------------------------------------------------
// export
// ---------------------------------------------------------------------------

export function dumpJson(terms: Term[], shape: "list" | "dict"): string {
  const payload =
    shape === "dict"
      ? Object.fromEntries(terms.map((t) => [t.src, t.dst]))
      : terms.map((t) => ({ src: t.src, dst: t.dst, info: t.info }));
  return JSON.stringify(payload, null, 2) + "\n";
}

export function sanitizeFilename(text: string): string {
  const clean = (text ?? "").trim().replace(/[<>:"/\\|?*\x00-\x1f]/g, "_").replace(/[. ]+$/, "");
  return (clean || "glossary").slice(0, 80);
}

export function presetFileName(directory: string[], filename: string): string {
  let name = filename.split(/[\\/]/).pop() ?? "glossary";
  if (!name.toLowerCase().endsWith(".json")) name += ".json";
  return /^\d{2}_/.test(name)
    ? name
    : `${String(Math.max(0, ...directory.filter((f) => /^\d{2}_/.test(f)).map((f) => Number(f.slice(0, 2)))) + 1).padStart(2, "0")}_${name}`;
}
