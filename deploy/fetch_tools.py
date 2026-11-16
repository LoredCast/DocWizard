"""Download pinned pandoc and Typst releases and layout fonts, check their SHA-256 and install them.

Used by Dockerfile.worker; stdlib only, so the image needs no curl or xz.
Update versions and checksums together.

    python3 -I deploy/fetch_tools.py               # pandoc and Typst into /usr/local/bin, fonts too
    python3 -I deploy/fetch_tools.py --fonts-only  # only the fonts
    python3 -I deploy/fetch_tools.py --fonts DIR   # fonts into DIR instead of the default

Typst runs with --ignore-system-fonts; the render jobs add the fonts folder below to its font
path (server/docwizard/render/design.py), so every layout can use these fonts.
"""

import hashlib
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
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


FONTS_DIR = Path("/usr/local/share/docwizard/fonts")
GOOGLE_FONTS = "https://raw.githubusercontent.com/google/fonts/7af0d90b4d532ffa4b041937b560c6df4e7b1e95/ofl"
NOTO = "https://github.com/notofonts/latin-greek-cyrillic/releases/download"
# Static fonts under the SIL Open Font License: (file, url, sha256).
FONT_FILES = [
    ("Tinos-Regular.ttf", f"{GOOGLE_FONTS}/tinos/Tinos-Regular.ttf",
     "60a0e8ef0c04dd5dd69ffe91025fa2ae5836cbd35600a82ba031977557e2cb61"),
    ("Tinos-Bold.ttf", f"{GOOGLE_FONTS}/tinos/Tinos-Bold.ttf",
     "393269dbab8899f938db19783eca5eac92eb431f7ae0ab45b8349ca895f1a06b"),
    ("Tinos-Italic.ttf", f"{GOOGLE_FONTS}/tinos/Tinos-Italic.ttf",
     "5942266ed398b155d7dc23e36833e7ec6be988f2439bdbeb8ef1bede808eaa91"),
    ("Tinos-BoldItalic.ttf", f"{GOOGLE_FONTS}/tinos/Tinos-BoldItalic.ttf",
     "a5de79f0fe863ea0954757acb3d47b3ccd0a930ce3dd5b97230cd3866790a06e"),
    ("Carlito-Regular.ttf", f"{GOOGLE_FONTS}/carlito/Carlito-Regular.ttf",
     "f6418f708baede9789daef5d458c0f53d2a888af9820e8062934e504fedc6595"),
    ("Carlito-Bold.ttf", f"{GOOGLE_FONTS}/carlito/Carlito-Bold.ttf",
     "bb5d20f79b82599ec72983597437373a80f2d2085fa91fc144fd74e876a594db"),
    ("Carlito-Italic.ttf", f"{GOOGLE_FONTS}/carlito/Carlito-Italic.ttf",
     "0b019225e58d702bfedcbd35c21696769f8ee115cb6343f84c2f240312450d1c"),
    ("Carlito-BoldItalic.ttf", f"{GOOGLE_FONTS}/carlito/Carlito-BoldItalic.ttf",
     "b32928186c119599e03ca6a1ffc680fdcb7fac95772f4b95d989cf6cd3861517"),
    ("Cousine-Regular.ttf", f"{GOOGLE_FONTS}/cousine/Cousine-Regular.ttf",
     "1da22250675fc4c42fcf3a9736c44bc0570516105331443b663fd5cfbd1412fe"),
    ("Cousine-Bold.ttf", f"{GOOGLE_FONTS}/cousine/Cousine-Bold.ttf",
     "17c8a7245156d2253531c9e529474937b09d9f641c5ae7695c5e33f22822eef4"),
    ("Cousine-Italic.ttf", f"{GOOGLE_FONTS}/cousine/Cousine-Italic.ttf",
     "ea2a76ae3d0ece9cd59f0d30fdc08dd70e8f5f457beee5b0852a7b50c2286c7c"),
    ("Cousine-BoldItalic.ttf", f"{GOOGLE_FONTS}/cousine/Cousine-BoldItalic.ttf",
     "848e858726fee0ae27b754e4cd6a2755209bf1428a8c91f747696d58c33906c3"),
]
# Noto Serif (the University of Oxford's recommended font) and Noto Sans: static cuts from the release zips.
FONT_ZIPS = [
    (f"{NOTO}/NotoSerif-v2.015/NotoSerif-v2.015.zip",
     "0e9a43c8a4b94ac76f55069ed1d7385bbcaf6b99527a94deb5619e032b7e76c1", "NotoSerif/unhinted/ttf/",
     ["NotoSerif-Regular.ttf", "NotoSerif-Bold.ttf", "NotoSerif-Italic.ttf", "NotoSerif-BoldItalic.ttf"]),
    (f"{NOTO}/NotoSans-v2.015/NotoSans-v2.015.zip",
     "0c34df072a3fa7efbb7cbf34950e1f971a4447cffe365d3a359e2d4089b958f5", "NotoSans/unhinted/ttf/",
     ["NotoSans-Regular.ttf", "NotoSans-Bold.ttf", "NotoSans-Italic.ttf", "NotoSans-BoldItalic.ttf"]),
]


def _download(url: str, dest: Path, digest: str) -> None:
    with urllib.request.urlopen(url, timeout=300) as resp, dest.open("wb") as out:
        shutil.copyfileobj(resp, out)
    h = hashlib.sha256()
    with dest.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != digest:
        sys.exit(f"{url}: checksum mismatch ({h.hexdigest()})")


def install_fonts(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for name, url, digest in FONT_FILES:
            if (dest / name).is_file() and hashlib.sha256((dest / name).read_bytes()).hexdigest() == digest:
                continue
            _download(url, Path(tmp) / name, digest)
            shutil.copyfile(Path(tmp) / name, dest / name)
        for url, digest, prefix, names in FONT_ZIPS:
            if all((dest / n).is_file() for n in names):
                continue
            archive = Path(tmp) / "fonts.zip"
            _download(url, archive, digest)
            with zipfile.ZipFile(archive) as zf:
                for n in names:
                    (dest / n).write_bytes(zf.read(prefix + n))
            archive.unlink()
    for f in dest.iterdir():
        f.chmod(0o644)
    dest.chmod(0o755)
    print(f"installed {len(FONT_FILES) + sum(len(z[3]) for z in FONT_ZIPS)} fonts into {dest}")


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
    args = sys.argv[1:]
    fonts = FONTS_DIR
    if "--fonts" in args:
        fonts = Path(args[args.index("--fonts") + 1])
    install_fonts(fonts)
    if "--fonts-only" in args or "--fonts" in args:
        return
    arch = platform.machine()
    if arch not in TOOLS:
        sys.exit(f"unsupported architecture {arch}")
    for name, url, digest in TOOLS[arch]:
        install(name, url, digest, Path("/usr/local/bin"))


if __name__ == "__main__":
    main()
