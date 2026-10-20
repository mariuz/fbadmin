import { api } from "../api";
import { errMsg, keys, type AppApi } from "../context";
import { typeInfo, type Selection } from "../types";
import type { MenuItem } from "./ContextMenu";
import { applyMenuFilters, contributedMenuItems } from "../registry";
import { t } from "../i18n";
import {
  ConfirmDialog, DatabaseDialog, RestoreDialog, ServerDialog, UserDialog,
} from "./dialogs";
import { DropDialog, canDrop } from "../features/objects/manage";
import {
  IconActivity, IconDatabase, IconFilter, IconInfo, IconLog, IconPlay, IconPlus, IconRefresh,
  IconTable, IconTerminal, IconTrash, IconUpload, IconX,
} from "./Icons";

async function runJob(app: AppApi, p: Promise<unknown>, what: string) {
  try {
    await p;
    app.toast(t("{what} started — see Processes", { what }), "info");
    app.jobStarted();
  } catch (e) {
    app.toast(errMsg(e), "error");
  }
}

export function buildMenu(app: AppApi, sel: Selection): MenuItem[] {
  const base = buildBaseMenu(app, sel);
  const extra = contributedMenuItems(app, sel);
  return applyMenuFilters(app, sel, extra.length ? [...base, { separator: true }, ...extra] : base);
}

function buildBaseMenu(app: AppApi, sel: Selection): MenuItem[] {
  const srv = app.servers.find((s) => s.id === sel.serverId);
  if (!srv) return [];
  const sid = srv.id;

  if (sel.kind === "server") {
    return [
      { label: t("Register database…"), icon: <IconDatabase />, onClick: () => app.showDialog(<DatabaseDialog server={srv} mode="register" />) },
      { label: t("Create database…"), icon: <IconPlus />, onClick: () => app.showDialog(<DatabaseDialog server={srv} mode="create" />) },
      { label: t("Restore backup…"), icon: <IconUpload />, onClick: () => app.showDialog(<RestoreDialog server={srv} />) },
      { separator: true },
      { label: t("Server info"), icon: <IconInfo />, onClick: () => app.openTab({ kind: "serverinfo", title: t("{name} · info", { name: srv.name }), serverId: sid }) },
      { label: t("Server log"), icon: <IconLog />, onClick: () => app.openTab({ kind: "serverlog", title: t("{name} · log", { name: srv.name }), serverId: sid }) },
      { label: t("Refresh"), icon: <IconRefresh />, onClick: () => app.invalidate(keys.server(sid)) },
      { separator: true },
      { label: t("Edit server…"), onClick: () => app.showDialog(<ServerDialog server={srv} />) },
      {
        label: t("Disconnect"),
        icon: <IconX />,
        onClick: async () => {
          await api.disconnectServer(sid);
          await app.reloadServers();
          app.invalidate(keys.server(sid));
        },
      },
      {
        label: t("Remove server"),
        danger: true,
        icon: <IconTrash />,
        onClick: () =>
          app.showDialog(
            <ConfirmDialog
              title={t("Remove server")}
              message={t("Remove the registration of \"{name}\"? Databases on the server are not touched.", { name: srv.name })}
              action={t("Remove")}
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
      { label: t("Query Tool"), icon: <IconTerminal />, onClick: () => app.openQueryTool(sid, dbid) },
      { label: t("Monitor"), icon: <IconActivity />, onClick: () => app.openTab({ kind: "monitor", title: t("{name} · monitor", { name: db.name }), serverId: sid, dbId: dbid }) },
      { label: t("Refresh"), icon: <IconRefresh />, onClick: () => app.invalidate(keys.db(sid, dbid)) },
      // Backup, sweep and gstat live in the maintenance feature (features/maintenance).
      { label: t("Recompute index selectivity"), onClick: () => runJob(app, api.recomputeSelectivity(sid, dbid), t("Index statistics")) },
      { separator: true },
      { label: t("Edit registration…"), onClick: () => app.showDialog(<DatabaseDialog server={srv} mode="edit" database={db} />) },
      {
        label: t("Disconnect"),
        icon: <IconX />,
        onClick: async () => {
          await api.disconnectDatabase(sid, dbid);
          app.invalidate(keys.db(sid, dbid));
        },
      },
      {
        label: t("Unregister"),
        icon: <IconTrash />,
        onClick: () =>
          app.showDialog(
            <ConfirmDialog
              title={t("Unregister database")}
              message={t("Remove \"{name}\" from the tree? The database file is not deleted.", { name: db.name })}
              action={t("Unregister")}
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
        label: t("Drop database…"),
        danger: true,
        icon: <IconTrash />,
        onClick: () =>
          app.showDialog(
            <ConfirmDialog
              title={t("Drop database")}
              message={`${t("This permanently DELETES the database file")}\n${db.path}\n${t("This cannot be undone.")}`}
              action={t("Drop database")}
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
    const items: MenuItem[] = [{ label: t("Refresh"), icon: <IconRefresh />, onClick: () => app.invalidate(keys.folder(sid, dbid, otype)) }];
    if (otype === "users") {
      items.push({ label: t("Create user…"), icon: <IconPlus />, onClick: () => app.showDialog(<UserDialog serverId={sid} dbId={dbid} />) });
    }
    // "Create <object>…" dialogs are contributed by features/objects
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
      { label: t("View/Edit data"), icon: <IconTable />, onClick: () => app.openTab({ kind: "data", title: name, serverId: sid, dbId: dbid, otype, name }) },
      { label: t("View filtered rows…"), icon: <IconFilter />, onClick: () => app.openTab({ kind: "data", title: name, serverId: sid, dbId: dbid, otype, name, initialSql: "filter" }) },
      { label: t("Count rows"), onClick: async () => {
        try {
          const { count } = await api.tableCount(sid, dbid, name);
          app.toast(t("{name}: {count} rows", { name, count: count.toLocaleString() }), "info");
        } catch (e) { app.toast(errMsg(e), "error"); }
      } },
      { header: t("Scripts") },
      { label: t("SELECT script"), onClick: scriptTo("select") },
      { label: t("INSERT script"), onClick: scriptTo("insert") },
      { label: t("UPDATE script"), onClick: scriptTo("update") },
      { label: t("DELETE script"), onClick: scriptTo("delete") },
    );
  } else if (otype === "procedures") {
    items.push(
      { label: t("Execute…"), icon: <IconPlay />, onClick: scriptTo("execute") },
      { label: t("SELECT FROM script"), onClick: scriptTo("select") },
    );
  } else if (otype === "functions") {
    items.push({ label: t("Call script"), icon: <IconPlay />, onClick: scriptTo("select") });
  } else if (otype === "users") {
    items.push({ label: t("Edit user…"), onClick: () => app.showDialog(<UserDialog serverId={sid} dbId={dbid} name={name} />) });
  }
  if (otype === "triggers" || otype === "indices") {
    items.push({
      label: t("Activate / deactivate"),
      onClick: async () => {
        try {
          const o = await api.object(sid, dbid, otype, name);
          const active = o.properties["Active"];
          const q = `"${name.replace(/"/g, '""')}"`;
          const sql = otype === "triggers" ? `ALTER TRIGGER ${q} ${active ? "INACTIVE" : "ACTIVE"}` : `ALTER INDEX ${q} ${active ? "INACTIVE" : "ACTIVE"}`;
          const r = await api.execute(sid, dbid, sql);
          if (r.error) throw new Error(r.error.message);
          app.toast(active ? t("{name} deactivated", { name }) : t("{name} activated", { name }));
          app.invalidate(keys.folder(sid, dbid, otype));
          app.select({ ...sel });
        } catch (e) { app.toast(errMsg(e), "error"); }
      },
    });
  }
  items.push(
    { label: t("CREATE script"), onClick: scriptTo("create") },
    { label: t("Query Tool"), icon: <IconTerminal />, onClick: () => app.openQueryTool(sid, dbid) },
    { label: t("Refresh"), icon: <IconRefresh />, onClick: () => { app.invalidate(keys.folder(sid, dbid, otype)); app.select({ ...sel }); } },
  );
  if (canDrop(otype) && sel.kind === "object") {
    items.push(
      { separator: true },
      {
        label: t("Drop {object}…", { object: (ti ? t(ti.singular) : "").toLowerCase() }),
        danger: true,
        icon: <IconTrash />,
        // shows what depends on the object before dropping it
        onClick: () => app.showDialog(<DropDialog serverId={sid} dbId={dbid} otype={otype} name={name} />),
      },
    );
  }
  return items;
}
