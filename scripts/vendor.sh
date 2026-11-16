#!/bin/sh
# Dev-only: fetch exact versions of the frontend dependencies, bundle
# CodeMirror and Lezer into one minified ES module, copy Temml, and write
# web/vendor/MANIFEST with versions, licenses and SHA-256 (§17.5).
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
VENDOR="$ROOT/web/vendor"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

PACKAGES="
@codemirror/state@6.7.6
@codemirror/view@6.43.15
@codemirror/commands@6.11.1
@codemirror/language@6.13.1
@codemirror/search@6.7.2
@codemirror/autocomplete@6.20.4
@codemirror/collab@6.1.1
@lezer/common@1.5.5
@lezer/highlight@1.2.5
@lezer/lr@1.4.12
@lezer/markdown@1.8.1
temml@0.14.0
pdfjs-dist@4.10.38
"
ESBUILD=esbuild@0.28.2

cd "$WORK"
npm init -y >/dev/null
# shellcheck disable=SC2086
npm install --no-audit --no-fund --omit=optional --save-exact $PACKAGES $ESBUILD >/dev/null
cp "$ROOT/scripts/vendor-entry.js" entry.js

mkdir -p "$VENDOR/temml" "$VENDOR/pdfjs"
npx esbuild entry.js --bundle --minify --format=esm --target=es2022 \
  --legal-comments=none --outfile="$VENDOR/codemirror.js"
cp node_modules/temml/dist/temml.mjs "$VENDOR/temml/temml.mjs"
cp node_modules/temml/LICENSE "$VENDOR/temml/LICENSE"
cp node_modules/pdfjs-dist/build/pdf.min.mjs "$VENDOR/pdfjs/pdf.min.mjs"
cp node_modules/pdfjs-dist/build/pdf.worker.min.mjs "$VENDOR/pdfjs/pdf.worker.min.mjs"
cp node_modules/pdfjs-dist/LICENSE "$VENDOR/pdfjs/LICENSE"

{
  echo "# Vendored frontend code. Regenerate with scripts/vendor.sh."
  echo "# package version license"
  npm ls --all --json | node -e '
    const tree = JSON.parse(require("fs").readFileSync(0, "utf8"));
    const seen = new Map();
    (function walk(deps) {
      for (const [name, info] of Object.entries(deps || {})) {
        if (name === "esbuild" || name.startsWith("@esbuild/") || name.startsWith("@napi-rs/")) continue;
        const pkgPath = require("path").join(process.cwd(), "node_modules", name, "package.json");
        if (!seen.has(name) && require("fs").existsSync(pkgPath)) {
          const pkg = require(pkgPath);
          seen.set(name, `${name} ${info.version} ${pkg.license}`);
        }
        walk(info.dependencies);
      }
    })(tree.dependencies);
    console.log([...seen.values()].sort().join("\n"));'
  echo "# sha256 file"
  cd "$VENDOR"
  sha256sum codemirror.js temml/temml.mjs pdfjs/pdf.min.mjs pdfjs/pdf.worker.min.mjs
} > "$VENDOR/MANIFEST"
echo "Wrote $VENDOR/MANIFEST"
