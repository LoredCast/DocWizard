// IndexedDB store for documents with unconfirmed edits (ARCHITECTURE.md §6.5).
// Each record: {docId, clientID, syncedVersion, baseText, text, offlineSince, updatedAt}.
// Everything here is best effort: private windows and full disks just mean no offline copy.

const DB_NAME = "docwizard";
const DB_VERSION = 1;
const KEEP = 50;
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
        if (!db.objectStoreNames.contains("docs")) {
          db.createObjectStore("docs", { keyPath: "docId" }).createIndex("updatedAt", "updatedAt");
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => resolve(null);
      req.onblocked = () => resolve(null);
    });
  }
  return dbPromise;
}

function run(mode, fn) {
  return open().then((db) => new Promise((resolve) => {
    if (!db) { resolve(null); return; }
    let result = null;
    try {
      const tx = db.transaction("docs", mode);
      const req = fn(tx.objectStore("docs"));
      if (req) req.onsuccess = () => { result = req.result ?? null; };
      tx.oncomplete = () => resolve(result);
      tx.onerror = () => resolve(null);
      tx.onabort = () => resolve(null);
    } catch {
      resolve(null);
    }
  }));
}

export const getDoc = (docId) => run("readonly", (store) => store.get(docId));

export async function putDoc(record) {
  await run("readwrite", (store) => store.put({ ...record, updatedAt: Date.now() }));
}

export const deleteDoc = (docId) => run("readwrite", (store) => store.delete(docId));

// Keep the newest KEEP records.
export async function prune() {
  await run("readwrite", (store) => {
    const req = store.index("updatedAt").openCursor(null, "prev");
    let seen = 0;
    req.onsuccess = () => {
      const cursor = req.result;
      if (!cursor) return;
      if (++seen > KEEP) cursor.delete();
      cursor.continue();
    };
    return null;
  });
}
