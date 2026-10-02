"""Glossary model, VNDB -> glossary conversion, validation and file I/O.

Everything LinguaGacha is able to import is supported:

* the standard dictionary list ``[{"src": ..., "dst": ..., "info": ...}, ...]``
* RPG Maker ``Actors.json`` ``[{"id": 1, "name": ..., "nickname": ...}, ...]``
* a plain key/value dictionary ``{"src": "dst", ...}``
* ``.xlsx`` workbooks with ``src`` / ``dst`` / ``info`` headers

See https://github.com/neavo/LinguaGacha/wiki/Glossary
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import xlsx_io

# --------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------

#: Hiragana, Katakana, CJK ideographs, half-width katakana.
JAPANESE_RE = re.compile(
    "[\u3040-\u309f\u30a0-\u30ff\u31f0-\u31ff"
    "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]"
)

LATIN_RE = re.compile(r"[A-Za-z]")

GENDER_LABELS = {
    "m": "Male",
    "f": "Female",
    "o": "Non-binary",
    "a": "Ambiguous",
}

ROLE_LABELS = {
    "main": "Protagonist",
    "primary": "Main character",
    "side": "Supporting character",
    "appears": "Cameo only",
}

#: Keys accepted for each logical field when importing a foreign file.
#: Header/field names accepted on import. Chinese aliases are included because
#: LinguaGacha's Chinese presets and many community glossaries use them.
_SRC_KEYS = ("src", "source", "original", "key", "term", "原文", "源文", "日文", "名前")
_DST_KEYS = ("dst", "dest", "destination", "translation", "target", "value", "译文", "翻译", "中文", "訳")
_INFO_KEYS = ("info", "note", "notes", "comment", "commentary", "description", "remark", "描述", "备注", "说明")
_NICK_KEYS = ("nickname", "nick", "别名", "昵称")
_NAME_KEYS = ("name", "姓名", "名字", "名前")

ERROR, WARNING, INFO = "error", "warning", "info"
LEVEL_LABELS = {ERROR: "Error", WARNING: "Warning", INFO: "Note"}
LEVEL_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}


def has_japanese(text: str) -> bool:
    return bool(JAPANESE_RE.search(text or ""))


def has_latin(text: str) -> bool:
    return bool(LATIN_RE.search(text or ""))


def normalize_key(text: str) -> str:
    """Case/width/whitespace-insensitive key used for duplicate detection."""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\u3000", " ").replace("\u00a0", " ")
    return re.sub(r"\s+", "", text).casefold()


def strip_spaces(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", "", text)


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------


@dataclass
class Term:
    """A single glossary entry.

    ``note`` is local bookkeeping (which VNDB entry it came from) and is never
    written out, because LinguaGacha only understands ``src``/``dst``/``info``.
    """

    src: str = ""
    dst: str = ""
    info: str = ""
    enabled: bool = True
    note: str = ""
    source_id: str = ""

    def to_export_dict(self, shape: str = "list") -> Any:
        if shape == "dict":
            return {self.src: self.dst}
        if shape == "actors":
            return {"id": 0, "name": self.src, "nickname": self.dst}
        return {"src": self.src, "dst": self.dst, "info": self.info}

    def as_row(self) -> list[str]:
        return [self.src, self.dst, self.info]


@dataclass
class Issue:
    level: str
    message: str
    index: int | None = None


class Glossary:
    """An ordered, de-duplicated list of :class:`Term` objects."""

    def __init__(self, terms: Iterable[Term] | None = None) -> None:
        self._terms: list[Term] = list(terms or [])
        self._index: dict[str, int] = {}
        self._reindex()

    # -- container protocol -------------------------------------------
    def __len__(self) -> int:
        return len(self._terms)

    def __iter__(self):
        return iter(self._terms)

    def __getitem__(self, index):
        return self._terms[index]

    # -- mutation -----------------------------------------------------
    def add(self, term: Term) -> int | None:
        """Append ``term`` unless its ``src`` duplicates an existing entry.

        An empty ``src`` is allowed (the user may be about to fill it in), but
        empty sources never collide with each other. Returns the new position,
        or ``None`` when it was a duplicate.
        """
        key = normalize_key(term.src)
        if key and key in self._index:
            return None
        self._terms.append(term)
        if key:
            self._index[key] = len(self._terms) - 1
        return len(self._terms) - 1

    def add_many(self, terms: Iterable[Term]) -> tuple[int, int]:
        """Add every term; returns ``(added, duplicates)``."""
        added = duplicates = 0
        for term in terms:
            if self.add(term) is None:
                duplicates += 1
            else:
                added += 1
        return added, duplicates

    def replace(self, terms: Iterable[Term]) -> None:
        self._terms = [t for t in terms]
        self._index = {}
        for position, term in enumerate(self._terms):
            self._index.setdefault(normalize_key(term.src), position)

    def remove(self, index: int) -> None:
        del self._terms[index]
        self._reindex()

    def set_field(self, index: int, field_name: str, value: str) -> None:
        if field_name == "enabled":
            self._terms[index].enabled = bool(value)
            return
        setattr(self._terms[index], field_name, value)
        if field_name == "src":
            self._reindex()

    def _reindex(self) -> None:
        self._index = {}
        for position, term in enumerate(self._terms):
            key = normalize_key(term.src)
            if key:
                self._index.setdefault(key, position)

    def index_of(self, src: str) -> int | None:
        return self._index.get(normalize_key(src))

    # -- merging ------------------------------------------------------
    def merge_imported(self, incoming: Iterable[Term], overwrite: bool = False) -> tuple[int, int, int]:
        """Merge an imported glossary into this one.

        Returns ``(added, updated, unchanged)``.
        """
        added = updated = unchanged = 0
        for term in incoming:
            existing = self.index_of(term.src)
            if existing is None:
                self.add(term)
                added += 1
                continue
            current = self._terms[existing]
            changed = False
            if term.dst and (not current.dst or overwrite):
                if current.dst != term.dst:
                    current.dst = term.dst
                    changed = True
            if term.info and (not current.info or overwrite):
                if current.info != term.info:
                    current.info = term.info
                    changed = True
            updated += changed
            unchanged += not changed
        return added, updated, unchanged

    def dedupe(self) -> int:
        """Drop later duplicates of the same ``src``; returns rows removed."""
        seen: set[str] = set()
        kept: list[Term] = []
        removed = 0
        for term in self._terms:
            key = normalize_key(term.src)
            if not key or key in seen:
                removed += 1
                continue
            seen.add(key)
            kept.append(term)
        self.replace(kept)
        return removed

    # -- views --------------------------------------------------------
    def enabled_terms(self) -> list[Term]:
        return [t for t in self._terms if t.enabled]

    def exportable(self, require_dst: bool = True) -> list[Term]:
        terms = []
        for term in self.enabled_terms():
            if not term.src.strip():
                continue
            if require_dst and not term.dst.strip():
                continue
            terms.append(term)
        return terms

    def stats(self) -> dict[str, int]:
        enabled = self.enabled_terms()
        return {
            "total": len(self._terms),
            "enabled": len(enabled),
            "untranslated": sum(1 for t in enabled if not t.dst.strip()),
        }

    # -- validation ---------------------------------------------------
    def validate(self, max_src_len: int = 20) -> list[Issue]:
        """Run all quality checks and return issues sorted by severity."""
        issues: list[Issue] = []
        seen: dict[str, int] = {}

        for index, term in enumerate(self._terms):
            src, dst = term.src.strip(), term.dst.strip()
            if not term.enabled:
                continue
            if not src:
                issues.append(Issue(ERROR, f"Row {index + 1}: source term is empty.", index))
                continue

            key = normalize_key(src)
            if key in seen:
                issues.append(
                    Issue(WARNING, f"Row {index + 1}: source \"{src}\" duplicates row {seen[key] + 1}.", index)
                )
            else:
                seen[key] = index

            if not dst:
                issues.append(Issue(WARNING, f"Row {index + 1}: \"{src}\" has no translation yet.", index))
            elif dst == src:
                issues.append(Issue(INFO, f"Row {index + 1}: \"{src}\" is translated as itself.", index))

            if len(src) > max_src_len:
                issues.append(
                    Issue(INFO, f"Row {index + 1}: source is long ({len(src)} chars); "
                                "glossaries usually only hold proper nouns.", index)
                )
            if any(ch.isspace() for ch in src):
                issues.append(
                    Issue(INFO, f"Row {index + 1}: \"{src}\" contains a space; the game may use "
                                "the unspaced form.", index)
                )
            if not has_japanese(src) and has_latin(src):
                issues.append(
                    Issue(INFO, f"Row {index + 1}: \"{src}\" is not Japanese. Ignore this if the "
                                "source language is not Japanese.", index)
                )
            if re.search(r"[，。！？、；：\n\r]", src):
                issues.append(
                    Issue(WARNING, f"Row {index + 1}: \"{src}\" contains punctuation or a line break; "
                                   "consider splitting it into separate terms.", index)
                )
        return sorted(issues, key=lambda i: (LEVEL_ORDER[i.level], i.index or 0))


# --------------------------------------------------------------------------
# VNDB -> glossary
# --------------------------------------------------------------------------


@dataclass
class BuildOptions:
    """Toggles that control how characters are turned into glossary rows."""

    include_aliases: bool = True
    alias_min_len: int = 2
    only_japanese_original: bool = True
    strip_spaces: bool = False
    prefill_dst: bool = True
    include_traits: bool = True
    max_traits: int = 4
    include_spoiler_info: bool = True
    #: VNDB reports gender/sex as [apparent, real]. Prefer the real (spoiler)
    #: value so the glossary works like VNDB's "spoil me" view.
    prefer_spoiler_values: bool = True
    #: The romanized name now goes into ``dst`` by default, so repeating it in
    #: ``info`` is normally redundant.
    include_reading: bool = False
    dedupe_alias_of_original: bool = True

    @classmethod
    def for_language(cls, target_lang: str) -> "BuildOptions":
        """Sensible defaults for a given target language."""
        if target_lang.startswith("zh"):
            return cls(include_aliases=True, alias_min_len=2, prefill_dst=True)
        if target_lang.startswith("ja"):
            return cls(include_aliases=False, alias_min_len=3, only_japanese_original=False,
                       prefill_dst=True)
        return cls(include_aliases=True, alias_min_len=2, prefill_dst=True)


def _pick_role(character: dict[str, Any], vn_id: str) -> tuple[str, int]:
    """Return ``(role, spoiler)`` for this character in this VN."""
    best: tuple[int, str] | None = None
    for entry in character.get("vns") or []:
        if entry.get("id") != vn_id:
            continue
        candidate = (int(entry.get("spoiler") or 0), entry.get("role") or "")
        if best is None or candidate[0] < best[0]:
            best = candidate
    if best is None:
        return "", 0
    return best[1], best[0]


def _pick_traits(traits: Sequence[dict[str, Any]], limit: int) -> tuple[list[str], int]:
    """Choose up to ``limit`` traits, at most one per group before repeating.

    VNDB returns traits by rating, so a plain top-N slice is usually four
    variations of hair colour. Round-robin over the groups is far more useful
    as a translation hint.
    """
    buckets: dict[str, list[str]] = {}
    total = 0
    for trait in traits or []:
        name = (trait.get("name") or "").strip()
        if not name:
            continue
        total += 1
        key = (trait.get("group_name") or "").strip()
        bucket = buckets.setdefault(key, [])
        if name not in bucket:
            bucket.append(name)
    if not buckets or limit <= 0:
        return [], total

    picked: list[str] = []
    while len(picked) < limit and any(buckets.values()):
        for key, names in buckets.items():
            if not names:
                continue
            value = names.pop(0)
            # 'Brown' alone is ambiguous; 'Eyes: Brown' is not.
            picked.append(f"{key}: {value}" if key else value)
            if len(picked) >= limit:
                break
    return picked, total


def _character_info(
    character: dict[str, Any],
    role: str,
    spoiler: int,
    options: BuildOptions,
    suffix: str = "",
) -> str:
    parts: list[str] = []
    gender_values = character.get("gender") or [None]
    if options.prefer_spoiler_values and len(gender_values) > 1 and gender_values[1]:
        gender = gender_values[1]
    else:
        gender = gender_values[0]
    label = GENDER_LABELS.get(gender or "")
    if label:
        parts.append(label)
    if role:
        parts.append(ROLE_LABELS.get(role, role))
    if spoiler >= 2 and options.include_spoiler_info:
        parts.append("[Spoiler]")

    if options.include_traits:
        shown, total = _pick_traits(character.get("traits") or [], options.max_traits)
        if shown:
            if total > len(shown):
                shown.append(f"... {total} total")
            parts.append("Traits: " + " / ".join(shown))

    if options.include_reading:
        name = (character.get("name") or "").strip()
        if name:
            parts.append(f"Romanization: {name}")

    if suffix:
        parts.append(suffix)
    return ", ".join(p for p in parts if p)


def terms_from_character(
    character: dict[str, Any],
    vn_id: str,
    options: BuildOptions | None = None,
) -> list[Term]:
    """Turn one VNDB character into glossary rows (name + aliases)."""
    options = options or BuildOptions()
    name = (character.get("name") or "").strip()
    original = (character.get("original") or "").strip()
    role, spoiler = _pick_role(character, vn_id)
    character_id = character.get("id", "")
    base_info = _character_info(character, role, spoiler, options)
    terms: list[Term] = []

    def make(src: str, alias_of: str = "") -> Term | None:
        src = strip_spaces(src) if options.strip_spaces else src.strip()
        if not src:
            return None
        if options.only_japanese_original and not has_japanese(src):
            return None
        if alias_of:
            alias_note = f"Alias of {alias_of}"
            info = f"{base_info}, {alias_note}" if base_info else alias_note
        else:
            info = base_info
        return Term(
            src=src,
            dst=name if options.prefill_dst else "",
            info=info,
            note=f"vndb:{character_id}",
            source_id=character_id,
        )

    if original:
        term = make(original)
        if term:
            terms.append(term)

    if options.include_aliases:
        for alias in character.get("aliases") or []:
            alias = (alias or "").strip()
            if not alias:
                continue
            if options.dedupe_alias_of_original and normalize_key(alias) == normalize_key(original):
                continue
            if normalize_key(alias) == normalize_key(name):
                continue
            if len(strip_spaces(alias)) < max(1, options.alias_min_len):
                continue
            term = make(alias, alias_of=name)
            if term:
                terms.append(term)

    return terms


def terms_from_vn(
    vn: dict[str, Any],
    characters: Sequence[dict[str, Any]],
    options: BuildOptions | None = None,
) -> tuple[list[Term], dict[str, int]]:
    """Convert a VN and its cast into glossary rows.

    Returns ``(terms, stats)`` where ``stats`` reports how much was skipped.
    """
    options = options or BuildOptions()
    vn_id = vn.get("id", "")
    terms: list[Term] = []
    stats = {"characters": len(characters), "terms": 0, "no_original": 0}

    ordered = sorted(characters, key=lambda c: (_role_rank(c, vn_id), (c.get("name") or "")))
    for character in ordered:
        if not (character.get("original") or "").strip():
            stats["no_original"] += 1
        produced = terms_from_character(character, vn_id, options)
        stats["terms"] += len(produced)
        terms.extend(produced)

    deduped: list[Term] = []
    seen: set[str] = set()
    for term in terms:
        key = normalize_key(term.src)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(term)
    stats["terms"] = len(deduped)
    return deduped, stats


def _role_rank(character: dict[str, Any], vn_id: str) -> tuple[int, int]:
    role, spoiler = _pick_role(character, vn_id)
    order = {"main": 0, "primary": 1, "side": 2, "appears": 3}
    return order.get(role, 4), spoiler


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------

SHAPE_LIST = "list"
SHAPE_DICT = "dict"
SHAPE_ACTORS = "actors"

SHAPE_LABELS = {
    SHAPE_LIST: "standard dictionary list",
    SHAPE_DICT: "key/value dictionary",
    SHAPE_ACTORS: "RPG Maker Actors.json",
}

_HEADER_ALIASES = {
    "src": "src", "source": "src", "original": "src", "key": "src", "term": "src",
    "原文": "src", "源文": "src", "日文": "src", "名前": "src",
    "dst": "dst", "dest": "dst", "translation": "dst", "target": "dst", "value": "dst",
    "译文": "dst", "翻译": "dst", "中文": "dst", "訳": "dst",
    "info": "info", "note": "info", "notes": "info", "comment": "info",
    "description": "info", "remark": "info",
    "描述": "info", "备注": "info", "说明": "info",
}


def _first(mapping: dict[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return str(mapping[key])
    return ""


def _terms_from_records(records: Sequence[Any]) -> tuple[list[Term], str]:
    """Detect which LinguaGacha import shape ``records`` uses."""
    records = [r for r in records if r is not None]
    if not records:
        return [], SHAPE_LIST

    first = records[0]

    if isinstance(first, (list, tuple)):
        # src / dst / info tuples.
        terms = []
        for record in records:
            if not isinstance(record, (list, tuple)) or len(record) < 2:
                continue
            src = str(record[0]).strip()
            if src:
                terms.append(Term(
                    src=src,
                    dst=str(record[1]).strip(),
                    info=str(record[2]).strip() if len(record) > 2 else "",
                ))
        return terms, SHAPE_LIST

    if not isinstance(first, dict):
        raise ValueError("Unrecognised structure: list items are neither objects nor arrays.")

    # RPG Maker Actors.json
    if "src" not in first and any(key in first for key in _NAME_KEYS):
        terms = []
        for record in records:
            if not isinstance(record, dict):
                continue
            src = _first(record, _NAME_KEYS)
            dst = _first(record, _NICK_KEYS)
            if src:
                terms.append(Term(src=src.strip(), dst=dst.strip(),
                                  info=_first(record, _INFO_KEYS)))
        return terms, SHAPE_ACTORS

    # Standard dictionary list
    terms = []
    for record in records:
        if isinstance(record, dict):
            src, dst = _first(record, _SRC_KEYS), _first(record, _DST_KEYS)
            info = _first(record, _INFO_KEYS)
        else:
            continue
        if src:
            terms.append(Term(src=src.strip(), dst=dst.strip(), info=info))
    return terms, SHAPE_LIST


def load_glossary_file(path: str | Path) -> tuple[list[Term], str]:
    """Load any glossary file LinguaGacha can import.

    Returns ``(terms, shape)``. ``shape`` is one of :data:`SHAPE_LIST`,
    :data:`SHAPE_DICT` or :data:`SHAPE_ACTORS`.
    """
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix == ".xlsx":
        return _load_xlsx(path)
    if suffix not in {".json", ".js", ".txt", ""}:
        raise ValueError(f"Unsupported file type: {suffix} (expected .json or .xlsx)")

    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        raise ValueError("The file is empty.")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Could not parse JSON: {exc}") from exc

    if isinstance(data, list):
        return _terms_from_records(data)
    if isinstance(data, dict):
        # A wrapper object such as {"glossary": [...]} is not a documented
        # shape, but is a natural thing for users to have.
        if all(isinstance(v, str) for v in data.values()):
            terms = [Term(src=str(k), dst=v) for k, v in data.items()]
            return terms, SHAPE_DICT
        for key in ("glossary", "terms", "entries", "data"):
            if isinstance(data.get(key), list):
                return _terms_from_records(data[key])
        terms = []
        for key, value in data.items():
            if isinstance(value, str):
                terms.append(Term(src=str(key), dst=value))
            elif isinstance(value, dict):
                terms.append(Term(
                    src=str(key),
                    dst=_first(value, _DST_KEYS),
                    info=_first(value, _INFO_KEYS),
                ))
        return terms, SHAPE_DICT
    raise ValueError("Unrecognised file structure.")


def _load_xlsx(path: Path) -> tuple[list[Term], str]:
    rows = xlsx_io.read_xlsx(path)
    if not rows:
        return [], SHAPE_LIST

    header = [str(cell or "").strip() for cell in rows[0]]
    mapping = {_HEADER_ALIASES.get(cell.strip().lower(), ""): position for position, cell in enumerate(header)}
    if "src" in mapping and "dst" in mapping:
        columns = {role: mapping[role] for role in ("src", "dst", "info") if role in mapping}
        terms = []
        for row in rows[1:]:
            src = str(row[columns["src"]] or "").strip() if columns["src"] < len(row) else ""
            if not src:
                continue
            dst = str(row[columns["dst"]] or "").strip() if "dst" in columns and columns["dst"] < len(row) else ""
            info = str(row[columns["info"]] or "").strip() if "info" in columns and columns["info"] < len(row) else ""
            terms.append(Term(src=src, dst=dst, info=info))
        return terms, SHAPE_LIST

    # No header row -> assume the first three columns are src/dst/info.
    terms = []
    for row in rows:
        src = str(row[0] or "").strip() if len(row) > 0 else ""
        if not src:
            continue
        dst = str(row[1] or "").strip() if len(row) > 1 else ""
        info = str(row[2] or "").strip() if len(row) > 2 else ""
        terms.append(Term(src=src, dst=dst, info=info))
    return terms, SHAPE_LIST


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------

EXPORT_SHAPES = {
    SHAPE_LIST: 'standard list  [{"src", "dst", "info"}]',
    SHAPE_DICT: 'key/value dict  {"src": "dst"}  (info is lost)',
}


def dump_json(terms: Sequence[Term], shape: str = SHAPE_LIST, indent: int | None = 2) -> str:
    if shape == SHAPE_DICT:
        payload: Any = {term.src: term.dst for term in terms}
    else:
        payload = [term.to_export_dict(shape) for term in terms]
    return json.dumps(payload, ensure_ascii=False, indent=indent) + "\n"


def write_json(path: str | Path, terms: Sequence[Term], shape: str = SHAPE_LIST, indent: int | None = 2) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_json(terms, shape, indent), encoding="utf-8")
    return path


def write_xlsx(path: str | Path, terms: Sequence[Term], include_enabled_column: bool = True) -> Path:
    path = Path(path)
    header = ["src", "dst", "info"]
    rows = [header]
    for term in terms:
        row = term.as_row()
        if include_enabled_column:
            row = row + ["Yes" if term.enabled else "No"]
        rows.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    return xlsx_io.write_xlsx(path, rows, sheet_name="Glossary")


# --------------------------------------------------------------------------
# LinguaGacha presets
# --------------------------------------------------------------------------

PRESET_LANGS = ("zh", "en", "user")
DEFAULT_PRESET_LANG = "zh"


def preset_dir(lingua_gacha_root: str | Path, lang: str = DEFAULT_PRESET_LANG) -> Path:
    """``<LinguaGacha>/resource/preset/glossary/<lang>``."""
    lang = lang if lang in PRESET_LANGS else DEFAULT_PRESET_LANG
    return Path(lingua_gacha_root) / "resource" / "preset" / "glossary" / lang


def list_presets(lingua_gacha_root: str | Path, lang: str = DEFAULT_PRESET_LANG) -> list[Path]:
    directory = preset_dir(lingua_gacha_root, lang)
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.suffix.lower() == ".json" and p.is_file())


def save_preset(
    lingua_gacha_root: str | Path,
    filename: str,
    terms: Sequence[Term],
    lang: str = DEFAULT_PRESET_LANG,
    overwrite: bool = False,
) -> Path:
    """Write a preset into LinguaGacha's preset folder.

    Filenames in that folder are conventionally ``NN_说明.json``; a numeric
    prefix is added automatically when missing so presets sort correctly.
    """
    directory = preset_dir(lingua_gacha_root, lang)
    directory.mkdir(parents=True, exist_ok=True)

    name = Path(filename).name
    if not name.lower().endswith(".json"):
        name += ".json"
    stem = name[:-5]

    if not re.match(r"^\d{2}_", name):
        # A preset with this stem may already exist under a different index.
        matches = sorted(directory.glob(f"*_{stem}.json"))
        if matches:
            if not overwrite:
                raise FileExistsError(f"A preset already exists at {matches[0]}")
            matches[0].write_text(dump_json(terms, SHAPE_LIST), encoding="utf-8")
            return matches[0]
        used = [
            int(p.name[:2]) for p in directory.glob("*.json") if re.match(r"^\d{2}_", p.name)
        ]
        name = f"{(max(used) + 1) if used else 1:02d}_{stem}.json"

    target = directory / name
    if target.exists() and not overwrite:
        raise FileExistsError(f"A preset already exists at {target}")
    target.write_text(dump_json(terms, SHAPE_LIST), encoding="utf-8")
    return target


def sanitize_filename(text: str) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (text or "").strip())
    text = text.strip(". ")
    return text[:80] or "glossary"