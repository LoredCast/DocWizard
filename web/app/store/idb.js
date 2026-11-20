// The browser's copy of the user's work (ARCHITECTURE.md §6.5). Stores:
//   docs:      {docId, owner, url, meta, clientID, syncedVersion, baseText, text, merges,
//               offlineSince, pinned, updatedAt}, kept for every recently opened document
//              (the last KEEP plus all pinned ones), so a reload works offline
//   recent:    {owner, items, savedAt}, a mirror of the home page's recent list
//   citations: {projectId, owner, etag, items}
//   layouts:   {id, owner, layout}
//   projects:  {projectId, owner, name, savedAt}: projects kept available offline as a whole;
//              their documents' records carry kept: projectId and aren't pruned
//   kits:      {key: "<projectId>:<docId>", owner, etag, kit, savedAt}: the last render kit of a
//              document previewed in the browser (§6.4), so the PDF preview also works offline
// Everything here is best effort: private windows and full disks just mean no offline copy.

const DB_NAME = "docwizard";
const DB_VERSION = 4;
const KEEP = 50;
const OWNER_KEY = "dw.owner";
let dbPromise = null;

function open() {
  if (!dbPromise) {
    dbPromise = new Promise((resolve) => {
      let req;
      try {
        req = indexedDB.open(DB_NAME, DB_VERSION);
      } catch {
        resolve(null);
        return;
      }
      req.onupgradeneeded = () => {
        const db = req.result;
        const docs = db.objectStoreNames.contains("docs")
          ? req.transaction.objectStore("docs")
          : db.createObjectStore("docs", { keyPath: "docId" });
        if (!docs.indexNames.contains("updatedAt")) docs.createIndex("updatedAt", "updatedAt");
        if (!docs.indexNames.contains("url")) docs.createIndex("url", "url");
        if (!db.objectStoreNames.contains("recent")) db.createObjectStore("recent", { keyPath: "owner" });
        if (!db.objectStoreNames.contains("citations")) db.createObjectStore("citations", { keyPath: "projectId" });
        if (!db.objectStoreNames.contains("layouts")) db.createObjectStore("layouts", { keyPath: "id" });
        if (!db.objectStoreNames.contains("projects")) db.createObjectStore("projects", { keyPath: "projectId" });
        if (!db.objectStoreNames.contains("kits")) db.createObjectStore("kits", { keyPath: "key" });
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => resolve(null);
      req.onblocked = () => resolve(null);
    });
  }
  return dbPromise;
}

function run(storeName, mode, fn) {
  return open().then((db) => new Promise((resolve) => {
    if (!db) { resolve(null); return; }
    let result = null;
    try {
      const tx = db.transaction(storeName, mode);
      const req = fn(tx.objectStore(storeName));
      if (req) req.onsuccess = () => { result = req.result ?? null; };
      tx.oncomplete = () => resolve(result);
      tx.onerror = () => resolve(null);
      tx.onabort = () => resolve(null);
    } catch {
      resolve(null);
    }
  }));
}

// -- Owner ------------------------------------------------------------------
// One browser can be used by several people in turn. Records carry the account
// that wrote them; the offline pages show only the last account's records.

export function currentOwner() {
  const meta = document.querySelector('meta[name="dw-user"]')?.content;
  if (meta) {
    try { localStorage.setItem(OWNER_KEY, meta); } catch { /* private window */ }
    return meta;
  }
  try { return localStorage.getItem(OWNER_KEY) ?? ""; } catch { return ""; }
}

// Records from before owners were recorded belong to whoever opens them next.
const mine = (record) => record && (record.owner === undefined || record.owner === currentOwner()) ? record : null;

// -- Documents ----------------------------------------------------------------

export const getDoc = (docId) => run("docs", "readonly", (s) => s.get(docId)).then(mine);

export const getDocByUrl = (url) => run("docs", "readonly", (s) => s.index("url").getAll(url))
  .then((rows) => (rows ?? []).filter(mine).sort((a, b) => b.updatedAt - a.updatedAt)[0] ?? null);

export const listDocs = () => run("docs", "readonly", (s) => s.getAll())
  .then((rows) => (rows ?? []).filter(mine).sort((a, b) => b.updatedAt - a.updatedAt));

export async function putDoc(record) {
  await run("docs", "readwrite", (s) => s.put({ ...record, owner: currentOwner(), updatedAt: Date.now() }));
}

export const deleteDoc = (docId) => run("docs", "readwrite", (s) => s.delete(docId));

export const hasPendingEdits = (r) => r.text !== r.baseText || (r.merges?.length ?? 0) > 0;

// Keep the newest KEEP records, all pinned ones, those of projects kept offline and all with
// edits not yet on the server.
export async function prune() {
  await run("docs", "readwrite", (store) => {
    const req = store.index("updatedAt").openCursor(null, "prev");
    let seen = 0;
    req.onsuccess = () => {
      const cursor = req.result;
      if (!cursor) return;
      const r = cursor.value;
      if (!r.pinned && !r.kept && !hasPendingEdits(r) && ++seen > KEEP) cursor.delete();
      cursor.continue();
    };
    return null;
  });
}

// -- Recent list, citations, layouts --------------------------------------------

export const putRecent = (items) => run("recent", "readwrite",
  (s) => s.put({ owner: currentOwner(), items, savedAt: Date.now() }));

// Pins are set on the home page; the offline copies follow so pruning keeps them.
export const markPinned = (pinned) => run("docs", "readwrite", (store) => {
  const req = store.openCursor();
  req.onsuccess = () => {
    const cursor = req.result;
    if (!cursor) return;
    const r = cursor.value;
    if (mine(r) && Boolean(r.pinned) !== pinned.has(r.docId)) cursor.update({ ...r, pinned: pinned.has(r.docId) });
    cursor.continue();
  };
  return null;
});

export const getRecent = () => run("recent", "readonly", (s) => s.get(currentOwner()));

export const getCitations = (projectId) => run("citations", "readonly", (s) => s.get(projectId)).then(mine);

export const putCitations = (projectId, etag, items) => run("citations", "readwrite",
  (s) => s.put({ projectId, owner: currentOwner(), etag, items }));

export const getKit = (key) => run("kits", "readonly", (s) => s.get(key)).then(mine);

export const putKit = (key, etag, kit) => run("kits", "readwrite",
  (s) => s.put({ key, owner: currentOwner(), etag, kit, savedAt: Date.now() }));

export const putLayout = (layout) => run("layouts", "readwrite",
  (s) => s.put({ id: layout.id, owner: currentOwner(), layout }));

// -- Projects kept offline ------------------------------------------------------

export const listKeptProjects = () => run("projects", "readonly", (s) => s.getAll())
  .then((rows) => (rows ?? []).filter(mine));

export const getKeptProject = (projectId) => run("projects", "readonly", (s) => s.get(projectId)).then(mine);

export const putKeptProject = (project) => run("projects", "readwrite",
  (s) => s.put({ ...project, owner: currentOwner() }));

// Stops keeping a project: its documents become ordinary recent copies again.
export async function dropKeptProject(projectId) {
  await run("projects", "readwrite", (s) => s.delete(projectId));
  await run("docs", "readwrite", (store) => {
    const req = store.openCursor();
    req.onsuccess = () => {
      const cursor = req.result;
      if (!cursor) return;
      const r = cursor.value;
      if (mine(r) && r.kept === projectId) cursor.update({ ...r, kept: null });
      cursor.continue();
    };
    return null;
  });
  await prune();
}

// -- Logging out ----------------------------------------------------------------

// Drops everything except documents with edits that haven't reached the server:
// those are sent the next time this account opens them.
export async function forgetAccount() {
  await run("docs", "readwrite", (store) => {
    const req = store.openCursor();
    req.onsuccess = () => {
      const cursor = req.result;
      if (!cursor) return;
      if (!hasPendingEdits(cursor.value)) cursor.delete();
      cursor.continue();
    };
    return null;
  });
  for (const name of ["recent", "citations", "layouts", "projects", "kits"]) await run(name, "readwrite", (s) => s.clear());
}
