"""Download pinned pandoc and Typst releases, check their SHA-256 and install them.

Used by Dockerfile.worker; stdlib only, so the image needs no curl or xz.
Update versions and checksums together.
"""

import hashlib
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

PANDOC = "3.9"
TYPST = "0.14.2"

TOOLS = {
    "x86_64": [
        ("pandoc", f"https://github.com/jgm/pandoc/releases/download/{PANDOC}/pandoc-{PANDOC}-linux-amd64.tar.gz",
         "872a10c7ec29d5278831d855b11bd4ade0df4d5ec9a089a3197bc63a37eb4003"),
        ("typst", f"https://github.com/typst/typst/releases/download/v{TYPST}/typst-x86_64-unknown-linux-musl.tar.xz",
         "a6044cbad2a954deb921167e257e120ac0a16b20339ec01121194ff9d394996d"),
    ],
    "aarch64": [
        ("pandoc", f"https://github.com/jgm/pandoc/releases/download/{PANDOC}/pandoc-{PANDOC}-linux-arm64.tar.gz",
         "e09b97d7313bd417a35bde6c747c458486ec84f696e1cfd0f8c527cd69c89433"),
        ("typst", f"https://github.com/typst/typst/releases/download/v{TYPST}/typst-aarch64-unknown-linux-musl.tar.xz",
         "491b101aa40a3a7ea82a3f8a6232cabb4e6a7e233810082e5ac812d43fdcd47a"),
    ],
}


def install(name: str, url: str, digest: str, dest: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "archive"
        with urllib.request.urlopen(url, timeout=120) as resp, archive.open("wb") as out:
            shutil.copyfileobj(resp, out)
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual != digest:
            sys.exit(f"{name}: checksum mismatch ({actual})")
        with tarfile.open(archive) as tar:
            member = next(m for m in tar.getmembers() if m.isfile() and Path(m.name).name == name)
            src = tar.extractfile(member)
            target = dest / name
            with target.open("wb") as out:
                shutil.copyfileobj(src, out)
            target.chmod(0o755)
    print(f"installed {name} from {url}")


def main() -> None:
    arch = platform.machine()
    if arch not in TOOLS:
        sys.exit(f"unsupported architecture {arch}")
    for name, url, digest in TOOLS[arch]:
        install(name, url, digest, Path("/usr/local/bin"))


if __name__ == "__main__":
    main()
