"""Download a pinned, complete ruwiki snapshot with resume and SHA1 verification."""

import argparse, hashlib, json, subprocess, urllib.request
from pathlib import Path

UA = "OstrovokKazanResearch/0.1 (+https://github.com/ostrovok-hackathon-2026/graceful-knitting-raccoon)"
BASE = "https://dumps.wikimedia.org"
JOBS = [
    "pagetable",
    "linktargettable",
    "redirecttable",
    "pagelinkstable",
    "imagelinkstable",
    "articlesmultistreamdumprecombine",
]


def download(folder, snapshot):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    manifest = folder / "dumpstatus.json"
    if not manifest.exists():
        req = urllib.request.Request(f"{BASE}/ruwiki/{snapshot}/dumpstatus.json", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as response:
            raw = response.read()
        data = json.loads(raw)
        if any(data["jobs"][j]["status"] != "done" for j in JOBS):
            raise ValueError("Snapshot is incomplete")
        manifest.write_bytes(raw)
    data = json.loads(manifest.read_text())
    for job in JOBS:
        for name, meta in data["jobs"][job]["files"].items():
            if not (name.endswith(".sql.gz") or name.endswith("-multistream.xml.bz2")):
                continue
            path = folder / name
            if not path.exists():
                part = path.with_suffix(path.suffix + ".part")
                print("DOWNLOAD", name, meta["size"], flush=True)
                subprocess.run(
                    [
                        "curl",
                        "--fail",
                        "--location",
                        "--retry",
                        "5",
                        "--retry-delay",
                        "10",
                        "--connect-timeout",
                        "30",
                        "--speed-limit",
                        "1024",
                        "--speed-time",
                        "120",
                        "--continue-at",
                        "-",
                        "--user-agent",
                        UA,
                        "--output",
                        str(part),
                        BASE + meta["url"],
                    ],
                    check=True,
                )
                if part.stat().st_size != meta["size"]:
                    raise ValueError(f"Wrong size: {name}")
                part.replace(path)
            sha = hashlib.sha1()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                    sha.update(block)
            if sha.hexdigest() != meta["sha1"]:
                raise ValueError(f"Wrong checksum: {name}; remove corrupted file before retrying")
            print("VERIFIED", name, flush=True)
    (folder / "download-complete.json").write_text(json.dumps({"snapshot": snapshot, "jobs": JOBS}))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--folder", required=True)
    p.add_argument("--snapshot", default="20261001")
    a = p.parse_args()
    download(a.folder, a.snapshot)
