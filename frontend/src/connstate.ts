/**
 * Disconnect that really disconnects (issue #9), browser side.
 *
 * - `emitDisconnected(serverId, dbId?)` after Disconnect: the app closes that server's (database's) tabs and
 *   the tree collapses its node (`onDisconnected`).
 * - Each browser tab says hello to the backend on load and bye when it is closed (`pagehide`, keepalive);
 *   when the last tab is gone the backend disconnects everything (`FBADMIN_DISCONNECT_ON_CLOSE`). A reload
 *   keeps its tab id (sessionStorage) and says hello again in time.
 * - Leaving the page while a Query Tool has an open transaction asks first (`setOpenTransaction`).
 */

export interface DisconnectedEvent { serverId: string; dbId?: string }

const EVENT = "fbadmin:disconnected";

export function emitDisconnected(serverId: string, dbId?: string) {
  window.dispatchEvent(new CustomEvent<DisconnectedEvent>(EVENT, { detail: { serverId, dbId } }));
}

export function onDisconnected(fn: (e: DisconnectedEvent) => void): () => void {
  const h = (e: Event) => fn((e as CustomEvent<DisconnectedEvent>).detail);
  window.addEventListener(EVENT, h);
  return () => window.removeEventListener(EVENT, h);
}

/** Whether a tab of `serverId` (and `dbId`) belongs to what was disconnected. */
export function affected(e: DisconnectedEvent, serverId?: string, dbId?: string): boolean {
  return !!serverId && serverId === e.serverId && (e.dbId === undefined || dbId === e.dbId);
}

// --- open transactions: ask before leaving ------------------------------------------------
const openTx = new Set<string>();
export function setOpenTransaction(key: string, open: boolean) {
  if (open) openTx.add(key);
  else openTx.delete(key);
}

// --- this browser tab ---------------------------------------------------------------------
function tabId(): string {
  try {
    let id = sessionStorage.getItem("fbadmin-tab");
    if (!id) {
      id = `tab-${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;
      sessionStorage.setItem("fbadmin-tab", id);
    }
    return id;
  } catch {
    return `tab-${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;
  }
}

function csrf(): Record<string, string> {
  const m = document.cookie.match(/(?:^|;\s*)fbadmin_csrf=([^;]+)/);
  return m ? { "X-CSRF-Token": decodeURIComponent(m[1]) } : {};
}

function post(path: string, keepalive = false) {
  return fetch(path, {
    method: "POST",
    keepalive,
    headers: { "Content-Type": "application/json", "X-Fbadmin-Client": "ui", ...csrf() },
    body: JSON.stringify({ tab: tabId() }),
  }).catch(() => undefined);
}

let started = false;
/** Say hello now and bye when the page goes away; called once by the app. */
export function startTabTracking() {
  if (started) return;
  started = true;
  void post("/api/client/hello");
  window.addEventListener("pagehide", () => void post("/api/client/bye", true));
  // back from the back/forward cache: the bye was already sent
  window.addEventListener("pageshow", (e) => { if (e.persisted) void post("/api/client/hello"); });
  window.addEventListener("beforeunload", (e) => {
    if (openTx.size) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
}
