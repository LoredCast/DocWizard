// One WebSocket per tab at /ws, multiplexed by document (ARCHITECTURE.md §8.2).
// Reconnects with capped exponential backoff and tells each document to reopen.

const MIN_DELAY = 500;
const MAX_DELAY = 30000;

export class Socket {
  constructor(url = defaultUrl()) {
    this.url = url;
    this.ws = null;
    this.state = "connecting";
    this.docs = new Map();
    this.stateListeners = new Set();
    this.errorListeners = new Set();
    this.delay = MIN_DELAY;
    this.timer = null;
    this.connect();
    window.addEventListener("online", () => this.retryNow());
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && this.state === "closed") this.retryNow();
    });
  }

  connect() {
    clearTimeout(this.timer);
    this.setState("connecting");
    let ws;
    try {
      ws = new WebSocket(this.url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.addEventListener("open", () => {
      this.delay = MIN_DELAY;
      this.setState("open");
      for (const handler of this.docs.values()) handler.onOpen?.();
    });
    ws.addEventListener("message", (event) => {
      let msg;
      try { msg = JSON.parse(event.data); } catch { return; }
      const handler = msg.doc ? this.docs.get(msg.doc) : null;
      if (handler) handler.onMessage(msg);
      else if (msg.t === "error") for (const fn of this.errorListeners) fn(msg);
    });
    ws.addEventListener("close", () => {
      if (this.ws !== ws) return;
      this.ws = null;
      for (const handler of this.docs.values()) handler.onClose?.();
      this.scheduleReconnect();
    });
  }

  scheduleReconnect() {
    this.setState("closed");
    const jitter = this.delay * (0.5 + Math.random() * 0.5);
    this.timer = setTimeout(() => this.connect(), jitter);
    this.delay = Math.min(MAX_DELAY, this.delay * 2);
  }

  retryNow() {
    if (this.state !== "closed") return;
    this.delay = MIN_DELAY;
    this.connect();
  }

  get isOpen() {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  send(msg) {
    if (!this.isOpen) return false;
    this.ws.send(JSON.stringify(msg));
    return true;
  }

  // handler: {onOpen(), onMessage(msg), onClose()}
  register(docId, handler) {
    this.docs.set(docId, handler);
    if (this.isOpen) handler.onOpen?.();
    return () => {
      this.docs.delete(docId);
      this.send({ t: "close", doc: docId });
    };
  }

  onState(fn) {
    this.stateListeners.add(fn);
    fn(this.state);
    return () => this.stateListeners.delete(fn);
  }

  onError(fn) {
    this.errorListeners.add(fn);
  }

  setState(state) {
    if (state === this.state) return;
    this.state = state;
    for (const fn of this.stateListeners) fn(state);
  }
}

function defaultUrl() {
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${location.host}/ws`;
}
