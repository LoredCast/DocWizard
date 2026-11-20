// The PDF preview made in the browser (ARCHITECTURE.md §6.4): pandoc and Typst compiled to
// WebAssembly turn a render kit (/api/projects/{id}/render-kit) into the PDF the render worker
// would make. Messages: {id, kit, text} where *text* replaces the kit's file for the open
// document; answers {id, pdf} (transferred) or {id, error}. Both programs stay loaded between
// renders, and Typst keeps its fonts while the kit's font list stays the same.
import { WASI, File, Directory, OpenFile, PreopenDirectory, ConsoleStdout } from "../../vendor/browser-render/wasi/index.js";
import initTypst, { TypstCompilerBuilder } from "../../vendor/browser-render/typst_ts_web_compiler.mjs";

// The page's CSP has no 'unsafe-eval'. Typst's glue builds a few functions from strings: one to
// find the global object, and the stubs of its file access (constants, the identity, an error). Those are made here without eval, and
// any other is refused with its source.
const CONSTANTS = { null: null, undefined, true: true, false: false };
globalThis.Function = new Proxy(Function, {
  construct(target, args) {
    const body = String(args.at(-1) ?? "").trim().replace(/;$/, "");
    if (body === "return this") return () => globalThis;
    if (args.length === 2 && body === `return ${String(args[0]).trim()}`) return (x) => x;
    const thrown = /^throw new Error\((['"])(.*)\1\)$/.exec(body);
    if (thrown) return () => { throw new Error(thrown[2]); };
    const m = /^return (-?\d+(?:\.\d+)?|null|undefined|true|false)$/.exec(body);
    if (m) {
      const value = m[1] in CONSTANTS ? CONSTANTS[m[1]] : Number(m[1]);
      return () => value;
    }
    throw new EvalError(`Function from a string isn't allowed: ${args.join(", ").slice(0, 200)}`);
  },
});

const VENDOR = new URL("../../vendor/browser-render/", import.meta.url);
const encoder = new TextEncoder();
const decoder = new TextDecoder();

// -- pandoc ------------------------------------------------------------------------

let pandoc = null;

async function loadPandoc() {
  if (pandoc) return pandoc;
  const args = ["pandoc.wasm", "+RTS", "-H64m", "-RTS"];
  const root = new Map();
  const wasi = new WASI(args, [], [
    new OpenFile(new File(new Uint8Array(), { readonly: true })),
    ConsoleStdout.lineBuffered(() => {}),
    ConsoleStdout.lineBuffered((line) => console.warn(`[pandoc] ${line}`)),
    new PreopenDirectory("/", root),
  ]);
  const module = await WebAssembly.compileStreaming(fetch(new URL("pandoc.wasm", VENDOR)));
  const instance = await WebAssembly.instantiate(module, { wasi_snapshot_preview1: wasi.wasiImport });
  wasi.initialize(instance);
  const ex = instance.exports;
  ex.__wasm_call_ctors?.();
  const view = () => new DataView(ex.memory.buffer);
  const argc = ex.malloc(4);
  view().setUint32(argc, args.length, true);
  const argv = ex.malloc(4 * (args.length + 1));
  args.forEach((arg, i) => {
    const bytes = encoder.encode(arg);
    const ptr = ex.malloc(bytes.length + 1);
    new Uint8Array(ex.memory.buffer, ptr, bytes.length + 1).set([...bytes, 0]);
    view().setUint32(argv + 4 * i, ptr, true);
  });
  view().setUint32(argv + 4 * args.length, 0, true);
  const argvp = ex.malloc(4);
  view().setUint32(argvp, argv, true);
  ex.hs_init_with_rtsopts(argc, argvp);
  pandoc = { ex, root };
  return pandoc;
}

function bytesOf(entry) {
  if ("text" in entry) return encoder.encode(entry.text);
  const bin = atob(entry.b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

// Puts *files* ({path: bytes}) into the directory map *root*, making the folders on the way.
function fill(root, files) {
  root.clear();
  for (const name of ["stdin", "stdout", "stderr", "warnings"]) root.set(name, new File(new Uint8Array()));
  const dirs = new Map([["", root]]);
  const folder = (path) => {
    if (dirs.has(path)) return dirs.get(path);
    const cut = path.lastIndexOf("/");
    const parent = folder(cut < 0 ? "" : path.slice(0, cut));
    const map = new Map();
    parent.set(path.slice(cut + 1), new Directory(map));
    dirs.set(path, map);
    return map;
  };
  for (const [path, data] of files) {
    const cut = path.lastIndexOf("/");
    folder(cut < 0 ? "" : path.slice(0, cut)).set(path.slice(cut + 1), new File(data));
  }
  // Directory links children to their parent when made; the folders above were filled later.
  const link = (map, parent) => {
    for (const entry of map.values()) {
      if (entry instanceof Directory) {
        entry.parent = parent;
        link(entry.contents, entry);
      }
    }
  };
  link(root, null);
}

function read(root, path) {
  let entry = { contents: root };
  for (const part of path.split("/")) {
    entry = entry?.contents?.get(part);
  }
  return entry instanceof File ? entry.data : null;
}

function runPandoc({ ex, root }, options, files) {
  fill(root, files);
  const json = encoder.encode(JSON.stringify(options));
  const ptr = ex.malloc(json.length);
  new Uint8Array(ex.memory.buffer, ptr, json.length).set(json);
  try {
    ex.convert(ptr, json.length);
  } finally {
    ex.free?.(ptr);
  }
  const errors = decoder.decode(read(root, "stderr") ?? new Uint8Array()).trim();
  const out = read(root, options["output-file"]);
  if (!out || (errors && !out.length)) throw new Error(errors || "pandoc made no output");
  return decoder.decode(out);
}

// -- Typst -------------------------------------------------------------------------

let typstReady = null;
let compiler = null;
let compilerFonts = "";

async function fontBytes(url) {
  const res = await fetch(url, { credentials: "same-origin" });
  if (!res.ok) throw new Error(`font ${url}: HTTP ${res.status}`);
  return new Uint8Array(await res.arrayBuffer());
}

async function typstFor(fonts, layoutFonts) {
  typstReady ??= initTypst({ module_or_path: new URL("typst_ts_web_compiler_bg.wasm", VENDOR) });
  await typstReady;
  const key = JSON.stringify([fonts, layoutFonts.map((f) => f.path)]);
  if (compiler && key === compilerFonts) return compiler;
  compiler?.free();
  compiler = null;
  const builder = new TypstCompilerBuilder();
  builder.set_dummy_access_model();
  for (const data of await Promise.all(fonts.map(fontBytes))) await builder.add_raw_font(data);
  for (const f of layoutFonts) await builder.add_raw_font(f.data);
  compiler = await builder.build();
  compilerFonts = key;
  return compiler;
}

function diagnostic(d) {
  if (typeof d === "string") return d;
  const where = d.path ? `${d.path}${d.range ? `:${d.range}` : ""}: ` : "";
  return `${where}${d.message ?? JSON.stringify(d)}`;
}

async function runTypst(kit, files, source) {
  const layoutFonts = kit.typst.layoutFonts.map((path) => ({ path, data: files.get(path) })).filter((f) => f.data);
  const typst = await typstFor(kit.typst.fonts, layoutFonts);
  typst.reset_shadow();
  for (const [path, data] of files) typst.map_shadow(`/${path}`, data);
  typst.add_source(`/${kit.typst.main}`, source);
  const out = typst.compile(`/${kit.typst.main}`, null, "pdf", 1);
  const pdf = out instanceof Uint8Array ? out : out?.result;
  if (!(pdf instanceof Uint8Array) || !pdf.length) {
    const errors = (out?.diagnostics ?? []).map(diagnostic).filter(Boolean);
    throw new Error(errors.join("\n") || "Typst made no PDF");
  }
  return pdf;
}

// -- Messages ----------------------------------------------------------------------

async function render(kit, text) {
  const files = new Map(Object.entries(kit.files).map(([path, entry]) => [path, bytesOf(entry)]));
  if (typeof text === "string" && kit.doc) files.set(`src/${kit.doc}`, encoder.encode(text));
  const source = runPandoc(await loadPandoc(), kit.pandoc, files);
  return runTypst(kit, files, source);
}

// One render at a time; a newer request replaces one still waiting.
let busy = false;
let waiting = null;

async function pump() {
  if (busy || !waiting) return;
  busy = true;
  const { id, kit, text } = waiting;
  waiting = null;
  const started = performance.now();
  try {
    const pdf = await render(kit, text);
    const copy = pdf.slice();
    self.postMessage({ id, pdf: copy, ms: Math.round(performance.now() - started) }, [copy.buffer]);
  } catch (err) {
    self.postMessage({ id, error: String(err?.message ?? err) });
  } finally {
    busy = false;
    pump();
  }
}

self.onmessage = (event) => {
  const msg = event.data ?? {};
  if (msg.type === "warm") {
    loadPandoc().catch(() => {});
    return;
  }
  if (waiting) self.postMessage({ id: waiting.id, superseded: true });
  waiting = msg;
  pump();
};
