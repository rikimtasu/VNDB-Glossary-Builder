"""Smoke tests: no third-party deps, no GUI required.

    python selftest.py [--offline]

``--offline`` skips the live VNDB calls and uses a bundled fixture instead.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import glossary_core as core
import xlsx_io
from vndb_client import VndbClient, parse_vn_id

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(f"{name} {detail}")


# --------------------------------------------------------------------------


def test_parse_ids() -> None:
    print("\n[vndbid parsing]")
    check("plain id", parse_vn_id("v17") == "v17")
    check("bare number", parse_vn_id("17") == "v17")
    check("url", parse_vn_id("https://vndb.org/v17") == "v17")
    check("release url", parse_vn_id("https://vndb.org/r1234") == "v1234")
    check("garbage", parse_vn_id("ever17") is None)


def test_build_terms() -> None:
    print("\n[characters -> terms]")
    vn = {"id": "v17", "title": "Ever17"}
    characters = [
        {
            "id": "c32",
            "name": "Akanegasaki Sora",
            "original": "茜ヶ崎 空",
            "aliases": ["Sorapi", "空ピー", "Y"],
            "gender": ["f", "f"],
            "vns": [{"id": "v17", "role": "primary", "spoiler": 0}],
            "traits": [
                {"name": "Brown", "group_name": "Hair"},
                {"name": "Long", "group_name": "Hair"},
                {"name": "Long", "group_name": "Hair"},
                {"name": "Green", "group_name": "Eyes"},
                {"name": "Slim", "group_name": "Body"},
            ],
        },
        {
            "id": "c24",
            "name": "Kuranari Takeshi",
            "original": "倉成 武",
            "aliases": [],
            "gender": ["m", "m"],
            "vns": [{"id": "v17", "role": "main", "spoiler": 2}],
            "traits": [],
        },
        {
            "id": "c99",
            "name": "Nobody",
            "original": "",
            "aliases": ["Ghost"],
            "gender": [None, None],
            "vns": [],
            "traits": [],
        },
    ]
    options = core.BuildOptions(max_traits=4)
    terms, stats = core.terms_from_vn(vn, characters, options)

    srcs = [t.src for t in terms]
    check("main characters come first", srcs[0] == "倉成 武", srcs)
    check("japanese original kept", "茜ヶ崎 空" in srcs)
    check("no-original character skipped", "Ghost" not in srcs and "Nobody" not in srcs)
    check("single-char alias dropped", "Y" not in srcs)
    check("japanese alias kept", "空ピー" in srcs, srcs)
    check("latin alias dropped by japanese filter", "Sorapi" not in srcs, srcs)
    check("duplicate traits collapsed", "Long/Long" not in (srcs[0] if srcs else ""))

    sora = next(t for t in terms if t.src == "茜ヶ崎 空")
    check("gender in info", "Female" in sora.info, sora.info)
    check("role in info", "Main character" in sora.info, sora.info)
    check("romanization kept out of info by default",
          "Romanization:" not in sora.info, sora.info)
    check("dst pre-filled with romanization by default", sora.dst == "Akanegasaki Sora", sora.dst)
    with_reading = core.BuildOptions(include_reading=True)
    terms_rw, _ = core.terms_from_vn(vn, characters, with_reading)
    check("romanization can be added to info",
          "Romanization: Akanegasaki Sora"
          in next(t for t in terms_rw if t.src == "茜ヶ崎 空").info)
    check("alias rows reuse the character name", next(t for t in terms if t.src == "空ピー").dst
          == "Akanegasaki Sora")
    check("info is ascii except the term itself",
          all(ord(c) < 0x2500 for c in sora.info), sora.info)
    check("traits grouped and diversified", "Hair: Brown" in sora.info and "Eyes: Green" in sora.info, sora.info)
    check("trait duplicates collapsed", sora.info.count("Hair: Long") == 1, sora.info)

    takeshi = next(t for t in terms if t.src == "倉成 武")
    check("spoiler marker", "[Spoiler]" in takeshi.info, takeshi.info)
    check("main role", "Protagonist" in takeshi.info, takeshi.info)

    # VNDB gender is [apparent, real]: defaults prefer the true (spoiler) value.
    secretive = {
        "id": "c99", "name": "Hidden One", "original": "隠し子", "aliases": [],
        "gender": ["a", "f"], "vns": [{"id": "v17", "role": "side", "spoiler": 0}],
        "traits": [],
    }
    shown, _ = core.terms_from_vn(vn, [secretive], core.BuildOptions())
    check("spoiler gender shown by default", "Female" in shown[0].info, shown[0].info)
    veiled, _ = core.terms_from_vn(
        vn, [secretive], core.BuildOptions(prefer_spoiler_values=False))
    check("apparent gender when opted out", "Ambiguous" in veiled[0].info, veiled[0].info)

    alias = next(t for t in terms if t.src == "空ピー")
    check("alias info annotated", "Alias of" in alias.info, alias.info)

    check("stats count", stats["no_original"] == 1, stats)

    # option toggles
    opts = core.BuildOptions(strip_spaces=True, only_japanese_original=True)
    terms2, _ = core.terms_from_vn(vn, characters, opts)
    stripped = next(t for t in terms2 if t.src == "茜ヶ崎空")
    check("spaces stripped", stripped.src == "茜ヶ崎空")
    check("dst still prefilled", stripped.dst == "Akanegasaki Sora")

    blank = core.BuildOptions(prefill_dst=False)
    terms_blank, _ = core.terms_from_vn(vn, characters, blank)
    check("prefill can be turned off",
          all(not t.dst for t in terms_blank),
          [t.dst for t in terms_blank if t.dst][:3])

    latin_only = core.BuildOptions(only_japanese_original=False, include_aliases=True, alias_min_len=1)
    terms3, _ = core.terms_from_vn(vn, characters, latin_only)
    check("non-japanese included when allowed", any(t.src == "Sorapi" for t in terms3))


def test_glossary_model() -> None:
    print("\n[glossary model]")
    g = core.Glossary()
    g.add(core.Term(src="茜ヶ崎 空", dst="茜崎空"))
    check("duplicate rejected", g.add(core.Term(src=" 茜ヶ崎 空 ")) is None)
    check("empty src allowed", g.add(core.Term(src="   ")) is not None)
    check("second empty src allowed", g.add(core.Term(src="")) is not None)
    g.dedupe()

    g.add(core.Term(src="倉成武", dst="仓成武"))
    added, updated, unchanged = g.merge_imported(
        [core.Term(src="茜ヶ崎 空", dst="茜崎 空", info="女性"),
         core.Term(src="田中 優", dst="田中优")]
    )
    check("merge updated", updated == 1, (added, updated, unchanged))
    check("merge added", added == 1)
    check("merged info", g[0].info == "女性", g[0].info)
    check("len", len(g) == 3, len(g))

    check("nothing to dedupe", g.dedupe() == 0)
    g.replace([
        core.Term(src="a", dst="1"),
        core.Term(src="A", dst="2"),
        core.Term(src="b", dst="3"),
        core.Term(src="", dst="4"),
    ])
    check("dedupe removes dupes + empty src", g.dedupe() == 2, len(g))
    check("after dedupe", len(g) == 2, len(g))
    g.replace([
        core.Term(src="茜ヶ崎 空", dst="茜崎空"),
        core.Term(src="倉成 武", dst="仓成武"),
        core.Term(src="田中 優", dst="田中优"),
    ])

    g[0].enabled = False
    check("export skips disabled", len(g.exportable(require_dst=False)) == 2)
    g[0].enabled = True
    g[0].dst = ""
    check("export skips untranslated", len(g.exportable(require_dst=True)) == 2)
    check("export keeps when not required", len(g.exportable(require_dst=False)) == 3)
    check("stats", g.stats()["untranslated"] == 1, g.stats())


def test_validation() -> None:
    print("\n[validation]")
    g = core.Glossary()
    g.add(core.Term(src="", dst="x"))
    g.add(core.Term(src="花", dst=""))
    g.add(core.Term(src="茜ヶ崎 空", dst="茜崎空"))
    g.add(core.Term(src="茜ヶ崎空", dst="茜崎空"))  # duplicate after normalisation
    g.add(core.Term(src="Master", dst="Master"))
    g.add(core.Term(src="おわり Baking 오brigatorio", dst="end"))
    issues = g.validate()
    levels = [i.level for i in issues]
    check("empty src is error", core.ERROR in levels)
    check("empty dst is warning", core.WARNING in levels)
    check("no-op flagged", any(i.level == core.INFO and "translated as itself" in i.message for i in issues))
    # Messages may quote the Japanese term itself, so strip quoted spans first.
    quoted = re.compile(r"\"[^\"]*\"")
    check("message prose is english",
          not any("\u4e00" <= c <= "\u9fff"
                  for i in issues
                  for c in quoted.sub("", i.message)))
    check("issues carry row numbers", all(i.index is not None for i in issues))
    check("sorted by severity", levels == sorted(levels, key=lambda l: core.LEVEL_ORDER[l]), levels)


def test_roundtrip_json() -> None:
    print("\n[json export / import]")
    terms = [
        core.Term(src="茜ヶ崎 空", dst="茜崎空", info="女性，主要角色"),
        core.Term(src="倉成 武", dst="仓成武", info="男性，主角"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        p1 = tmp / "list.json"
        core.write_json(p1, terms, shape=core.SHAPE_LIST)
        data = json.loads(p1.read_text(encoding="utf-8"))
        check("list shape", isinstance(data, list) and set(data[0]) == {"src", "dst", "info"}, data[:1])
        check("unicode not escaped", "茜ヶ崎 空" in p1.read_text(encoding="utf-8"))
        back, shape = core.load_glossary_file(p1)
        check("list roundtrip", shape == core.SHAPE_LIST and back[0].src == terms[0].src)
        check("list roundtrip dst", back[1].dst == "仓成武" and back[1].info == terms[1].info)

        p2 = tmp / "dict.json"
        core.write_json(p2, terms, shape=core.SHAPE_DICT)
        data2 = json.loads(p2.read_text(encoding="utf-8"))
        check("dict shape", isinstance(data2, dict) and data2["茜ヶ崎 空"] == "茜崎空", data2)
        back2, shape2 = core.load_glossary_file(p2)
        check("dict roundtrip", shape2 == core.SHAPE_DICT and len(back2) == 2)

        # foreign key names
        p3 = tmp / "cn.json"
        p3.write_text(json.dumps([{"原文": "花", "译文": "花", "描述": "角色"}], ensure_ascii=False), encoding="utf-8")
        back3, _ = core.load_glossary_file(p3)
        check("chinese keys", back3[0].src == "花" and back3[0].dst == "花" and back3[0].info == "角色")

        p3b = tmp / "cn2.json"
        p3b.write_text(json.dumps([{"源文": "水", "翻译": "水", "备注": "n"}], ensure_ascii=False), encoding="utf-8")
        back3b, _ = core.load_glossary_file(p3b)
        check("alt chinese keys", back3b[0].src == "水" and back3b[0].dst == "水" and back3b[0].info == "n")

        # RPG Maker Actors.json
        p4 = tmp / "Actors.json"
        p4.write_text(json.dumps([{"id": 1, "name": "アルテ", "nickname": "小アル"}]), encoding="utf-8")
        back4, shape4 = core.load_glossary_file(p4)
        check("actors shape", shape4 == core.SHAPE_ACTORS, shape4)
        check("actors values", back4[0].src == "アルテ" and back4[0].dst == "小アル")

        # BOM + tuple rows
        p5 = tmp / "bom.json"
        p5.write_bytes(json.dumps([["あ", "啊", "note"]], ensure_ascii=False).encode("utf-8-sig"))
        back5, _ = core.load_glossary_file(p5)
        check("bom + tuple rows", back5[0].src == "あ" and back5[0].dst == "啊")

        # wrapped object
        p6 = tmp / "wrapped.json"
        p6.write_text(json.dumps({"glossary": [{"src": "x", "dst": "y"}]}), encoding="utf-8")
        back6, _ = core.load_glossary_file(p6)
        check("wrapped object", back6[0].src == "x")

        # bad input
        p7 = tmp / "bad.json"
        p7.write_text("{not json", encoding="utf-8")
        try:
            core.load_glossary_file(p7)
            check("bad json raises", False)
        except ValueError:
            check("bad json raises", True)


def test_xlsx() -> None:
    print("\n[xlsx roundtrip]")
    terms = [
        core.Term(src="茜ヶ崎 空", dst="茜崎空", info='含有 " 引号 & <标签>\n换行'),
        core.Term(src="倉成 武", dst="仓成武", info="男性，主角"),
        core.Term(src="", dst="", info=""),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "out.xlsx"
        core.write_xlsx(path, terms)
        check("file created", path.is_file() and path.stat().st_size > 0)

        rows = xlsx_io.read_xlsx(path)
        check("header", rows[0][:3] == ["src", "dst", "info"], rows[0])
        check("row count", len(rows) == 4, len(rows))
        check("special chars preserved", rows[1][2] == terms[0].info, repr(rows[1][2]))
        check("empty cells", rows[3][:3] == ["", "", ""], rows[3])

        back, shape = core.load_glossary_file(path)
        check("xlsx import shape", shape == core.SHAPE_LIST)
        check("xlsx import rows", [t.src for t in back] == ["茜ヶ崎 空", "倉成 武"], [t.src for t in back])
        check("xlsx import dst", back[1].dst == "仓成武")

        # sheet without header
        raw = Path(tmp) / "raw.xlsx"
        xlsx_io.write_xlsx(raw, [["a", "b"], ["c", "d"]], header=False)
        back2, _ = core.load_glossary_file(raw)
        check("header-less xlsx", [t.src for t in back2] == ["a", "c"], [t.src for t in back2])

        check("col letters", [xlsx_io.col_letter(i) for i in (0, 25, 26, 27, 51)] ==
              ["A", "Z", "AA", "AB", "AZ"])


def test_presets() -> None:
    print("\n[presets]")
    terms = [core.Term(src="a", dst="b")]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = core.save_preset(root, "ever17", terms, lang="zh")
        check("numeric prefix added", target.name == "01_ever17.json", target.name)
        check("nested dir", target.parent == root / "resource" / "preset" / "glossary" / "zh")
        try:
            core.save_preset(root, "ever17", terms, lang="zh")
            check("exists raises", False)
        except FileExistsError:
            check("exists raises", True)
        second = core.save_preset(root, "other.json", terms, lang="zh")
        check("next index", second.name == "02_other.json", second.name)
        check("list_presets", [p.name for p in core.list_presets(root, "zh")] ==
              ["01_ever17.json", "02_other.json"])
        check("en dir empty", core.list_presets(root, "en") == [])
        back, _ = core.load_glossary_file(core.list_presets(root, "zh")[0])
        check("preset reloads", back[0].src == "a")
        check("sanitize", core.sanitize_filename('a/b:c*"<>|?') == "a_b_c______")


def test_live_api() -> None:
    print("\n[live VNDB API]")
    client = VndbClient()
    results = client.search_vns("Ever17")
    check("search returns results", len(results) > 0)
    check("has title", bool(results[0].get("title")), results[0] if results else None)
    by_id = client.search_vns("v17")
    check("lookup by id", by_id and by_id[0]["id"] == "v17")
    characters = client.vn_characters("v17")
    check("cast fetched", len(characters) > 5, len(characters))
    check("cast has originals", any(c.get("original") for c in characters))
    check("cast has aliases field", all("aliases" in c for c in characters))

    vn = client.get_vn("v17")
    options = core.BuildOptions()
    terms, stats = core.terms_from_vn(vn, characters, options)
    check("terms generated", len(terms) > 0, len(terms))
    check("all have japanese src", all(core.has_japanese(t.src) for t in terms))
    check("all have info", all(t.info for t in terms))
    check("info text is english",
          all(not any("\u4e00" <= c <= "\u9fff" for c in t.info) for t in terms))
    sample = terms[0]
    print(f"       e.g. {json.dumps(sample.to_export_dict(), ensure_ascii=False)}")


def test_gui() -> None:
    """Drive the real Tk window end-to-end (needs a display)."""
    print("\n[gui smoke test]")
    import tempfile
    import time

    import vndb_glossary as gui

    app = gui.App()
    app.withdraw()
    try:
        def pump(seconds: float = 25.0) -> None:
            deadline = time.time() + seconds
            while app.busy and time.time() < deadline:
                app.update()
                time.sleep(0.01)
            app.update()
            check("request finished", not app.busy)

        app.search_var.set("v17")
        app.action_search()
        pump()
        check("vn list populated", len(app.vn_tree.get_children()) > 0,
              len(app.vn_tree.get_children()))

        first = app.vn_tree.get_children()[0]
        app.vn_tree.selection_set(first)
        app.vn_tree.focus(first)
        app.update()
        check("selection bound", app.selected_vn is not None)

        app.action_build("replace")
        pump()
        check("glossary populated", len(app.glossary) > 0, len(app.glossary))
        check("rows rendered", len(app.glossary_tree.tree.get_children()) > 0)
        check("all src filled", all(t.src for t in app.glossary))
        check("all info filled", all(t.info for t in app.glossary))

        # in-place edit through the commit callback
        iid = app.glossary_tree.tree.get_children()[0]
        index = app.glossary_tree.tree.index(iid)
        original = app.glossary[index].src
        app._on_cell_commit(iid, "dst", "Test Translation")
        check("edit applied", app.glossary[index].dst == "Test Translation", app.glossary[index].dst)

        # toggling the enabled column
        app._on_cell_commit(iid, "on", "")
        check("toggle applied", app.glossary[index].enabled is False)
        app._on_cell_commit(iid, "on", "")
        check("toggle back", app.glossary[index].enabled is True)

        app.action_validate()
        check("validation ran", app.issues is not None)
        check("rows coloured", bool(app.glossary_tree.tree.tag_configure("error")))

        # filter
        app.filter_var.set("倉成武")
        app.update()
        filtered = len(app.glossary_tree.tree.get_children())
        check("filter narrows", 0 < filtered < len(app.glossary), filtered)
        app.filter_var.set("")
        app.update()
        check("filter cleared", len(app.glossary_tree.tree.get_children()) == len(app.glossary))

        # export through the real code path, minus the file dialogs
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "g.json"
            xlsx_path = Path(tmp) / "g.xlsx"
            terms = app._export_terms()
            core.write_json(json_path, terms, shape=core.SHAPE_LIST)
            core.write_xlsx(xlsx_path, terms)
            check("json export", json_path.is_file())
            check("xlsx export", xlsx_path.is_file())
            check("json has no empty src", all(t["src"] for t in json.loads(json_path.read_text("utf-8"))))
            check("xlsx has no empty src", all(r[0] for r in xlsx_io.read_xlsx(xlsx_path)[1:]))

            # preset round trip through the real functions
            preset = core.save_preset(Path(tmp), "smoke", terms, lang="zh")
            back, _ = core.load_glossary_file(preset)
            check("preset round trip", [t.src for t in back] == [t.src for t in terms])

        # append mode merges instead of replacing
        before = len(app.glossary)
        app._build_mode = "append"
        app._on_build_done((app.selected_vn, [], {"characters": 0, "terms": 0, "no_original": 0}, "append", []))
        check("append keeps rows", len(app.glossary) == before)
    finally:
        try:
            app.destroy()
        except Exception:
            pass


def main() -> int:
    offline = "--offline" in sys.argv
    test_parse_ids()
    test_build_terms()
    test_glossary_model()
    test_validation()
    test_roundtrip_json()
    test_xlsx()
    test_presets()
    if not offline:
        test_live_api()
    if "--gui" in sys.argv:
        test_gui()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())