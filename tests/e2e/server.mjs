// Starts a throwaway DocWizard server for the browser tests: its own data folders, open
// registration, the render worker as a child process. Python, pandoc and Typst must be installed.
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, chmodSync } from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

export async function startServer(env = {}) {
  const dir = mkdtempSync(path.join(tmpdir(), "dw-e2e-"));
  chmodSync(dir, 0o755); // the worker's job users must reach the job folders when it runs as root
  const port = await freePort();
  // The app on localhost, previews and published sites on 127.0.0.1: two origins, as in production.
  const base = `http://localhost:${port}`;
  const config = {
    DOCWIZARD_CONFIG: path.join(dir, "none.toml"),
    DOCWIZARD_SERVER_HOST: "127.0.0.1",
    DOCWIZARD_SERVER_PORT: String(port),
    DOCWIZARD_SERVER_PUBLIC_URL: base,
    DOCWIZARD_SERVER_PUBLISH_URL: `http://127.0.0.1:${port}`,
    DOCWIZARD_SERVER_PREVIEW_URL: `http://127.0.0.1:${port}/_preview`,
    DOCWIZARD_SERVER_DATA_DIR: path.join(dir, "app"),
    DOCWIZARD_SERVER_PUBLISH_DIR: path.join(dir, "publish"),
    DOCWIZARD_SERVER_JOBS_DIR: path.join(dir, "jobs"),
    DOCWIZARD_SERVER_CACHE_DIR: path.join(dir, "cache"),
    DOCWIZARD_ACCOUNTS_REGISTRATION: "open",
    DOCWIZARD_RENDER_SPAWN_WORKER: "true",
    DOCWIZARD_SYNC_ENABLED: "false",
    DOCWIZARD_LOG_LEVEL: "WARNING",
    DOCWIZARD_SCRYPT_LOG_N: "10",
  };
  const python = process.env.PYTHON ?? "python3";
  const proc = spawn(python, ["-m", "docwizard", "serve"], {
    cwd: ROOT,
    env: { ...process.env, PYTHONPATH: path.join(ROOT, "server"), ...config, ...env },
    stdio: ["ignore", "inherit", "inherit"],
  });
  let exited = null;
  proc.on("exit", (code) => { exited = code ?? -1; });
  for (let i = 0; i < 150; i++) {
    if (exited !== null) throw new Error(`the server exited with ${exited}`);
    try {
      const r = await fetch(`${base}/healthz`);
      if (r.ok) break;
    } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 200));
  }
  return {
    base,
    dir,
    async stop() {
      if (exited === null) {
        proc.kill("SIGTERM");
        await new Promise((r) => { proc.on("exit", r); setTimeout(r, 5000); });
      }
      rmSync(dir, { recursive: true, force: true });
    },
  };
}
