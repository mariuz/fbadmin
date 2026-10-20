/**
 * Local databases (milestone 1.2): create or open a .fdb file on this computer and work on it
 * through the Firebird embedded engine, without a server. Available in the desktop app (which
 * bundles the engine) and in development with FBADMIN_EMBEDDED_DIR; never in server mode.
 * Registered files live under the "Local databases" entry of the tree (backend: api/embedded.py).
 */
import { useEffect, useState } from "react";
import { errMsg, keys, useApp, type AppApi } from "../../context";
import { Modal } from "../../components/Modal";
import { IconDatabase, IconFolder, IconPlus } from "../../components/Icons";
import type { MenuItem } from "../../components/ContextMenu";
import { registerMenu, registerMenuFilter, registerTopbar } from "../../registry";
import { t } from "../../i18n";
import { desktopFilePicker, eapi, embeddedStatus, type EmbeddedStatus } from "./api";

type Mode = "create" | "open";

const sep = (path: string) => (path.includes("\\") && !path.includes("/") ? "\\" : "/");

function LocalDatabaseDialog(props: { mode: Mode; status: EmbeddedStatus }) {
  const app = useApp();
  const [mode, setMode] = useState<Mode>(props.mode);
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [pageSize, setPageSize] = useState(16384);
  const [charset, setCharset] = useState("UTF8");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const picker = desktopFilePicker();
  const dir = props.status.data_directory;
  const suggested = `${dir}${sep(dir)}${(name.trim() || "database").replace(/[\\/:*?"<>|]/g, "_")}.fdb`;
  const close = () => app.showDialog(null);

  const browse = async () => {
    try {
      const p = await picker!(mode === "create", path || suggested);
      if (p) setPath(p);
    } catch (e) {
      setError(errMsg(e));
    }
  };

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const target = path.trim() || (mode === "create" ? suggested : "");
      const r = await eapi.add({ path: target, name: name.trim() || null, create: mode === "create", page_size: pageSize, charset });
      await app.reloadServers();
      app.invalidate(keys.server(r.server_id));
      app.select({ kind: "database", serverId: r.server_id, dbId: r.database.id });
      app.toast(r.created ? t("Local database {name} created", { name: r.database.name })
        : t("Local database {name} opened", { name: r.database.name }));
      close();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={mode === "create" ? t("Create local database") : t("Open local database file")}
      onClose={close}
      footer={
        <>
          {error && <span className="err" style={{ whiteSpace: "pre-wrap" }}>{error}</span>}
          <button className="btn" onClick={close}>{t("Cancel")}</button>
          <button className="btn primary" disabled={busy || (mode === "open" && !path.trim())} onClick={submit}>
            {mode === "create" ? t("Create") : t("Open")}
          </button>
        </>
      }
    >
      <div className="form">
        <span />
        <div role="radiogroup" style={{ display: "flex", gap: 16 }}>
          <label><input type="radio" checked={mode === "create"} onChange={() => setMode("create")} /> {t("Create a new file")}</label>
          <label><input type="radio" checked={mode === "open"} onChange={() => setMode("open")} /> {t("Open an existing file")}</label>
        </div>
        <label className="l" htmlFor="emb-path">{t("Database file")}</label>
        <div style={{ display: "flex", gap: 6 }}>
          <input id="emb-path" className="text" style={{ flex: 1 }} autoFocus value={path}
            placeholder={mode === "create" ? suggested : t("Full path of a .fdb file")} onChange={(e) => setPath(e.target.value)} />
          {picker && <button className="btn" onClick={browse}>{t("Browse…")}</button>}
        </div>
        <label className="l" htmlFor="emb-name">{t("Display name")}</label>
        <input id="emb-name" className="text" value={name} placeholder={t("defaults to file name")} onChange={(e) => setName(e.target.value)} />
        {mode === "create" && (
          <>
            <label className="l">{t("Page size")}</label>
            <select className="text" value={pageSize} onChange={(e) => setPageSize(Number(e.target.value))}>
              {[4096, 8192, 16384, 32768].map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
            <label className="l">{t("Default charset")}</label>
            <select className="text" value={charset} onChange={(e) => setCharset(e.target.value)}>
              {["UTF8", "NONE", "WIN1250", "WIN1251", "WIN1252", "ISO8859_1"].map((c) => <option key={c}>{c}</option>)}
            </select>
          </>
        )}
        <span />
        <span className="muted">
          {t("The file is opened by the Firebird {version} embedded engine inside fbadmin: no server is needed, but no other program (or Firebird server) can use the file while fbadmin has it open.",
            { version: props.status.version ?? "" })}
        </span>
      </div>
    </Modal>
  );
}

async function openDialog(app: AppApi, mode: Mode) {
  try {
    const st = await embeddedStatus();
    if (!st.available) {
      app.toast(st.reason, "error");
      return;
    }
    app.showDialog(<LocalDatabaseDialog mode={mode} status={st} />);
  } catch (e) {
    app.toast(errMsg(e), "error");
  }
}

function LocalDatabaseButton(props: { app: AppApi }) {
  const [st, setSt] = useState<EmbeddedStatus | null>(null);
  useEffect(() => {
    let live = true;
    embeddedStatus().then((s) => live && setSt(s)).catch(() => { /* older backend: no button */ });
    return () => { live = false; };
  }, []);
  if (!st?.available) return null;
  return (
    <button className="tb" title={t("Create or open a database file on this computer (no server needed)")}
      onClick={() => props.app.showDialog(<LocalDatabaseDialog mode="create" status={st} />)}>
      <IconDatabase /> {t("Local database")}
    </button>
  );
}

registerTopbar({ id: "local-database", render: (app) => <LocalDatabaseButton app={app} /> });

registerMenu((app, sel): MenuItem[] => {
  const srv = app.servers.find((s) => s.id === sel.serverId);
  if (!srv?.embedded || sel.kind !== "server") return [];
  return [
    { header: t("Local databases") },
    { label: t("Create local database…"), icon: <IconPlus />, onClick: () => openDialog(app, "create") },
    { label: t("Open local database file…"), icon: <IconFolder />, onClick: () => openDialog(app, "open") },
  ];
});

// Items that do not apply to the embedded engine: there is no server to edit, the engine does not
// authenticate (no user management), the trace and encryption plugins are not bundled, and
// "Register/Create database" are replaced by "Open/Create local database".
const HIDDEN = [() => t("Edit server…"), () => t("Register database…"), () => t("Create database…"),
  () => t("Trace sessions"), () => t("Trace…"), () => t("Encryption…"), () => t("Firebird users (Services API)…")];

registerMenuFilter((app, sel, items) => {
  const srv = app.servers.find((s) => s.id === sel.serverId);
  if (!srv?.embedded) return items;
  const hidden = new Set(HIDDEN.map((f) => f()));
  const out = items.filter((it) => !("label" in it) || !hidden.has(it.label));
  // drop separators and headers left without items
  return out.filter((it, i, a) => {
    if ("separator" in it) return i > 0 && i < a.length - 1 && !("separator" in a[i - 1]);
    if ("header" in it) return i < a.length - 1 && "label" in a[i + 1];
    return true;
  });
});
