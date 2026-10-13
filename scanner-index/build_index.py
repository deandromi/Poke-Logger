#!/usr/bin/env python3
"""Produce the exact scanner-index-v2.zip format consumed by Poké-Logger Android.

This is a REAL data build from TCGdex thumbnails, not generated dummy entries.
Coverage is NOT recognition accuracy; Pillow scaling is not pixel-perfect Android scaling.
"""
import argparse
import concurrent.futures
import hashlib
import io
import json
import os
import random
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from zipfile import ZipFile, ZIP_DEFLATED

CAT = "scanner-card-catalogue-v2.tsv"
SIG = "scanner-image-signatures-v2.tsv"
BASE = "https://assets.tcgdex.net/"
API = "https://api.tcgdex.net/v2/en/cards"


def fetch(url, attempts=5):
    if not (url.startswith(BASE) or url == API):
        raise ValueError("Untrusted card URL")
    last = None
    for attempt in range(attempts):
        try:
            with urlopen(Request(url, headers={"User-Agent": "PokeLoggerIndexBuilder/1.0"}), timeout=24) as res:
                if res.status != 200:
                    raise ValueError(f"HTTP {res.status}")
                cap = (25 if url == API else 3) * 1024 * 1024
                data = res.read(cap + 1)
                if len(data) > cap:
                    raise ValueError("Oversized download")
                return data
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as e:
            last = e
            if isinstance(e, HTTPError) and e.code in (400, 401, 403, 404, 410):
                break
            if attempt < attempts - 1:
                time.sleep(min(12, 2 ** attempt) + random.random())
    raise RuntimeError(str(last))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe(value):
    return str(value or "").replace("\t", " ").replace("\n", " ").replace("\r", " ").strip()


def build_catalogue(output):
    cards = json.loads(fetch(API))
    if not isinstance(cards, list):
        raise ValueError("Invalid upstream catalogue")
    rows, seen = [], set()
    for c in cards:
        if not isinstance(c, dict):
            continue
        name, cid, image = safe(c.get("name")), safe(c.get("id")), safe(c.get("image"))
        if not name or not cid or not image.startswith(BASE) or cid in seen:
            continue
        seen.add(cid)
        rows.append("\t".join((f"tcgdex:{cid}", name, safe(c.get("localId")), cid.rpartition("-")[0], image)))
    if len(rows) < 1000:
        raise ValueError("Catalogue is incomplete")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(rows) + "\n", encoding="utf8")
    print("Reference cards:", len(rows), flush=True)


def image_signature(data):
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(data)) as source:
        image = ImageOps.contain(source.convert("RGB"), (144, 200), Image.Resampling.BILINEAR)
        width, height = image.size
        x, y = int(width * .12), int(height * .15)
        art = image.crop((x, y, min(width, x + int(width * .76)), min(height, y + int(height * .49))))
        def dhash(im):
            px = im.resize((9, 8), Image.Resampling.BILINEAR).load()
            lum = lambda p: p[0] * 3 + p[1] * 6 + p[2]
            return sum((1 << (row * 8 + col)) for row in range(8) for col in range(8)
                       if lum(px[col, row]) > lum(px[col + 1, row]))
        colors = image.resize((3, 3), Image.Resampling.BILINEAR).load()
        rgb = ",".join(str(colors[x, y][channel])
                       for y in range(3) for x in range(3) for channel in range(3))
        return f"{dhash(image):x}|{dhash(art):x}|{rgb}"


def make_entry(row):
    provider_id, _, _, _, image = row.split("\t")
    try:
        return provider_id, image_signature(fetch(image.rstrip("/") + "/low.webp")), ""
    except Exception as e:
        return provider_id, None, safe(e)


def build_shard(catalogue, index, count, output, failures, workers):
    rows = catalogue.read_text("utf8").splitlines()
    assigned = [row for i, row in enumerate(rows) if i % count == index]
    output.parent.mkdir(parents=True, exist_ok=True)
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool, output.open("w", encoding="utf8") as f:
        for pos, (cid, value, err) in enumerate(pool.map(make_entry, assigned), 1):
            if value:
                f.write(f"{cid}|{value}\n")
            else:
                errors.append(f"{cid}\t{err}")
            if pos % 250 == 0:
                print(f"Shard {index}: {pos}/{len(assigned)}, failures={len(errors)}", flush=True)
    failures.write_text("\n".join(errors) + ("\n" if errors else ""), encoding="utf8")
    print(f"Shard {index}: {len(assigned)-len(errors)} successful, {len(errors)} failed", flush=True)


def validate_catalogue(text):
    records = {}
    for row in text.splitlines():
        fields = row.split("\t")
        if len(fields) != 5 or not fields[0].startswith("tcgdex:") or not fields[4].startswith(BASE):
            raise ValueError("Invalid catalogue row")
        if fields[0] in records:
            raise ValueError("Duplicate catalogue ID")
        records[fields[0]] = row
    if len(records) < 1000:
        raise ValueError("Catalogue too small")
    return records


def validate_signature(line, records):
    fields = line.split("|")
    if len(fields) != 4 or fields[0] not in records:
        raise ValueError("Invalid reference ID")
    if any(len(h) > 16 or not h for h in fields[1:3]):
        raise ValueError("Invalid hash length")
    int(fields[1], 16)
    int(fields[2], 16)
    parts = fields[3].split(",")
    if len(parts) != 27 or any(int(p) < 0 or int(p) > 255 for p in parts):
        raise ValueError("Invalid RGB descriptor")
    return fields[0]


def verify(bundle):
    with ZipFile(bundle) as z:
        if z.testzip() is not None or set(z.namelist()) != {CAT, SIG, "manifest.txt"}:
            raise ValueError("Damaged or unexpected index archive")
        manifest = dict(line.split("=", 1) for line in z.read("manifest.txt").decode().splitlines())
        cat, sig = z.read(CAT), z.read(SIG)
        if manifest.get("format") != "pokelogger-scanner-v2":
            raise ValueError("Unsupported index version")
        if digest(cat) != manifest.get("catalogue_sha256") or digest(sig) != manifest.get("signatures_sha256"):
            raise ValueError("Hash mismatch")
        cards = validate_catalogue(cat.decode("utf8"))
        found = set()
        for line in sig.decode("utf8").splitlines():
            cid = validate_signature(line, cards)
            if cid in found:
                raise ValueError("Duplicate signature ID")
            found.add(cid)
        coverage = len(found) / len(cards)
        if coverage < .95:
            raise ValueError(f"Insufficient index coverage: {coverage:.2%}")
        print(f"Verified: {len(found)}/{len(cards)} ({coverage:.2%})", flush=True)


def assemble(catalogue_path, directory, output):
    cat = catalogue_path.read_bytes()
    cards = validate_catalogue(cat.decode("utf8"))
    refs = set()
    by_id = {}
    paths = sorted(directory.glob("scanner-signatures-shard-*.tsv"))
    if len(paths) != 8:
        raise ValueError(f"Expected 8 signature shards, got {len(paths)}")
    for path in paths:
        for line in path.read_text("utf8").splitlines():
            cid = validate_signature(line, cards)
            if cid in refs:
                raise ValueError("Duplicate signature ID")
            refs.add(cid)
            by_id[cid] = line
    coverage = len(by_id) / len(cards)
    print(f"Indexed {len(by_id)}/{len(cards)} = {coverage:.2%}", flush=True)
    if coverage < .95:
        raise ValueError("Insufficient coverage; refusing to publish")
    sig = ("\n".join(by_id[cid] for cid in cards if cid in by_id) + "\n").encode()
    manifest = f"format=pokelogger-scanner-v2\ncatalogue_sha256={digest(cat)}\nsignatures_sha256={digest(sig)}\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("manifest.txt", manifest)
        z.writestr(CAT, cat)
        z.writestr(SIG, sig)
    if output.stat().st_size > 40 * 1024 * 1024 or max(len(cat), len(sig)) > 20 * 1024 * 1024:
        raise ValueError("Installer size limit exceeded")
    verify(output)
    meta = {"format":"pokelogger-scanner-v2","cards_in_catalogue":len(cards),
            "cards_indexed":len(by_id),"coverage":round(coverage,5),
            "sha256":digest(output.read_bytes()),"source":API,
            "note":"Indexed coverage is NOT measured identification accuracy"}
    output.with_name("scanner-index-v2-manifest.json").write_text(json.dumps(meta,indent=2)+"\n")
    output.with_name("scanner-index-v2.sha256").write_text(f"{meta['sha256']}  {output.name}\n")
    print(f"Ready: {output} ({output.stat().st_size/1048576:.2f} MiB)", flush=True)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("catalogue"); c.add_argument("--output", type=Path, required=True)
    s = sub.add_parser("shard")
    s.add_argument("--catalogue", type=Path, required=True)
    s.add_argument("--shard-index", type=int, required=True)
    s.add_argument("--shard-count", type=int, default=8)
    s.add_argument("--workers", type=int, default=6)
    s.add_argument("--output", type=Path, required=True)
    s.add_argument("--failures", type=Path, required=True)
    a = sub.add_parser("assemble")
    a.add_argument("--catalogue", type=Path, required=True)
    a.add_argument("--shard-dir", type=Path, required=True)
    a.add_argument("--output", type=Path, required=True)
    v = sub.add_parser("verify"); v.add_argument("archive", type=Path)
    opts = ap.parse_args()
    if opts.cmd == "catalogue": build_catalogue(opts.output)
    elif opts.cmd == "shard":
        build_shard(opts.catalogue, opts.shard_index, opts.shard_count, opts.output, opts.failures, opts.workers)
    elif opts.cmd == "assemble": assemble(opts.catalogue, opts.shard_dir, opts.output)
    elif opts.cmd == "verify": verify(opts.archive)


if __name__ == "__main__":
    main()
