"""A small signer for test files, written for kunye's tests.

It builds a signed provenance record from scratch (boxes, a small CBOR
encoder, a COSE signature made with the local test key) and places it in a
JPEG or PNG file. Writing it here keeps the test files free of any outside
program's own markings.

Only what the tests need is covered: one manifest, an actions assertion and a
hash of the file. It is not meant for signing real images.
"""
import base64
import hashlib
import struct
import subprocess
import uuid
import zlib
from pathlib import Path

SUFFIX = bytes.fromhex("00110010800000aa00389b71")


# ---------- CBOR (encoder, only the types used here) ----------
def _head(major, n):
    if n < 24:
        return bytes([(major << 5) | n])
    if n < 256:
        return bytes([(major << 5) | 24, n])
    if n < 65536:
        return bytes([(major << 5) | 25]) + struct.pack(">H", n)
    return bytes([(major << 5) | 26]) + struct.pack(">I", n)


class Tag:
    def __init__(self, tag, value):
        self.tag, self.value = tag, value


def cbor(v):
    if v is None:
        return b"\xf6"
    if isinstance(v, bool):
        return b"\xf5" if v else b"\xf4"
    if isinstance(v, int):
        return _head(0, v) if v >= 0 else _head(1, -1 - v)
    if isinstance(v, (bytes, bytearray)):
        return _head(2, len(v)) + bytes(v)
    if isinstance(v, str):
        b = v.encode()
        return _head(3, len(b)) + b
    if isinstance(v, list):
        return _head(4, len(v)) + b"".join(cbor(x) for x in v)
    if isinstance(v, dict):
        return _head(5, len(v)) + b"".join(cbor(k) + cbor(x) for k, x in v.items())
    if isinstance(v, Tag):
        return _head(6, v.tag) + cbor(v.value)
    raise TypeError(type(v))


# ---------- boxes ----------
def box(kind, body):
    return struct.pack(">I", 8 + len(body)) + kind + body


def jumd(kind4, label):
    return box(b"jumd", kind4 + SUFFIX + b"\x03" + label.encode() + b"\x00")


def superbox(kind4, label, *content):
    return box(b"jumb", jumd(kind4, label) + b"".join(content))


def cbor_assertion(label, value):
    return superbox(b"cbor", label, box(b"cbor", cbor(value)))


def body_of(superbox_bytes):
    return superbox_bytes[8:]


# ---------- signature ----------
def _pem_blocks(text):
    out, cur = [], None
    for line in text.splitlines():
        if line.startswith("-----BEGIN"):
            cur = []
        elif line.startswith("-----END"):
            out.append(base64.b64decode("".join(cur)))
            cur = None
        elif cur is not None:
            cur.append(line.strip())
    return out


def _der_to_raw(sig, size=32):
    # ECDSA signature in DER (SEQUENCE of two INTEGERs) to raw r||s
    i = 2 if sig[1] < 0x80 else 3
    parts = []
    for _ in range(2):
        assert sig[i] == 0x02
        n = sig[i + 1]
        v = sig[i + 2:i + 2 + n].lstrip(b"\x00")
        parts.append(v.rjust(size, b"\x00"))
        i += 2 + n
    return b"".join(parts)


def cose_sign(claim_bytes, certs_dir):
    certs_dir = Path(certs_dir)
    chain = _pem_blocks((certs_dir / "chain.pem").read_text())
    protected = cbor({1: -7, 33: chain})
    to_sign = cbor(["Signature1", protected, b"", claim_bytes])
    der = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(certs_dir / "ee.key")],
        input=to_sign, capture_output=True, check=True,
    ).stdout
    return cbor(Tag(18, [protected, {}, None, _der_to_raw(der)]))


# ---------- the record ----------
def build_record(actions, title, file_hash, exclusion, certs_dir, label):
    a_actions = cbor_assertion("c2pa.actions.v2", {"actions": actions})
    a_hash = cbor_assertion("c2pa.hash.data", {
        "exclusions": [{"start": exclusion[0], "length": exclusion[1]}],
        "name": "jumbf manifest", "alg": "sha256", "hash": file_hash, "pad": b"",
    })
    store = superbox(b"c2as", "c2pa.assertions", a_actions, a_hash)
    claim = {
        "instanceID": "xmp:iid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, label)),
        "claim_generator_info": {"name": "kunye test builder", "version": "1.0"},
        "signature": f"self#jumbf=/c2pa/{label}/c2pa.signature",
        "created_assertions": [
            {"url": "self#jumbf=c2pa.assertions/c2pa.actions.v2", "hash": hashlib.sha256(body_of(a_actions)).digest()},
            {"url": "self#jumbf=c2pa.assertions/c2pa.hash.data", "hash": hashlib.sha256(body_of(a_hash)).digest()},
        ],
        "dc:title": title,
        "alg": "sha256",
    }
    claim_bytes = cbor(claim)
    sig = cose_sign(claim_bytes, certs_dir)
    manifest = superbox(b"c2ma", label,
                        store,
                        superbox(b"c2cl", "c2pa.claim.v2", box(b"cbor", claim_bytes)),
                        superbox(b"c2cs", "c2pa.signature", box(b"cbor", sig)))
    return superbox(b"c2pa", "c2pa", manifest)


def _jpeg_insert_at(data):
    # after the first APP0 segment if present, else right after the start marker
    if data[2:4] == b"\xff\xe0":
        return 4 + struct.unpack(">H", data[4:6])[0]
    return 2


def _png_chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def sign_bytes(data, actions, title, certs_dir):
    """Returns the file with a signed record placed in it."""
    is_png = data[:8] == b"\x89PNG\r\n\x1a\n"
    label = "urn:c2pa:" + str(uuid.uuid5(uuid.NAMESPACE_OID, title + hashlib.sha256(data).hexdigest()))
    pos = 33 if is_png else _jpeg_insert_at(data)  # PNG: after the header chunk

    def wrap(record):
        if is_png:
            return _png_chunk(b"caBX", record)
        payload = b"JP" + struct.pack(">HI", 0x0211, 1) + record
        return b"\xff\xeb" + struct.pack(">H", 2 + len(payload)) + payload

    # 1. build once with an empty hash to learn the size, 2. hash the file around it, 3. build again
    probe = wrap(build_record(actions, title, b"\x00" * 32, (pos, 60000), certs_dir, label))
    size = len(probe)
    probe = wrap(build_record(actions, title, b"\x00" * 32, (pos, size), certs_dir, label))
    assert len(probe) == size
    file_hash = hashlib.sha256(data[:pos] + data[pos:]).digest()
    final = wrap(build_record(actions, title, file_hash, (pos, size), certs_dir, label))
    assert len(final) == size, "record size changed"
    return data[:pos] + final + data[pos:]
