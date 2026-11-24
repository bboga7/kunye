"""Runs kunye in a headless browser on the test files and on ordinary images.

    Run it through tests/run_all.sh.

For each test file it compares kunye's reading with tests/files/expected.json.
For each clean copy it checks that the picture data is unchanged and that the
removed notes are gone. Ordinary images are only checked for false alarms:
a signed record or a model label reported where the file has none.
"""
import base64
import os
import io
import json
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
PAGE = (HERE.parent / "index.html").resolve().as_uri()
FILES = HERE / "files"

JS = """async ([b64, mode]) => {
  const bin = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const pick = (r) => ({
    format: r.format, error: r.error || null,
    record: r.record ? { error: r.record.error || null, valid: !!r.record.valid, broken: !!r.record.broken,
      checks: (r.record.checks || []).map(c => [c.name, c.status]),
      sources: (r.record.sources || []).map(s => s.kind),
      signer: r.record.signer ? r.record.signer.subject.cn : null } : null,
    place: !!r.place, personal: r.personal.map(p => p.name), labels: r.labels.map(l => l.source.kind),
    plain: r.plain.length, unknown: r.unknown.length,
    preview: r.extra.some(x => x.includes("preview picture")) });
  const a = await window.kunye.analyze(bin);
  const out = { before: pick(a) };
  if (mode) {
    const c = await window.kunye.clean(bin, mode);
    out.after = pick(await window.kunye.analyze(c));
    let s = ""; for (let i = 0; i < c.length; i += 0x8000) s += String.fromCharCode(...c.subarray(i, i + 0x8000));
    out.cleaned = btoa(s);
  }
  return out;
}"""


def pixels(data):
    im = Image.open(io.BytesIO(data))
    im.load()
    return im.convert("RGBA").tobytes()


def picture_bytes(data):
    """The compressed picture data itself (JPEG scans, PNG IDAT chunks)."""
    if data[:2] == b"\xff\xd8":
        return data[data.rfind(b"\xff\xda"):]
    out, i = b"", 8
    while i < len(data):
        n = int.from_bytes(data[i:i + 4], "big")
        if data[i + 4:i + 8] == b"IDAT":
            out += data[i + 8:i + 8 + n]
        i += 12 + n
    return out


def main(extra_dirs):
    expected = json.loads((FILES / "expected.json").read_text())
    report = {"test_files": {}, "clean": {}, "ordinary": {}}
    fails = []
    with sync_playwright() as p:
        b = p.chromium.launch(**({"executable_path": os.environ["KUNYE_BROWSER"]} if os.environ.get("KUNYE_BROWSER") else {}))
        # every page in this run is opened with the network switched off
        pg = b.new_context(offline=True).new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("request", lambda r: None if r.url.startswith(("file:", "data:", "blob:")) else errors.append("request " + r.url))
        pg.goto(PAGE)

        # 1. Reading the test files
        for name, exp in expected.items():
            data = (FILES / name).read_bytes()
            got = pg.evaluate(JS, [base64.b64encode(data).decode(), None])["before"]
            ok = []
            rec = got["record"]
            ok.append(bool(rec) == exp["record"])
            if exp["record"]:
                ok.append(rec is not None and rec["valid"] == exp["valid"])
                src = exp.get("source")
                kinds = rec["sources"] if rec else []
                want = {"camera": "camera", "model": "model", "model-edited": "model-edit"}[src]
                ok.append(bool(kinds) and kinds[-1] == want)
            ok.append(got["place"] == exp["place"])
            ok.append(any("serial" in n.lower() or "model" in n.lower() for n in got["personal"]) == exp["device"])
            ok.append(got["preview"] == bool(exp.get("preview")))
            if exp.get("unsigned_label"):
                ok.append(got["labels"] == ["model"])
            else:
                ok.append(got["labels"] == [])
            passed = all(ok)
            report["test_files"][name] = {"passed": passed, "got": got}
            if not passed:
                fails.append(name)

        # 2. Clean copies
        for name in expected:
            data = (FILES / name).read_bytes()
            for mode in ("you", "all"):
                r = pg.evaluate(JS, [base64.b64encode(data).decode(), mode])
                cleaned = base64.b64decode(r["cleaned"])
                after = r["after"]
                same_data = picture_bytes(cleaned) == picture_bytes(data)
                try:
                    same_pixels = pixels(cleaned) == pixels(data)
                except Exception as e:
                    same_pixels = False
                no_you = not after["place"] and not after["personal"] and not after["preview"]
                rec_state = None
                if mode == "all":
                    gone = after["record"] is None and after["labels"] == [] and after["plain"] == 0
                else:
                    gone = no_you
                    if expected[name]["record"]:
                        rec_state = "still matches" if after["record"] and after["record"]["valid"] else "no longer matches"
                key = f"{name} [{mode}]"
                report["clean"][key] = {"picture_data_identical": same_data, "pixels_identical": same_pixels, "notes_gone": gone, "record_after": rec_state, "size": [len(data), len(cleaned)]}
                if mode == "you" and expected[name]["record"]:
                    (FILES.parent / "out").mkdir(exist_ok=True)
                    (FILES.parent / "out" / f"clean-you-{name}").write_bytes(cleaned)
                if not (same_data and same_pixels and gone):
                    fails.append(key)

        # 3. Ordinary images
        for d in extra_dirs:
            for f in sorted(Path(d).rglob("*")):
                if f.suffix.lower() not in (".jpg", ".jpeg", ".png") or not f.is_file():
                    continue
                data = f.read_bytes()
                try:
                    r = pg.evaluate(JS, [base64.b64encode(data).decode(), "all"])
                except Exception as e:
                    report["ordinary"][str(f)] = {"crash": str(e)[:200]}
                    continue
                got = r["before"]
                r2 = pg.evaluate(JS, [base64.b64encode(data).decode(), "you"])
                try:
                    keeps = all(pixels(base64.b64decode(x["cleaned"])) == pixels(data) for x in (r, r2))
                except Exception as e:
                    keeps = "unreadable: " + type(e).__name__
                you_left = r2["after"]["place"] or bool(r2["after"]["personal"]) or r2["after"]["preview"]
                report["ordinary"][str(f)] = {
                    "false_record": got["record"] is not None,
                    "false_label": bool(got["labels"]),
                    "error": got["error"],
                    "place": got["place"], "personal": len(got["personal"]), "preview": got["preview"],
                    "clean_keeps_pixels": keeps,
                    "notes_about_you_left_after_clean": you_left,
                }
        report["page_errors"] = errors
        b.close()

    o = report["ordinary"]
    readable = {k: v for k, v in o.items() if not v.get("error") and "crash" not in v}
    summary = {
        "test_files": len(expected),
        "test_files_read_as_expected": sum(v["passed"] for v in report["test_files"].values()),
        "clean_copies": len(report["clean"]),
        "clean_copies_ok": sum(v["picture_data_identical"] and v["pixels_identical"] and v["notes_gone"] for v in report["clean"].values()),
        "signed_files_after_removing_notes_about_you": sorted({v["record_after"] for v in report["clean"].values() if v["record_after"]}),
        "ordinary_images": len(o),
        "ordinary_not_jpeg_or_png_or_crashed": len(o) - len(readable),
        "ordinary_false_record": sum(v["false_record"] for v in readable.values()),
        "ordinary_false_label": sum(v["false_label"] for v in readable.values()),
        "ordinary_with_place": sum(v["place"] for v in readable.values()),
        "ordinary_with_personal_notes": sum(v["personal"] > 0 for v in readable.values()),
        "ordinary_with_preview": sum(v["preview"] for v in readable.values()),
        "ordinary_notes_about_you_left_after_clean": sum(v["notes_about_you_left_after_clean"] for v in readable.values()),
        "ordinary_clean_keeps_pixels": sum(v["clean_keeps_pixels"] is True for v in readable.values()),
        "page_errors": len([e for e in report["page_errors"] if not e.startswith("request")]),
        "outside_requests": [e for e in report["page_errors"] if e.startswith("request")],
        "fails": fails,
    }
    report["summary"] = summary
    (HERE / "results.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
