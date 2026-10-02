# vndb_glossary

A desktop tool that turns a VNDB visual novel's cast into a
[LinguaGacha](https://github.com/neavo/LinguaGacha)-compatible glossary.

It searches VNDB through the public [Kana API](https://api.vndb.org/kana), fetches
every character (voiced **and** unvoiced — the `va` field alone is not enough),
and lets you edit names into a `src / dst / info` table that LinguaGacha accepts
directly.

The GUI is a [Tauri](https://tauri.app/) app: a TypeScript frontend with a small
Rust backend that proxies VNDB requests (no CORS issues, with retries/back-off)
and handles file I/O. The `*.py` files are the legacy Tkinter implementation and
are kept for reference; the Tauri app in `src/` + `src-tauri/` is the current GUI.

## Requirements

- [Node.js](https://nodejs.org/) 20+
- [Rust](https://www.rust-lang.org/tools/install) stable (via rustup)
- Windows: WebView2 (preinstalled on Windows 10/11) and the MSVC C++ build tools
  (`winget install Rustlang.Rustup` is enough on a machine with Visual Studio)

## Run

```sh
npm install
npm run tauri dev
```

## Build an installer / portable binary

```sh
npm run tauri build
```

Bundles land in `src-tauri/target/release/bundle/`.

## Check the logic without a window

```sh
npm test        # 31 headless checks over the TS glossary core
npx tsc --noEmit
```

## What it produces

The standard LinguaGacha glossary shape:

```json
[
  {"src": "河野 貴明", "dst": "Kouno Takaaki", "info": "Male, Protagonist, ..."},
  {"src": "羽根崎 美緒", "dst": "Hanesaki Mio", "info": "Female, Main character, ..."}
]
```

- `src` — the official Japanese name / alias as it appears in the script
- `dst` — your translation. Pre-filled with VNDB's romanized name by default
  (`河野 貴明` → `Kouno Takaaki`) so you can edit a transliteration rather than
  type one from scratch; overwrite it wherever you want a localized name.
- `info` — machine-generated context for the translator:
  gender / role from VNDB, up to N character traits (round-robin across groups,
  so you get a hair trait **and** an eyes trait instead of four hair traits),
  and a `[Spoiler]` marker when relevant.

## Features

| Area | What you get |
| ---- | ------------ |
| Search | VN search by title/alias or direct vndbid, one-click open on vndb.org |
| Build | Append or replace the cast; aliases filtered by minimum length; only-Japanese-source-names toggle; romanized-name fill of `dst` |
| Edit | In-place cell editing, enable/disable per row, add/delete rows, live filter, untranslated-only filter, sortable columns |
| Validate | Duplicate sources, missing translations, same-as-source, over-long sources, sources with spaces, non-Japanese sources, punctuation in a term |
| Import | LinguaGacha standard list, RPG Maker `Actors.json`, key/value dict, `.xlsx` |
| Export | JSON in standard-list or key/value shape, `.xlsx` |
| Presets | Save straight into `<LinguaGacha>/resource/preset/glossary/<lang>/` and reload them later; `NN_` index prefix is added automatically |

## Files

| Path | Purpose |
| ---- | ------- |
| `src/main.ts` | Tauri GUI (search, table, validation, import/export, presets) |
| `src/styles.css` | App stylesheet (system font stack: Segoe UI, …) |
| `src/lib/vndb.ts` | VNDB query layer (talks to the Rust `vndb_request` command) |
| `src/lib/glossary.ts` | Term model, VNDB→term conversion, validation, JSON import/export |
| `src/lib/xlsx.ts` | XLSX import/export via SheetJS |
| `src/lib/files.ts` | File I/O via Rust commands |
| `src-tauri/src/main.rs` | Rust backend: VNDB proxy with retries + file commands |
| `src-tauri/tauri.conf.json` | App/bundle configuration |
| `scripts/logic_check.ts` | Headless checks (`npm test`) |
| `scripts/gen_icons.py` | Generates the app icons |
| `vndb_glossary.py` etc. | Legacy Tkinter GUI (reference only) |

Settings (endpoint, build options, LinguaGacha folder) persist in `localStorage`.

## Build options that come up often

- **Aliases / nicknames** — on for typical JP name-heavy VNs where shortenings and
  nicknames show up in the script; off for clean, romanization-only casts.
- **Only Japanese source names** — keeps `src` restricted to strings that actually
  contain kana/kanji; turn off for romanized-script games.
- **Strip spaces from source names** — `茜ヶ崎 空` → `茜ヶ崎空`. Useful when the game
  engine drops name spaces, but check the validation notes.
- **Fill dst with romanized name** — on by default. VNDB's romanized name is
  copied straight into `dst`, so `河野 貴明` → `Kouno Takaaki`. Use it as a
  transliteration starting point and overwrite where you want a localized name.

## Notes

- The public API allows ~200 requests per 5 minutes; the client throttles itself
  and retries 429/5xx with back-off. Character lists for big VNs are paginated.
- VNDB `va` (voice actor) entries only cover the *voiced* cast, which is why the
  tool queries `/character` with the nested `vn` filter instead.
- Spoiler-marked roles are only flagged in `info` when the "Flag spoilered
  characters" option is on — nothing about the VN's plot is exposed in the GUI.

## License

MIT. VNDB data remains © VNDB.org; the generated glossary is yours.
