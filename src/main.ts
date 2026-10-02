import "./styles.css";
import { invoke } from "@tauri-apps/api/core";
import { open, save, ask, message } from "@tauri-apps/plugin-dialog";
import { openUrl } from "@tauri-apps/plugin-opener";
import { readText, writeText, listDir, makeDir } from "./lib/files";
import {
  DEFAULT_OPTIONS,
  SHAPE_LABELS,
  dumpJson,
  normalizeKey,
  parseGlossaryText,
  presetFileName,
  sanitizeFilename,
  termsFromVn,
  validateTerms,
  type BuildOptions,
  type Issue,
  type Term,
} from "./lib/glossary";
import { parseVnId, searchVns, vnCharacters, DEFAULT_ENDPOINT, type Vn } from "./lib/vndb";
import { readXlsxFile, writeXlsxFile } from "./lib/xlsx";

// ---------------------------------------------------------------------------
// settings
// ---------------------------------------------------------------------------

interface Settings {
  endpoint: string;
  token: string;
  linguaGachaRoot: string;
  presetLang: string;
  options: BuildOptions;
  exportShape: "list" | "dict";
  skipUntranslated: boolean;
}

const SETTINGS_KEY = "vndb-glossary-settings-v1";

function loadSettings(): Settings {
  const fallback: Settings = {
    endpoint: DEFAULT_ENDPOINT,
    token: "",
    linguaGachaRoot: "",
    presetLang: "zh",
    options: { ...DEFAULT_OPTIONS },
    exportShape: "list",
    skipUntranslated: true,
  };
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (raw) return { ...fallback, ...(JSON.parse(raw) as Partial<Settings>) };
  } catch {
    /* ignore */
  }
  return fallback;
}

function saveSettings() {
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
}

const settings = loadSettings();

// ---------------------------------------------------------------------------
// state
// ---------------------------------------------------------------------------

let vns: Vn[] = [];
let selectedVn: Vn | null = null;
let terms: Term[] = [];
let issues: Issue[] = [];
let filterText = "";
let showUntranslatedOnly = false;
let busy = false;
let selectedRows = new Set<number>();
let sortKey: "src" | "dst" | "info" | null = null;
let sortDir: 1 | -1 = 1;

const $ = <T extends HTMLElement = HTMLElement>(sel: string) =>
  document.querySelector<T>(sel)!;

// ---------------------------------------------------------------------------
// shell
// ---------------------------------------------------------------------------

document.querySelector("#app")!.innerHTML = `
  <fieldset class="panel" id="vn-panel">
    <legend class="panel-title">1 · Find a visual novel on VNDB</legend>
    <div class="search-row">
      <label>Search or vndbid:</label>
      <input type="text" id="search-input" placeholder="title, alias, v17, or vndb.org link…" />
      <button id="btn-search" class="primary">Search VNs</button>
      <button id="btn-by-id">Load by vndbid</button>
      <button id="btn-open-vndb">Open on vndb.org</button>
    </div>
    <div class="vn-split">
      <div class="vn-list">
        <div class="panel-title">Visual novels</div>
        <div class="scroll vn-scroll">
          <table class="grid" id="vn-table">
            <thead><tr><th>ID</th><th>Title</th><th>Original</th><th>Released</th><th>Lang</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>
      <div class="vn-options">
        <div>
          <h3>Build options · Characters</h3>
          <label><input type="checkbox" id="opt-aliases" /> Include aliases / nicknames</label>
          <div class="opt-row"><label>Min alias length:</label>
            <input type="number" id="opt-alias-min" min="1" max="12" /></div>
          <label><input type="checkbox" id="opt-jp-only" /> Only Japanese source names</label>
          <label><input type="checkbox" id="opt-strip" /> Strip spaces from source names</label>
          <label><input type="checkbox" id="opt-prefill" /> Fill dst with romanized name</label>
        </div>
        <div>
          <h3>Info column</h3>
          <label><input type="checkbox" id="opt-traits" /> Write character traits</label>
          <div class="opt-row"><label>Max traits:</label>
            <input type="number" id="opt-trait-max" min="0" max="20" /></div>
          <label><input type="checkbox" id="opt-spoiler" /> Flag spoilered characters</label>
          <label><input type="checkbox" id="opt-reading" /> Also repeat romanization in info</label>
        </div>
        <button id="btn-fetch-append" class="primary">Fetch Characters → Append</button>
        <button id="btn-fetch-replace">Fetch Characters → Replace All</button>
        <div class="vn-selected" id="vn-selected">No visual novel selected</div>
      </div>
    </div>
  </fieldset>

  <fieldset class="panel" id="glossary-panel">
    <legend class="panel-title">2 · Glossary — double-click a cell to edit. Click ✓ to enable/disable a row. Only enabled rows are exported.</legend>
    <div class="toolbar">
      <button id="btn-import">Import…</button>
      <button id="btn-merge">Merge Import</button>
      <button id="btn-exp-json">Export JSON</button>
      <button id="btn-exp-xlsx">Export XLSX</button>
      <button id="btn-save-preset">Save Preset</button>
      <button id="btn-load-preset">Load Preset</button>
      <button id="btn-add-row">Add Row</button>
      <button id="btn-del">Delete Selected</button>
      <button id="btn-dedupe">Dedupe</button>
      <button id="btn-validate">Validate</button>
    </div>
    <div class="filter-row">
      <label>Filter: <input type="text" id="filter-input" /></label>
      <label><input type="checkbox" id="flt-untranslated" /> Show untranslated only</label>
      <label><input type="checkbox" id="flt-skip" /> Skip untranslated on export</label>
      <label class="dim">Export format:
        <select id="export-shape"><option value="list">list</option><option value="dict">dict</option></select>
      </label>
      <span class="spacer"></span>
      <span class="dim" id="term-count">0 terms</span>
    </div>
    <div class="scroll gloss-scroll">
      <table class="grid" id="gloss-table">
        <thead><tr>
          <th style="width:34px">✓</th><th data-sort="src">Source (src)</th>
          <th data-sort="dst">Translation (dst)</th><th data-sort="info">Description (info)</th>
          <th>Source ID</th>
        </tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </fieldset>

  <fieldset class="panel" id="log-panel">
    <legend class="panel-title">3 · Validation / log</legend>
    <div id="log"></div>
  </fieldset>

  <div id="statusbar"><span class="spinner" id="spinner"></span><span id="status">Ready.</span></div>
  <div id="modal-root"></div>
`;

function setBusy(on: boolean, label?: string) {
  busy = on;
  $("#spinner").classList.toggle("on", on);
  for (const id of ["#btn-search", "#btn-fetch-append", "#btn-fetch-replace"]) {
    ($(id) as HTMLButtonElement).disabled = on;
  }
  if (label !== undefined) $("#status").textContent = label;
}

// ---------------------------------------------------------------------------
// log + status
// ---------------------------------------------------------------------------

function log(message: string, cls: "error" | "warning" | "note" | "good" = "note") {
  const el = document.createElement("div");
  el.className = cls;
  el.textContent = message;
  const box = $("#log");
  box.appendChild(el);
  box.scrollTop = box.scrollHeight;
}

function clearLog() {
  $("#log").innerHTML = "";
}

function updateStatus(message?: string) {
  if (message !== undefined) {
    $("#status").textContent = message;
    return;
  }
  const enabled = terms.filter((t) => t.enabled);
  const untranslated = enabled.filter((t) => t.src.trim() && !t.dst.trim()).length;
  const exporting = exportable().length;
  const suffix = settings.exportShape === "dict" ? " (current format drops info)" : "";
  $("#status").textContent =
    `${terms.length} terms · ${enabled.length} enabled · ` +
    `${untranslated} untranslated · exporting ${exporting}${suffix}`;
  $("#term-count").textContent = `${terms.length} terms`;
}

function exportable(): Term[] {
  return terms.filter(
    (t) =>
      t.enabled && t.src.trim() && (!settings.skipUntranslated || t.dst.trim()),
  );
}

// ---------------------------------------------------------------------------
// VN panel
// ---------------------------------------------------------------------------

function mainTitle(vn: Vn): string {
  const main = (vn.titles ?? []).find((t) => t.main);
  return main?.title ?? vn.alttitle ?? "-";
}

function renderVnTable() {
  const body = $("#vn-table tbody") as HTMLTableSectionElement;
  body.innerHTML = "";
  vns.forEach((vn, i) => {
    const tr = document.createElement("tr");
    if (selectedVn?.id === vn.id) tr.classList.add("selected");
    for (const v of [vn.id, vn.title ?? "", vn.alttitle ?? "", vn.released ?? "TBA", vn.olang ?? ""]) {
      const td = document.createElement("td");
      td.textContent = String(v ?? "");
      tr.appendChild(td);
    }
    tr.addEventListener("click", () => selectVn(i));
    tr.addEventListener("dblclick", () => fetchCharacters("append"));
    body.appendChild(tr);
  });
}

function selectVn(i: number) {
  selectedVn = vns[i] ?? null;
  renderVnTable();
  const el = $("#vn-selected");
  if (!selectedVn) {
    el.textContent = "No visual novel selected";
    return;
  }
  const langs = (selectedVn.languages ?? []).join(", ") || "-";
  el.textContent =
    `Selected: ${selectedVn.title}  [${selectedVn.id}]\n` +
    `Original title: ${mainTitle(selectedVn)}\nLanguages: ${langs}`;
}

async function doSearch() {
  const text = ($("#search-input") as HTMLInputElement).value.trim();
  if (!text) {
    log("Enter something to search for.", "warning");
    return;
  }
  setBusy(true, `Searching for "${text}"…`);
  try {
    vns = await searchVns(text, settings.endpoint);
    selectedVn = null;
    renderVnTable();
    log(`Found ${vns.length} visual novels.`, "good");
    updateStatus(
      `Found ${vns.length} visual novels. Double-click one to fetch its characters.`,
    );
    if (vns.length) selectVn(0);
  } catch (e) {
    setBusy(false);
    updateStatus("Request failed");
    const msg = e instanceof Error ? e.message : String(e);
    log(`[Error] ${msg}`, "error");
    await message(msg.split("\n")[0], { title: "Request failed", kind: "error" });
    return;
  }
  setBusy(false);
}

async function fetchCharacters(mode: "append" | "replace") {
  if (busy) {
    log("The previous request has not finished yet.", "warning");
    return;
  }
  if (!selectedVn) {
    await message("Select a visual novel in the list first.", { title: "Nothing selected" });
    return;
  }
  const vn = { ...selectedVn };
  const options = { ...settings.options };
  setBusy(true, `Fetching characters for ${vn.id}…`);
  try {
    const characters = await vnCharacters(vn.id, settings.endpoint, (n) =>
      updateStatus(`Read ${n} characters…`),
    );
    updateStatus("Building terms…");
    // Let the UI breathe before the CPU-heavy build.
    await new Promise((r) => setTimeout(r, 0));
    const { terms: fresh, stats } = termsFromVn(vn, characters, options);
    if (mode === "replace") {
      terms = fresh;
      log(`Replaced the glossary with characters from ${vn.title}: ${fresh.length} terms.`, "good");
    } else {
      const before = new Set(terms.map((t) => normalizeKey(t.src)));
      let added = 0;
      let dups = 0;
      for (const t of fresh) {
        const k = normalizeKey(t.src);
        if (!k || before.has(k)) {
          dups += 1;
        } else {
          before.add(k);
          terms.push(t);
          added += 1;
        }
      }
      log(
        `Appended ${added} terms from ${vn.title} [${vn.id}] ` +
          `(${dups} duplicates skipped, ${stats.characters} characters total).`,
        "good",
      );
    }
    dedupeTerms(true);
    selectedRows.clear();
    renderGlossary();
    runValidation(true);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    log(`[Error] ${msg}`, "error");
    await message(msg.split("\n")[0], { title: "Request failed", kind: "error" });
  }
  setBusy(false);
}

// ---------------------------------------------------------------------------
// glossary table
// ---------------------------------------------------------------------------

function viewIndices(): number[] {
  const needle = normalizeKey(filterText);
  const out: number[] = [];
  terms.forEach((t, i) => {
    if (!t.enabled) {
      if (!needle && !showUntranslatedOnly) out.push(i);
      return;
    }
    if (showUntranslatedOnly && t.dst.trim()) return;
    if (needle && !normalizeKey(`${t.src}${t.dst}${t.info}`).includes(needle)) return;
    out.push(i);
  });
  if (sortKey) {
    const k = sortKey;
    const d = sortDir;
    out.sort((a, b) => terms[a][k].localeCompare(terms[b][k]) * d);
  }
  return out;
}

function renderGlossary() {
  const body = $("#gloss-table tbody") as HTMLTableSectionElement;
  body.innerHTML = "";
  const worst = new Map<number, string>();
  for (const issue of issues) {
    if (issue.index === null) continue;
    if (worst.get(issue.index) !== "error") worst.set(issue.index, issue.level);
  }
  for (const i of viewIndices()) {
    const t = terms[i];
    const tr = document.createElement("tr");
    const level = worst.get(i);
    if (level === "error") tr.classList.add("row-error");
    else if (level === "warning") tr.classList.add("row-warning");
    if (!t.enabled) tr.classList.add("row-off");
    if (selectedRows.has(i)) tr.classList.add("selected");

    const toggle = document.createElement("td");
    toggle.textContent = t.enabled ? "✓" : "";
    toggle.style.cursor = "pointer";
    toggle.style.textAlign = "center";
    toggle.addEventListener("click", () => {
      t.enabled = !t.enabled;
      renderGlossary();
    });
    tr.appendChild(toggle);

    for (const [col, value] of [
      ["src", t.src],
      ["dst", t.dst],
      ["info", t.info],
    ] as const) {
      const td = document.createElement("td");
      td.textContent = value;
      td.classList.add("cell-edit");
      td.title = "Double-click to edit";
      td.addEventListener("dblclick", () => beginEdit(td, i, col));
      tr.appendChild(td);
    }
    const origin = document.createElement("td");
    origin.textContent = t.note;
    origin.classList.add("dim");
    tr.appendChild(origin);

    tr.addEventListener("click", (e) => {
      if ((e.target as HTMLElement).tagName === "INPUT") return;
      if (e.ctrlKey || e.metaKey) {
        if (selectedRows.has(i)) selectedRows.delete(i);
        else selectedRows.add(i);
      } else {
        selectedRows = new Set([i]);
      }
      renderGlossary();
    });
    body.appendChild(tr);
  }
  document.querySelectorAll<HTMLTableCellElement>("#gloss-table th[data-sort]").forEach((th) => {
    const k = th.dataset.sort as "src" | "dst" | "info";
    th.style.cursor = "pointer";
    th.textContent = `${sortKey === k ? (sortDir === 1 ? "▲ " : "▼ ") : ""}${{ src: "Source (src)", dst: "Translation (dst)", info: "Description (info)" }[k]}`;
    th.onclick = () => {
      if (sortKey === k) {
        if (sortDir === 1) sortDir = -1;
        else sortKey = null;
      } else {
        sortKey = k;
        sortDir = 1;
      }
      renderGlossary();
    };
  });
  updateStatus();
}

function beginEdit(td: HTMLTableCellElement, index: number, col: "src" | "dst" | "info") {
  if (td.querySelector("input")) return;
  const original = terms[index][col];
  td.textContent = "";
  const input = document.createElement("input");
  input.value = original;
  td.appendChild(input);
  input.focus();
  input.select();
  let done = false;
  const commit = (save: boolean) => {
    if (done) return;
    done = true;
    if (save) {
      const v = input.value.trim();
      if (v !== terms[index][col]) {
        if (col === "src" && v && terms.some((t, j) => j !== index && normalizeKey(t.src) === normalizeKey(v))) {
          log(`Row ${index + 1}: "${v}" already exists; kept the old value.`, "warning");
        } else {
          terms[index][col] = col === "info" ? input.value.trim() : v;
        }
      }
    }
    renderGlossary();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") commit(true);
    else if (e.key === "Escape") commit(false);
    e.stopPropagation();
  });
  input.addEventListener("blur", () => commit(true));
}

function dedupeTerms(silent = false): number {
  const seen = new Set<string>();
  const kept: Term[] = [];
  let removed = 0;
  for (const t of terms) {
    const k = normalizeKey(t.src);
    if (!k || seen.has(k)) {
      removed += 1;
      continue;
    }
    seen.add(k);
    kept.push(t);
  }
  terms = kept;
  if (!silent) log(`Removed ${removed} duplicate or empty source terms.`);
  return removed;
}

function runValidation(quiet = false) {
  issues = validateTerms(terms);
  const counts = { error: 0, warning: 0, info: 0 };
  for (const i of issues) counts[i.level] += 1;
  if (issues.length || !quiet) {
    clearLog();
    if (!issues.length) {
      log("Validation passed: no problems found.", "good");
    } else {
      const labels = { error: "Error", warning: "Warning", info: "Note" } as const;
      for (const i of issues) {
        log(`[${labels[i.level]}] ${i.message}`, i.level === "info" ? "note" : i.level);
      }
      log(`-- ${counts.error} error(s), ${counts.warning} warning(s), ${counts.info} note(s).`);
    }
  }
  renderGlossary();
  updateStatus(`Validation: ${counts.error} error(s), ${counts.warning} warning(s), ${counts.info} note(s).`);
}

// ---------------------------------------------------------------------------
// import / export
// ---------------------------------------------------------------------------

async function importFile(merge: boolean) {
  const path = await open({
    title: merge ? "Merge glossary into current one" : "Import glossary",
    filters: [{ name: "Glossary files", extensions: ["json", "xlsx", "txt"] }],
  });
  if (typeof path !== "string" || !path) return;
  try {
    let incoming;
    if (path.toLowerCase().endsWith(".xlsx")) {
      incoming = { terms: await readXlsxFile(path), shape: "list" as const };
    } else {
      incoming = parseGlossaryText(await readText(path));
    }
    const label = SHAPE_LABELS[incoming.shape] ?? incoming.shape;
    if (merge) {
      let added = 0;
      let updated = 0;
      const byKey = new Map(terms.map((t) => [normalizeKey(t.src), t]));
      for (const t of incoming.terms) {
        const cur = byKey.get(normalizeKey(t.src));
        if (!cur) {
          terms.push(t);
          byKey.set(normalizeKey(t.src), t);
          added += 1;
        } else {
          let changed = false;
          if (t.dst && !cur.dst) {
            cur.dst = t.dst;
            changed = true;
          }
          if (t.info && !cur.info) {
            cur.info = t.info;
            changed = true;
          }
          if (changed) updated += 1;
        }
      }
      log(`Merged ${incoming.terms.length} terms (${label}): ${added} added, ${updated} updated.`, "good");
    } else {
      if (terms.length) {
        const ok = await ask(
          `The current glossary has ${terms.length} terms. Importing will replace all of them. Continue?\n\nUse Merge Import instead to keep the existing terms.`,
          { title: "Replace the current glossary?", kind: "warning" },
        );
        if (!ok) return;
      }
      terms = incoming.terms;
      log(`Imported ${incoming.terms.length} terms (${label}).`, "good");
    }
    selectedRows.clear();
    renderGlossary();
    runValidation(true);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    log(`[Error] Import failed: ${msg}`, "error");
    await message(msg, { title: "Import failed", kind: "error" });
  }
}

function defaultBasename(suffix: string): string {
  const stem = selectedVn
    ? sanitizeFilename(`${selectedVn.title ?? selectedVn.id}_glossary`)
    : "glossary";
  return `${stem}.${suffix}`;
}

async function confirmExport(count: number): Promise<boolean> {
  if (!count) {
    await message(
      'Every term is either disabled or still untranslated.\nTo export empty translations anyway, untick "Skip untranslated on export".',
      { title: "Nothing to export", kind: "warning" },
    );
    return false;
  }
  const disabled = terms.length - terms.filter((t) => t.enabled).length;
  const untranslated = terms.filter((t) => t.enabled && t.src.trim() && !t.dst.trim()).length;
  let msg = `${count} terms will be exported.`;
  const details: string[] = [];
  if (disabled) details.push(`${disabled} disabled skipped`);
  if (untranslated && settings.skipUntranslated) details.push(`${untranslated} untranslated skipped`);
  if (details.length) msg += `\n\n(${details.join("; ")})`;
  if (settings.exportShape === "dict") msg += "\n\nNote: the key/value format cannot store the info column.";
  return ask(msg + "\n\nContinue?", { title: "Export glossary" });
}

async function exportJson() {
  const rows = exportable();
  if (!(await confirmExport(rows.length))) return;
  const path = await save({
    title: "Export JSON glossary",
    defaultPath: defaultBasename("json"),
    filters: [{ name: "JSON", extensions: ["json"] }],
  });
  if (!path) return;
  try {
    await writeText(path, dumpJson(rows, settings.exportShape));
    log(`Exported ${rows.length} terms to ${path}.`, "good");
    updateStatus(`Exported ${rows.length} terms to ${path.split(/[\\/]/).pop()}`);
    await message(`Exported ${rows.length} terms to:\n${path}`, { title: "Export complete" });
  } catch (e) {
    await message(String(e), { title: "Export failed", kind: "error" });
  }
}

async function exportXlsx() {
  const rows = exportable();
  if (!(await confirmExport(rows.length))) return;
  const path = await save({
    title: "Export XLSX glossary",
    defaultPath: defaultBasename("xlsx"),
    filters: [{ name: "Excel workbook", extensions: ["xlsx"] }],
  });
  if (!path) return;
  try {
    await writeXlsxFile(path, rows);
    log(`Exported ${rows.length} terms to ${path}.`, "good");
    updateStatus(`Exported ${rows.length} terms to ${path.split(/[\\/]/).pop()}`);
    await message(`Exported ${rows.length} terms to:\n${path}`, { title: "Export complete" });
  } catch (e) {
    await message(String(e), { title: "Export failed", kind: "error" });
  }
}

// ---------------------------------------------------------------------------
// presets
// ---------------------------------------------------------------------------

function presetDir(root: string, lang: string): string {
  const sep = root.includes("/") && !root.includes("\\") ? "/" : "\\";
  return `${root}${sep}resource${sep}preset${sep}glossary${sep}${lang}`;
}

async function ensureLinguaRoot(): Promise<string | null> {
  if (settings.linguaGachaRoot) return settings.linguaGachaRoot;
  const dir = await open({ title: "Select the LinguaGacha folder", directory: true });
  if (typeof dir !== "string" || !dir) return null;
  settings.linguaGachaRoot = dir;
  saveSettings();
  return dir;
}

async function choosePresetLang(): Promise<string | null> {
  const root = document.createElement("div");
  root.className = "modal-backdrop";
  root.innerHTML = `
    <div class="modal"><h2>Preset language</h2>
      <label>Preset folder (resource/preset/glossary/&lt;language&gt;):
        <select id="preset-lang" style="width:100%">
          <option value="zh">Chinese (zh)</option>
          <option value="en">English (en)</option>
          <option value="user">User-defined (user)</option>
        </select></label>
      <div class="actions"><button id="m-cancel">Cancel</button>
        <button id="m-ok" class="primary">OK</button></div>
    </div>`;
  document.querySelector("#modal-root")!.appendChild(root);
  const sel = root.querySelector("#preset-lang") as HTMLSelectElement;
  sel.value = settings.presetLang;
  return new Promise((resolve) => {
    const close = (v: string | null) => {
      root.remove();
      resolve(v);
    };
    (root.querySelector("#m-cancel") as HTMLButtonElement).onclick = () => close(null);
    (root.querySelector("#m-ok") as HTMLButtonElement).onclick = () => close(sel.value);
  });
}

async function savePreset() {
  const rows = exportable();
  if (!(await confirmExport(rows.length))) return;
  const root = await ensureLinguaRoot();
  if (!root) return;
  const lang = (await choosePresetLang()) ?? settings.presetLang;
  settings.presetLang = lang;
  saveSettings();
  const dir = presetDir(root, lang);
  await makeDir(dir);

  const stem = selectedVn ? sanitizeFilename(`${selectedVn.title ?? ""}_${selectedVn.id}`) : "glossary";
  const name = await promptModal("Save as LinguaGacha preset", `Location: ${dir}`, `${stem}.json`);
  if (!name) return;

  const existing = (await listDir(dir)).map((e) => e.name ?? "").filter((f) => f.endsWith(".json"));
  let target = presetFileName(existing, name);
  if (existing.includes(target)) {
    const ok = await ask(`${dir}/${target} already exists.\n\nOverwrite it?`, {
      title: "Preset already exists",
      kind: "warning",
    });
    if (!ok) return;
  }
  await writeText(`${dir}/${target}`, dumpJson(rows, "list"));
  log(`Saved preset to ${dir}/${target}`, "good");
  await message(
    `The preset was written to:\n${dir}/${target}\n\nLinguaGacha will offer it as a preset for new projects.`,
    { title: "Preset saved" },
  );
}

function promptModal(title: string, hint: string, initial: string): Promise<string | null> {
  const root = document.createElement("div");
  root.className = "modal-backdrop";
  root.innerHTML = `
    <div class="modal"><h2></h2>
      <div class="dim"></div>
      <input type="text" id="m-name" />
      <div class="actions"><button id="m-cancel">Cancel</button>
        <button id="m-ok" class="primary">Save</button></div>
    </div>`;
  root.querySelector("h2")!.textContent = title;
  (root.querySelector(".dim") as HTMLElement).textContent = hint;
  const input = root.querySelector("#m-name") as HTMLInputElement;
  input.value = initial;
  document.querySelector("#modal-root")!.appendChild(root);
  input.focus();
  input.select();
  return new Promise((resolve) => {
    const close = (v: string | null) => {
      root.remove();
      resolve(v);
    };
    (root.querySelector("#m-cancel") as HTMLButtonElement).onclick = () => close(null);
    (root.querySelector("#m-ok") as HTMLButtonElement).onclick = () =>
      close(input.value.trim() || null);
    input.onkeydown = (e) => {
      if (e.key === "Enter") close(input.value.trim() || null);
      if (e.key === "Escape") close(null);
    };
  });
}

async function loadPreset() {
  const root = await ensureLinguaRoot();
  if (!root) return;
  const lang = (await choosePresetLang()) ?? settings.presetLang;
  settings.presetLang = lang;
  saveSettings();
  const dir = presetDir(root, lang);
  let entries: string[];
  try {
    entries = (await listDir(dir)).map((e) => e.name ?? "").filter((f) => f.endsWith(".json")).sort();
  } catch {
    await message(`Could not read ${dir}.`, { title: "Load failed", kind: "error" });
    return;
  }
  if (!entries.length) {
    await message(`There are no .json presets in ${dir}.`, { title: "Load preset" });
    return;
  }
  const picked = await pickModal("Load LinguaGacha preset", dir, entries);
  if (!picked) return;
  try {
    const { terms: incoming, shape } = parseGlossaryText(await readText(`${dir}/${picked}`));
    if (terms.length) {
      const ok = await ask("Loading a preset will replace the current glossary. Continue?", {
        title: "Replace the current glossary?",
        kind: "warning",
      });
      if (!ok) return;
    }
    terms = incoming;
    selectedRows.clear();
    log(`Loaded preset ${picked}: ${incoming.length} terms (${SHAPE_LABELS[shape]}).`, "good");
    renderGlossary();
    runValidation(true);
  } catch (e) {
    await message(String(e), { title: "Load failed", kind: "error" });
  }
}

function pickModal(title: string, dir: string, entries: string[]): Promise<string | null> {
  const root = document.createElement("div");
  root.className = "modal-backdrop";
  root.innerHTML = `
    <div class="modal"><h2></h2><div class="dim"></div>
      <ul class="pick"></ul>
      <div class="actions"><button id="m-cancel">Cancel</button>
        <button id="m-ok" class="primary">Load</button></div>
    </div>`;
  root.querySelector("h2")!.textContent = title;
  (root.querySelector(".dim") as HTMLElement).textContent = `Folder: ${dir}`;
  const ul = root.querySelector("ul")!;
  let close: (v: string | null) => void = () => undefined;
  entries.forEach((name, i) => {
    const li = document.createElement("li");
    li.textContent = name;
    if (i === 0) li.classList.add("active");
    li.onclick = () => {
      ul.querySelectorAll("li").forEach((x) => x.classList.remove("active"));
      li.classList.add("active");
    };
    li.ondblclick = () => close(name);
    ul.appendChild(li);
  });
  document.querySelector("#modal-root")!.appendChild(root);
  return new Promise((resolve) => {
    close = (v: string | null) => {
      root.remove();
      resolve(v);
    };
    (root.querySelector("#m-cancel") as HTMLButtonElement).onclick = () => close(null);
    (root.querySelector("#m-ok") as HTMLButtonElement).onclick = () =>
      close(ul.querySelector("li.active")?.textContent ?? null);
  });
}

// ---------------------------------------------------------------------------
// wiring
// ---------------------------------------------------------------------------

function bindOptions() {
  const o = settings.options;
  const check = (id: string, get: () => boolean, set: (v: boolean) => void) => {
    const el = $(id) as HTMLInputElement;
    el.checked = get();
    el.addEventListener("change", () => {
      set(el.checked);
      saveSettings();
    });
  };
  const num = (id: string, get: () => number, set: (v: number) => void) => {
    const el = $(id) as HTMLInputElement;
    el.value = String(get());
    el.addEventListener("change", () => {
      const v = Number(el.value);
      if (Number.isFinite(v)) {
        set(v);
        saveSettings();
      }
    });
  };
  check("#opt-aliases", () => o.includeAliases, (v) => (o.includeAliases = v));
  num("#opt-alias-min", () => o.aliasMinLen, (v) => (o.aliasMinLen = v));
  check("#opt-jp-only", () => o.onlyJapaneseOriginal, (v) => (o.onlyJapaneseOriginal = v));
  check("#opt-strip", () => o.stripSpaces, (v) => (o.stripSpaces = v));
  check("#opt-prefill", () => o.prefillDst, (v) => (o.prefillDst = v));
  check("#opt-traits", () => o.includeTraits, (v) => (o.includeTraits = v));
  num("#opt-trait-max", () => o.maxTraits, (v) => (o.maxTraits = v));
  check("#opt-spoiler", () => o.includeSpoilerInfo, (v) => (o.includeSpoilerInfo = v));
  check("#opt-reading", () => o.includeReading, (v) => (o.includeReading = v));

  const shape = $("#export-shape") as HTMLSelectElement;
  shape.value = settings.exportShape;
  shape.addEventListener("change", () => {
    settings.exportShape = shape.value as "list" | "dict";
    saveSettings();
    updateStatus();
  });
  const skip = $("#flt-skip") as HTMLInputElement;
  skip.checked = settings.skipUntranslated;
  skip.addEventListener("change", () => {
    settings.skipUntranslated = skip.checked;
    saveSettings();
    updateStatus();
  });
  ($("#flt-untranslated") as HTMLInputElement).addEventListener("change", (e) => {
    showUntranslatedOnly = (e.target as HTMLInputElement).checked;
    renderGlossary();
  });
  ($("#filter-input") as HTMLInputElement).addEventListener("input", (e) => {
    filterText = (e.target as HTMLInputElement).value;
    renderGlossary();
  });
}

async function init() {
  bindOptions();
  renderVnTable();
  renderGlossary();
  updateStatus("Ready. Search for a VN above, select it, then click Fetch Characters.");

  ($("#btn-search") as HTMLButtonElement).onclick = doSearch;
  ($("#search-input") as HTMLInputElement).addEventListener("keydown", (e) => {
    if (e.key === "Enter") doSearch();
  });
  ($("#btn-by-id") as HTMLButtonElement).onclick = () => {
    const input = $("#search-input") as HTMLInputElement;
    const id = parseVnId(input.value);
    if (!id) {
      void message("Enter a vndbid such as v17 or https://vndb.org/v17", {
        title: "vndbid required",
        kind: "warning",
      });
      return;
    }
    input.value = id;
    void doSearch();
  };
  ($("#btn-open-vndb") as HTMLButtonElement).onclick = () => {
    if (!selectedVn) {
      void message("Select a visual novel in the list first.", { title: "Nothing selected" });
      return;
    }
    void openUrl(`https://vndb.org/${selectedVn.id}`);
  };
  ($("#btn-fetch-append") as HTMLButtonElement).onclick = () => void fetchCharacters("append");
  ($("#btn-fetch-replace") as HTMLButtonElement).onclick = () => void fetchCharacters("replace");

  ($("#btn-import") as HTMLButtonElement).onclick = () => void importFile(false);
  ($("#btn-merge") as HTMLButtonElement).onclick = () => void importFile(true);
  ($("#btn-exp-json") as HTMLButtonElement).onclick = () => void exportJson();
  ($("#btn-exp-xlsx") as HTMLButtonElement).onclick = () => void exportXlsx();
  ($("#btn-save-preset") as HTMLButtonElement).onclick = () => void savePreset();
  ($("#btn-load-preset") as HTMLButtonElement).onclick = () => void loadPreset();

  ($("#btn-add-row") as HTMLButtonElement).onclick = () => {
    const src = filterText.trim();
    if (src && terms.some((t) => normalizeKey(t.src) === normalizeKey(src))) {
      void message("That source term is already in the glossary.", { title: "Duplicate" });
      return;
    }
    terms.push({ src, dst: "", info: "", enabled: true, note: "" });
    renderGlossary();
  };
  ($("#btn-del") as HTMLButtonElement).onclick = () => {
    if (!selectedRows.size) return;
    const gone = new Set(selectedRows);
    terms = terms.filter((_, i) => !gone.has(i));
    log(`Deleted ${gone.size} terms.`);
    selectedRows.clear();
    renderGlossary();
  };
  ($("#btn-dedupe") as HTMLButtonElement).onclick = () => {
    const n = dedupeTerms();
    log(`Removed ${n} duplicate or empty source terms.`);
    selectedRows.clear();
    renderGlossary();
  };
  ($("#btn-validate") as HTMLButtonElement).onclick = () => runValidation(false);

  document.addEventListener("keydown", (e) => {
    if (e.key === "Delete" && selectedRows.size) {
      const gone = new Set(selectedRows);
      terms = terms.filter((_, i) => !gone.has(i));
      log(`Deleted ${gone.size} terms.`);
      selectedRows.clear();
      renderGlossary();
    }
    if (e.key === "F5") {
      e.preventDefault();
      runValidation(false);
    }
  });

  // Sanity check that the Rust backend is reachable.
  try {
    await invoke("ping");
  } catch (e) {
    log(`Backend not reachable: ${e}`, "error");
  }
}

void init();
