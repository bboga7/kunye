"""Builds the kunye test files on the local machine.

Every file is made from scratch: a drawn picture, plain notes (EXIF, and an
embedded text block for some), and for some files a signed record made by
tests/signer.py with a throwaway test certificate. Nothing is downloaded.

    Run it through tests/run_all.sh.
"""
import io
import json
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw

import signer

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
OUT = HERE / "files"
CERTS = HERE / "certs"


def picture(seed, size=(960, 640)):
    """A simple drawn landscape, different for each seed."""
    w, h = size
    im = Image.new("RGB", size, (232, 239, 246))
    d = ImageDraw.Draw(im)
    d.ellipse((w * 0.62, h * 0.12, w * 0.62 + 120, h * 0.12 + 120), fill=(255, 255, 255))
    tones = [(159, 184, 211), (95, 134, 174), (45, 91, 136)]
    for i, tone in enumerate(tones):
        y = h * (0.55 + 0.13 * i) + (seed * 7 % 30)
        pts = [(0, y)]
        for x in range(0, w + 40, 40):
            pts.append((x, y + 30 * (((x // 40 + seed + i) % 5) - 2)))
        pts += [(w, h), (0, h)]
        d.polygon(pts, fill=tone)
    return im


def exif_bytes(with_place=True, with_device=True):
    ex = Image.Exif()
    if with_device:
        ex[0x010F] = "Testmaker"           # Make (invented)
        ex[0x0110] = "TM-100"              # Model (invented)
        ex[0x0131] = "kunye test builder"  # Software
        ex[0x0132] = "2026:11:20 10:15:00"
        ifd = ex.get_ifd(0x8769)
        ifd[0x9003] = "2026:11:20 10:15:00"  # DateTimeOriginal
        ifd[0xA430] = "Test Owner"           # CameraOwnerName
        ifd[0xA431] = "SN-00012345"          # BodySerialNumber
        ifd[0xA434] = "Test lens 35mm"       # LensModel
    if with_place:
        gps = ex.get_ifd(0x8825)
        gps[1] = "N"
        gps[2] = (12.0, 20.0, 44.4)
        gps[3] = "W"
        gps[4] = (45.0, 40.0, 44.0)
    return ex.tobytes()


def save(im, path, exif=None, xmp=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    kw = {}
    if exif:
        kw["exif"] = exif
    if xmp:
        kw["xmp"] = xmp
    if path.suffix == ".png":
        im.save(path, "PNG", **kw)
    else:
        im.save(path, "JPEG", quality=88, **kw)


SOURCE = "http://cv.iptc.org/newscodes/digitalsourcetype/"


def manifest(title, action, source_type):
    acts = [{"action": a, "digitalSourceType": SOURCE + t} for a, t in zip(action, source_type)] if isinstance(action, list) else [{"action": action, "digitalSourceType": SOURCE + source_type}]
    return {
        "claim_generator_info": [{"name": "kunye test builder", "version": "1.0"}],
        "title": title,
        "assertions": [
            {
                "label": "c2pa.actions",
                "data": {"actions": acts},
            }
        ],
    }


def sign(src, dest, title, action, source_type):
    acts = manifest(title, action, source_type)["assertions"][0]["data"]["actions"]
    dest.write_bytes(signer.sign_bytes(src.read_bytes(), acts, title, CERTS))


def png_chunks(data):
    i, out = 8, []
    while i < len(data):
        n = int.from_bytes(data[i:i + 4], "big")
        out.append((data[i + 4:i + 8], i, i + 12 + n))
        i += 12 + n
    return out


def png_chunk(kind, body):
    import zlib
    return len(body).to_bytes(4, "big") + kind + body + zlib.crc32(kind + body).to_bytes(4, "big")


def flip_picture_byte(src, dest):
    data = src.read_bytes()
    if src.suffix == ".png":
        # Change one pixel and write the picture data again, as an editor would.
        im = Image.open(src).convert("RGB")
        x, y = 10, 10
        r, g, b = im.getpixel((x, y))
        im.putpixel((x, y), (r ^ 1, g, b))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        new_idat = b"".join(data2[s:e] for k, s, e in png_chunks(buf.getvalue()) if k == b"IDAT") if (data2 := buf.getvalue()) else b""
        parts, placed = [], False
        for k, s, e in png_chunks(data):
            if k == b"IDAT":
                if not placed:
                    parts.append(new_idat)
                    placed = True
                continue
            parts.append(data[s:e])
        dest.write_bytes(data[:8] + b"".join(parts))
    else:
        b = bytearray(data)
        b[b.rfind(b"\xff\xda") + 400] ^= 0x01
        dest.write_bytes(bytes(b))


def change_note(src, dest, old, new):
    data = src.read_bytes()
    assert len(old) == len(new) and old in data
    data = data.replace(old, new, 1)
    if src.suffix == ".png":
        # keep every chunk's checksum valid, as an editor would
        fixed = data[:8]
        for k, s, e in png_chunks(data):
            fixed += png_chunk(k, data[s + 8:e - 4])
        data = fixed
    dest.write_bytes(data)


XMP_AI = (
    '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description rdf:about="" xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/" '
    'Iptc4xmpExt:DigitalSourceType="' + SOURCE + 'trainedAlgorithmicMedia"/>'
    '</rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
)


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    tmp = OUT / "_unsigned"
    expect = {}

    def add(name, **e):
        expect[name] = e

    for ext in (".jpg", ".png"):
        base = "png" if ext == ".png" else "jpg"
        # 1. camera capture with notes and a signed record
        u = tmp / f"camera{ext}"
        save(picture(1), u, exif=exif_bytes())
        n = f"signed-camera.{base}"
        sign(u, OUT / n, "camera", "c2pa.created", "digitalCapture")
        add(n, record=True, valid=True, source="camera", place=True, device=True)

        # 2. generated by a model, signed, no plain notes
        u = tmp / f"model{ext}"
        save(picture(2), u)
        n = f"signed-model-made.{base}"
        sign(u, OUT / n, "model made", "c2pa.created", "trainedAlgorithmicMedia")
        add(n, record=True, valid=True, source="model", place=False, device=False)

        # 3. edited with a model's help, signed
        u = tmp / f"edited{ext}"
        save(picture(3), u, exif=exif_bytes(with_place=False))
        n = f"signed-model-edited.{base}"
        sign(u, OUT / n, "model edited", ["c2pa.created", "c2pa.edited"], ["digitalCapture", "compositeWithTrainedAlgorithmicMedia"])
        add(n, record=True, valid=True, source="model-edited", place=False, device=True)

        # 4. picture changed after signing
        n = f"changed-picture-after-signing.{base}"
        flip_picture_byte(OUT / f"signed-model-made.{base}", OUT / n)
        add(n, record=True, valid=False, source="model", place=False, device=False)

        # 5. a plain note changed after signing (model name, same length)
        n = f"changed-note-after-signing.{base}"
        change_note(OUT / f"signed-camera.{base}", OUT / n, b"TM-100", b"TM-200")
        add(n, record=True, valid=False, source="camera", place=True, device=True)

        # 6. place and device notes, no record
        n = f"notes-place-device.{base}"
        save(picture(4), OUT / n, exif=exif_bytes())
        add(n, record=False, place=True, device=True)

        # 7. unsigned text note claiming the image was made by a model
        n = f"unsigned-model-label.{base}"
        if ext == ".png":
            u = tmp / "label.png"
            save(picture(5), u)
            raw = u.read_bytes()
            itxt = png_chunk(b"iTXt", b"XML:com.adobe.xmp\0\0\0\0\0" + XMP_AI.encode())
            (OUT / n).write_bytes(raw[:33] + itxt + raw[33:])
        else:
            save(picture(5), OUT / n, xmp=XMP_AI.encode())
        add(n, record=False, place=False, device=False, unsigned_label="model")

        # 9. place and device notes plus a small preview of the uncropped picture (JPEG only)
        if ext == ".jpg":
            import piexif
            full = picture(7)
            thumb = io.BytesIO()
            full.resize((160, 107)).save(thumb, "JPEG", quality=70)
            ex = piexif.load(exif_bytes())
            ex["1st"] = {piexif.ImageIFD.Compression: 6}
            ex["thumbnail"] = thumb.getvalue()
            n = "notes-with-preview.jpg"
            save(full.crop((200, 100, 760, 540)), OUT / n, exif=piexif.dump(ex))
            add(n, record=False, place=True, device=True, preview=True)

        # 8. no notes at all
        n = f"no-notes.{base}"
        save(picture(6), OUT / n)
        add(n, record=False, place=False, device=False)

    shutil.rmtree(tmp)
    (OUT / "expected.json").write_text(json.dumps(expect, indent=2))

    # Optional: check the files with a second, separately written reader, if it is installed.
    try:
        import c2pa
    except ImportError:
        print("second reader not installed, skipping that check")
        return
    report = {}
    for name in expect:
        try:
            report[name] = c2pa.Reader(str(OUT / name)).get_validation_state()
        except Exception as e:  # no record in the file
            report[name] = "no record"
    (OUT / "second-reader-check.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))

if __name__ == "__main__":
    main()
