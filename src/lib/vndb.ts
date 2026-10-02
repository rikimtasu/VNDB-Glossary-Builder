import { invoke } from "@tauri-apps/api/core";

export const DEFAULT_ENDPOINT = "https://api.vndb.org/kana";
export const USER_AGENT = "vndb-glossary-builder/1.0 (tauri)";

export const VN_FIELDS =
  "id,title,alttitle,olang,released,languages," +
  "titles{lang,title,latin,main,official}";

export const CHARACTER_FIELDS =
  "id,name,original,aliases,sex,gender,description," +
  "blood_type,height,weight,bust,waist,hips,cup,age,birthday," +
  "traits{name,group_name,sexual}," +
  "vns{id,role,spoiler}";

export interface Vn {
  id: string;
  title?: string;
  alttitle?: string | null;
  olang?: string;
  released?: string | null;
  languages?: string[];
  titles?: { lang: string; title: string; latin?: string | null; main?: boolean }[];
  [k: string]: unknown;
}

export interface VndbCharacter {
  id: string;
  name?: string;
  original?: string | null;
  aliases?: string[];
  sex?: (string | null)[];
  gender?: (string | null)[];
  traits?: { name?: string; group_name?: string }[];
  vns?: { id: string; role?: string; spoiler?: number }[];
  [k: string]: unknown;
}

const VNDB_ID_RE = /^v\d+$/i;
const VNDB_URL_RE = /vndb\.org\/[vrp](\d+)/i;

/** Extract a `v<number>` id from loose user input, or null. */
export function parseVnId(text: string): string | null {
  const t = (text ?? "").trim();
  if (!t) return null;
  const m = VNDB_URL_RE.exec(t);
  if (m) return "v" + m[1];
  if (VNDB_ID_RE.test(t)) return t.toLowerCase();
  if (/^\d+$/.test(t)) return "v" + t;
  return null;
}

// Client-side throttle: the API allows ~200 requests / 5 min.
let lastRequest = 0;
const MIN_INTERVAL = 350;

async function throttled<T>(fn: () => Promise<T>): Promise<T> {
  const gap = MIN_INTERVAL - (Date.now() - lastRequest);
  if (gap > 0) await new Promise((r) => setTimeout(r, gap));
  lastRequest = Date.now();
  return fn();
}

export interface QueryArgs {
  filters?: unknown;
  fields?: string;
  results?: number;
  page?: number;
  sort?: string;
  reverse?: boolean;
  count?: boolean;
}

export async function vndbQuery(
  path: string,
  args: QueryArgs,
  endpoint: string = DEFAULT_ENDPOINT,
  token = "",
): Promise<{ results: Record<string, unknown>[]; more?: boolean }> {
  const payload: Record<string, unknown> = {
    results: Math.max(1, Math.min(args.results ?? 25, 100)),
    page: args.page ?? 1,
  };
  if (args.filters) payload.filters = args.filters;
  if (args.fields) payload.fields = args.fields;
  if (args.sort) payload.sort = args.sort;
  if (args.reverse) payload.reverse = true;
  if (args.count) payload.count = true;
  return throttled(() =>
    invoke<{ results: Record<string, unknown>[]; more?: boolean }>("vndb_request", {
      endpoint,
      path,
      payload,
      token,
    }),
  );
}

export async function searchVns(
  text: string,
  endpoint: string,
  results = 30,
): Promise<Vn[]> {
  const t = (text ?? "").trim();
  if (!t) throw new Error("Enter something to search for.");
  const vnId = parseVnId(t);
  if (vnId && (t.toLowerCase().startsWith("v") || t.includes("/") || /^\d+$/.test(t))) {
    const res = await vndbQuery("vn", { filters: ["id", "=", vnId], fields: VN_FIELDS, results }, endpoint);
    if (!res.results.length) throw new Error(`No visual novel found for ${vnId}.`);
    return res.results as unknown as Vn[];
  }
  const res = await vndbQuery(
    "vn",
    { filters: ["search", "=", t], fields: VN_FIELDS, results, sort: "searchrank" },
    endpoint,
  );
  return (res.results ?? []) as unknown as Vn[];
}

/** Fetch the full character cast (voiced and unvoiced) with pagination. */
export async function vnCharacters(
  vnId: string,
  endpoint: string,
  onProgress?: (count: number) => void,
): Promise<VndbCharacter[]> {
  const id = parseVnId(vnId) ?? (vnId ?? "").trim();
  if (!id) throw new Error("Enter a vndbid.");
  const out: VndbCharacter[] = [];
  let page = 1;
  for (;;) {
    const res = await vndbQuery(
      "character",
      {
        filters: ["and", ["vn", "=", ["id", "=", id]]],
        fields: CHARACTER_FIELDS,
        results: 100,
        page,
        sort: "id",
      },
      endpoint,
    );
    for (const item of res.results ?? []) out.push(item as unknown as VndbCharacter);
    onProgress?.(out.length);
    if (!res.more) return out;
    page += 1;
  }
}
