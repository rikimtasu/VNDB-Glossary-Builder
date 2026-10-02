/** Headless logic check for the TS port (runs in plain node). */
import {
  normalizeKey,
  hasJapanese,
  termsFromVn,
  validateTerms,
  parseGlossaryText,
  dumpJson,
  presetFileName,
  sanitizeFilename,
  DEFAULT_OPTIONS,
} from "../src/lib/glossary.ts";
import { parseVnId } from "../src/lib/vndb.ts";

let failures = 0;
function check(name: string, cond: boolean, detail = "") {
  if (cond) console.log(`  ok   ${name}`);
  else {
    console.log(`  FAIL ${name} ${detail}`);
    failures += 1;
  }
}

console.log("[parseVnId]");
check("plain", parseVnId("v17") === "v17");
check("bare number", parseVnId("17") === "v17");
check("url", parseVnId("https://vndb.org/v17") === "v17");
check("release url", parseVnId("https://vndb.org/r1234") === "v1234");
check("garbage", parseVnId("ever17") === null);

console.log("[termsFromVn]");
const vn: any = { id: "v17", title: "Ever17" };
const chars: any[] = [
  {
    id: "c32", name: "Akanegasaki Sora", original: "茜ヶ崎 空",
    aliases: ["Sorapi", "空ピー", "Y"], gender: ["f", "f"],
    vns: [{ id: "v17", role: "primary", spoiler: 0 }],
    traits: [
      { name: "Brown", group_name: "Hair" },
      { name: "Long", group_name: "Hair" },
      { name: "Green", group_name: "Eyes" },
    ],
  },
  {
    id: "c24", name: "Kuranari Takeshi", original: "倉成 武", aliases: [],
    gender: ["m", "m"], vns: [{ id: "v17", role: "main", spoiler: 2 }], traits: [],
  },
];
const { terms, stats } = termsFromVn(vn, chars, { ...DEFAULT_OPTIONS });
const srcs = terms.map((t) => t.src);
check("main first", srcs[0] === "倉成 武", JSON.stringify(srcs));
check("alias kept", srcs.includes("空ピー"));
check("latin alias dropped", !srcs.includes("Sorapi"));
check("single char dropped", !srcs.includes("Y"));
const sora = terms.find((t) => t.src === "茜ヶ崎 空")!;
check("dst prefilled", sora.dst === "Akanegasaki Sora", sora.dst);
check("gender+role", sora.info.includes("Female") && sora.info.includes("Main character"), sora.info);
check("traits diversified", sora.info.includes("Hair: Brown") && sora.info.includes("Eyes: Green"), sora.info);
check("no reading dup", !sora.info.includes("Romanization:"), sora.info);
const takeshi = terms.find((t) => t.src === "倉成 武")!;
check("spoiler flag", takeshi.info.includes("[Spoiler]"), takeshi.info);
check("stats", stats.noOriginal === 0 && stats.characters === 2, JSON.stringify(stats));

// VNDB gender is [apparent, real]: defaults prefer the true (spoiler) value.
const secretive: any = {
  id: "c99",
  name: "Hidden One",
  original: "隠し子",
  aliases: [],
  gender: ["a", "f"],
  vns: [{ id: "v17", role: "side", spoiler: 0 }],
  traits: [],
};
const shown = termsFromVn(vn, [secretive], { ...DEFAULT_OPTIONS });
check("spoiler gender shown by default", shown.terms[0].info.includes("Female"), shown.terms[0].info);
const veiled = termsFromVn(vn, [secretive], { ...DEFAULT_OPTIONS, preferSpoilerValues: false });
check("apparent gender when opted out", veiled.terms[0].info.includes("Ambiguous"), veiled.terms[0].info);

console.log("[validate]");
const issues = validateTerms([
  { src: "", dst: "x", info: "", enabled: true, note: "" },
  { src: "花", dst: "", info: "", enabled: true, note: "" },
  { src: "茜ヶ崎 空", dst: "茜崎空", info: "", enabled: true, note: "" },
  { src: "茜ヶ崎空", dst: "茜崎空", info: "", enabled: true, note: "" },
  { src: "Master", dst: "Master", info: "", enabled: true, note: "" },
]);
check("empty src error", issues.some((i) => i.level === "error"));
check("empty dst warning", issues.some((i) => i.level === "warning" && i.message.includes("no translation")));
check("dup warning", issues.some((i) => i.message.includes("duplicates row")));
check("self-translation note", issues.some((i) => i.message.includes("translated as itself")));

console.log("[import/export]");
const std = parseGlossaryText(JSON.stringify([{ src: "あ", dst: "啊", info: "n" }]));
check("list shape", std.shape === "list" && std.terms[0].src === "あ");
const dict = parseGlossaryText(JSON.stringify({ あ: "啊" }));
check("dict shape", dict.shape === "dict" && dict.terms[0].dst === "啊");
const actors = parseGlossaryText(JSON.stringify([{ id: 1, name: "アルテ", nickname: "小アル" }]));
check("actors shape", actors.shape === "actors" && actors.terms[0].dst === "小アル");
const cn = parseGlossaryText(JSON.stringify([{ 原文: "花", 译文: "花", 描述: "角色" }]));
check("chinese keys", cn.terms[0].src === "花" && cn.terms[0].info === "角色");
const js = dumpJson([{ src: "a", dst: "b", info: "i", enabled: true, note: "" }], "list");
check("dump list", js.includes('"src": "a"'));
const jd = dumpJson([{ src: "a", dst: "b", info: "i", enabled: true, note: "" }], "dict");
check("dump dict", jd.includes('"a": "b"') && !jd.includes("info"));
check("preset name", presetFileName([], "ever17") === "01_ever17.json", presetFileName([], "ever17"));
check("preset next", presetFileName(["01_ever17.json"], "other.json") === "02_other.json");
check("sanitize", sanitizeFilename('a/b:c*"<>|?') === "a_b_c______");
check("normalize", normalizeKey(" 茜ヶ崎 空 ") === normalizeKey("茜ヶ崎空"));
check("hasJapanese", hasJapanese("空ピー") && !hasJapanese("Sorapi"));

if (failures) {
  console.log(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("all checks passed");
