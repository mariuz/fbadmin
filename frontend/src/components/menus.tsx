import { api } from "../api";
import { errMsg, keys, type AppApi } from "../context";
import { CREATE_TEMPLATES } from "../templates";
import { typeInfo, type Selection } from "../types";
import type { MenuItem } from "./ContextMenu";
import {
  BackupDialog, ConfirmDialog, CreateTableDialog, DatabaseDialog, RestoreDialog, ServerDialog, UserDialog,
} from "./dialogs";
import {
  IconActivity, IconDatabase, IconDownload, IconFilter, IconInfo, IconLog, IconPlay, IconPlus, IconRefresh,
  IconTable, IconTerminal, IconTrash, IconUpload, IconX,
} from "./Icons";

async function runJob(app: AppApi, p: Promise<unknown>, what: string) {
  try {
    await p;
    app.toast(`${what} started — see Processes`, "info");
    app.jobStarted();
  } catch (e) {
    app.toast(errMsg(e), "error");
  }
}

export function buildMenu(app: AppApi, sel: Selection): MenuItem[] {
  const srv = app.servers.find((s) => s.id === sel.serverId);
  if (!srv) return [];
  const sid = srv.id;

  if (sel.kind === "server") {
    return [
      { label: "Register database…", icon: <IconDatabase />, onClick: () => app.showDialog(<DatabaseDialog server={srv} mode="register" />) },
      { label: "Create database…", icon: <IconPlus />, onClick: () => app.showDialog(<DatabaseDialog server={srv} mode="create" />) },
      { label: "Restore backup…", icon: <IconUpload />, onClick: () => app.showDialog(<RestoreDialog server={srv} />) },
      { separator: true },
      { label: "Server info", icon: <IconInfo />, onClick: () => app.openTab({ kind: "serverinfo", title: `${srv.name} · info`, serverId: sid }) },
      { label: "Server log", icon: <IconLog />, onClick: () => app.openTab({ kind: "serverlog", title: `${srv.name} · log`, serverId: sid }) },
      { label: "Refresh", icon: <IconRefresh />, onClick: () => app.invalidate(keys.server(sid)) },
      { separator: true },
      { label: "Edit server…", onClick: () => app.showDialog(<ServerDialog server={srv} />) },
      {
        label: "Disconnect",
        icon: <IconX />,
        onClick: async () => {
          await api.disconnectServer(sid);
          await app.reloadServers();
          app.invalidate(keys.server(sid));
        },
      },
      {
        label: "Remove server",
        danger: true,
        icon: <IconTrash />,
        onClick: () =>
          app.showDialog(
            <ConfirmDialog
              title="Remove server"
              message={`Remove the registration of "${srv.name}"? Databases on the server are not touched.`}
              action="Remove"
              onConfirm={async () => {
                await api.deleteServer(sid);
                app.select(null);
                await app.reloadServers();
              }}
            />,
          ),
      },
    ];
  }

  const dbid = sel.dbId!;
  const db = srv.databases.find((d) => d.id === dbid);
  if (!db) return [];

  if (sel.kind === "database") {
    return [
      { label: "Query Tool", icon: <IconTerminal />, onClick: () => app.openQueryTool(sid, dbid) },
      { label: "Monitor", icon: <IconActivity />, onClick: () => app.openTab({ kind: "monitor", title: `${db.name} · monitor`, serverId: sid, dbId: dbid }) },
      { label: "Refresh", icon: <IconRefresh />, onClick: () => app.invalidate(keys.db(sid, dbid)) },
      { header: "Maintenance" },
      { label: "Backup…", icon: <IconDownload />, onClick: () => app.showDialog(<BackupDialog server={srv} dbId={dbid} />) },
      { label: "Sweep", onClick: () => runJob(app, api.sweep(sid, dbid), "Sweep") },
      { label: "Database statistics (gstat)", onClick: () => runJob(app, api.statistics(sid, dbid), "Statistics") },
      { label: "Recompute index selectivity", onClick: () => runJob(app, api.recomputeSelectivity(sid, dbid), "Index statistics") },
      { separator: true },
      { label: "Edit registration…", onClick: () => app.showDialog(<DatabaseDialog server={srv} mode="edit" database={db} />) },
      {
        label: "Disconnect",
        icon: <IconX />,
        onClick: async () => {
          await api.disconnectDatabase(sid, dbid);
          app.invalidate(keys.db(sid, dbid));
        },
      },
      {
        label: "Unregister",
        icon: <IconTrash />,
        onClick: () =>
          app.showDialog(
            <ConfirmDialog
              title="Unregister database"
              message={`Remove "${db.name}" from the tree? The database file is not deleted.`}
              action="Unregister"
              onConfirm={async () => {
                await api.removeDatabase(sid, dbid);
                app.select({ kind: "server", serverId: sid });
                await app.reloadServers();
                app.invalidate(keys.server(sid));
              }}
            />,
          ),
      },
      {
        label: "Drop database…",
        danger: true,
        icon: <IconTrash />,
        onClick: () =>
          app.showDialog(
            <ConfirmDialog
              title="Drop database"
              message={`This permanently DELETES the database file\n${db.path}\nThis cannot be undone.`}
              action="Drop database"
              confirmText={db.name}
              onConfirm={async () => {
                await api.dropDatabase(sid, dbid);
                app.select({ kind: "server", serverId: sid });
                await app.reloadServers();
                app.invalidate(keys.server(sid));
              }}
            />,
          ),
      },
    ];
  }

  const otype = sel.otype!;
  const ti = typeInfo(otype);

  if (sel.kind === "folder") {
    const items: MenuItem[] = [{ label: "Refresh", icon: <IconRefresh />, onClick: () => app.invalidate(keys.folder(sid, dbid, otype)) }];
    if (otype === "tables") {
      items.push({ label: "Create table…", icon: <IconPlus />, onClick: () => app.showDialog(<CreateTableDialog serverId={sid} dbId={dbid} />) });
    } else if (otype === "users") {
      items.push({ label: "Create user…", icon: <IconPlus />, onClick: () => app.showDialog(<UserDialog serverId={sid} dbId={dbid} />) });
    }
    if (CREATE_TEMPLATES[otype]) {
      items.push({ label: `New ${ti?.singular.toLowerCase()} script`, icon: <IconTerminal />, onClick: () => app.openQueryTool(sid, dbid, CREATE_TEMPLATES[otype]) });
    }
    return items;
  }

  // object or column
  const name = sel.name!;
  const scriptTo = (kind: string, autorun = false) => async () => {
    try {
      const { sql } = await api.script(sid, dbid, otype, name, kind);
      app.openQueryTool(sid, dbid, sql, autorun);
    } catch (e) {
      app.toast(errMsg(e), "error");
    }
  };
  const items: MenuItem[] = [];
  if (otype === "tables" || otype === "views" || otype === "system_tables") {
    items.push(
      { label: "View/Edit data", icon: <IconTable />, onClick: () => app.openTab({ kind: "data", title: name, serverId: sid, dbId: dbid, otype, name }) },
      { label: "View filtered rows…", icon: <IconFilter />, onClick: () => app.openTab({ kind: "data", title: name, serverId: sid, dbId: dbid, otype, name, initialSql: "filter" }) },
      { label: "Count rows", onClick: async () => {
        try {
          const { count } = await api.tableCount(sid, dbid, name);
          app.toast(`${name}: ${count.toLocaleString()} rows`, "info");
        } catch (e) { app.toast(errMsg(e), "error"); }
      } },
      { header: "Scripts" },
      { label: "SELECT script", onClick: scriptTo("select") },
      { label: "INSERT script", onClick: scriptTo("insert") },
      { label: "UPDATE script", onClick: scriptTo("update") },
      { label: "DELETE script", onClick: scriptTo("delete") },
    );
  } else if (otype === "procedures") {
    items.push(
      { label: "Execute…", icon: <IconPlay />, onClick: scriptTo("execute") },
      { label: "SELECT FROM script", onClick: scriptTo("select") },
    );
  } else if (otype === "functions") {
    items.push({ label: "Call script", icon: <IconPlay />, onClick: scriptTo("select") });
  } else if (otype === "users") {
    items.push({ label: "Edit user…", onClick: () => app.showDialog(<UserDialog serverId={sid} dbId={dbid} name={name} />) });
  }
  if (otype === "triggers" || otype === "indices") {
    items.push({
      label: "Activate / deactivate",
      onClick: async () => {
        try {
          const o = await api.object(sid, dbid, otype, name);
          const active = o.properties["Active"];
          const q = `"${name.replace(/"/g, '""')}"`;
          const sql = otype === "triggers" ? `ALTER TRIGGER ${q} ${active ? "INACTIVE" : "ACTIVE"}` : `ALTER INDEX ${q} ${active ? "INACTIVE" : "ACTIVE"}`;
          const r = await api.execute(sid, dbid, sql);
          if (r.error) throw new Error(r.error.message);
          app.toast(`${name} ${active ? "deactivated" : "activated"}`);
          app.invalidate(keys.folder(sid, dbid, otype));
          app.select({ ...sel });
        } catch (e) { app.toast(errMsg(e), "error"); }
      },
    });
  }
  items.push(
    { label: "CREATE script", onClick: scriptTo("create") },
    { label: "Query Tool", icon: <IconTerminal />, onClick: () => app.openQueryTool(sid, dbid) },
    { label: "Refresh", icon: <IconRefresh />, onClick: () => { app.invalidate(keys.folder(sid, dbid, otype)); app.select({ ...sel }); } },
  );
  if (otype !== "system_tables") {
    items.push(
      { separator: true },
      {
        label: `Drop ${ti?.singular.toLowerCase()}…`,
        danger: true,
        icon: <IconTrash />,
        onClick: () =>
          app.showDialog(
            <ConfirmDialog
              title={`Drop ${ti?.singular.toLowerCase()}`}
              message={`Drop ${ti?.singular.toLowerCase()} ${name}? This cannot be undone.`}
              action="Drop"
              onConfirm={async () => {
                await api.dropObject(sid, dbid, otype, name);
                app.toast(`Dropped ${name}`);
                app.select({ kind: "folder", serverId: sid, dbId: dbid, otype });
                app.invalidate(keys.folder(sid, dbid, otype));
              }}
            />,
          ),
      },
    );
  }
  return items;
}
