import { useEffect, useRef, useState, type ReactElement } from "react";
import { api, withSchema } from "../api";
import { errMsg, keys, selectionKey, useApp } from "../context";
import { OBJECT_TYPES, type Selection, type Server } from "../types";
import {
  IconChevron, IconChevronDown, IconColumn, IconDatabase, IconFolder, IconKey, IconSchema, IconServer, IconSpinner, TYPE_ICONS,
} from "./Icons";
import { ContextMenu, type MenuItem } from "./ContextMenu";
import { buildMenu } from "./menus";
import { getTreeNodes } from "../registry";
import { t } from "../i18n";

export interface NodeSpec {
  key: string;
  label: string;
  sub?: string;
  count?: number;
  icon: (p: { size?: number }) => ReactElement;
  selection: Selection;
  load?: () => Promise<NodeSpec[]>;
  dim?: boolean;
  title?: string;
}

function TreeNode(props: {
  spec: NodeSpec;
  depth: number;
  onContext: (e: React.MouseEvent, sel: Selection) => void;
}) {
  const app = useApp();
  const { spec } = props;
  const [open, setOpen] = useState(false);
  const [children, setChildren] = useState<NodeSpec[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const version = app.versions[spec.key] ?? 0;
  const firstVersion = useRef(version);
  // keys are schema-agnostic (so refreshing a folder refreshes it in every schema); compare the schema too
  const selected = selectionKey(app.selection) === spec.key.split("@")[0] && app.selection?.schema === spec.selection.schema;

  const load = async () => {
    if (!spec.load) return;
    setLoading(true);
    setError(null);
    try {
      setChildren(await spec.load());
    } catch (e) {
      setError(errMsg(e));
      setChildren(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (version !== firstVersion.current && open) load();
    else if (version !== firstVersion.current) setChildren(null);
    firstVersion.current = version;
  }, [version]);

  const toggle = async () => {
    if (!spec.load) return;
    if (!open && children === null) await load();
    setOpen((o) => !o);
  };

  const Icon = spec.icon;
  return (
    <>
      <div
        className={"tree-row" + (selected ? " selected" : "") + (spec.dim ? " dim" : "")}
        style={{ paddingLeft: 6 + props.depth * 16 }}
        title={spec.title}
        role="treeitem"
        aria-level={props.depth + 1}
        aria-selected={selected}
        aria-expanded={spec.load ? open : undefined}
        tabIndex={selected ? 0 : -1}
        data-level={props.depth}
        data-expandable={spec.load ? "1" : "0"}
        data-open={open ? "1" : "0"}
        onClick={() => app.select(spec.selection)}
        onDoubleClick={toggle}
        onContextMenu={(e) => {
          e.preventDefault();
          app.select(spec.selection);
          props.onContext(e, spec.selection);
        }}
      >
        <span
          className="twisty"
          onClick={(e) => {
            e.stopPropagation();
            toggle();
          }}
        >
          {loading ? <IconSpinner size={12} className="spin" /> : spec.load ? (open ? <IconChevronDown size={12} /> : <IconChevron size={12} />) : null}
        </span>
        <Icon />
        <span className="label">{spec.label}</span>
        {spec.count !== undefined && <span className="count">({spec.count})</span>}
        {spec.sub && <span className="sub">{spec.sub}</span>}
      </div>
      {open && error && (
        <div className="tree-error" style={{ paddingLeft: 26 + props.depth * 16 }}>{error}</div>
      )}
      {open && children && children.length === 0 && !error && (
        <div className="tree-empty" style={{ paddingLeft: 26 + (props.depth + 1) * 16 }}>{t("No items")}</div>
      )}
      {open && children?.map((c) => <TreeNode key={c.key} spec={c} depth={props.depth + 1} onContext={props.onContext} />)}
    </>
  );
}

const RELATIONS = new Set(["tables", "views", "system_tables"]);

function objectNodes(sid: string, dbid: string, otype: string, items: Array<Record<string, any>>, schema?: string): NodeSpec[] {
  return items.map((it) => {
    let sub: string | undefined;
    let dim = false;
    if (otype === "triggers") {
      sub = (it.relation ? it.relation + " · " : "") + it.event;
      dim = !!it.inactive;
    } else if (otype === "indices") {
      sub = it.relation;
      dim = !!it.inactive;
    } else if (otype === "domains") sub = it.data_type;
    else if (otype === "users") dim = !it.active;
    else if (otype === "procedures" || otype === "functions") dim = it.valid === 0;
    return {
      key: keys.object(sid, dbid, otype, it.name),
      label: it.name,
      sub,
      dim,
      title: it.description ?? undefined,
      icon: TYPE_ICONS[otype],
      selection: { kind: "object", serverId: sid, dbId: dbid, otype, name: it.name, schema },
      load: RELATIONS.has(otype)
        ? async () => {
            const o = await withSchema(sid, dbid, schema, () => api.object(sid, dbid, otype, it.name));
            return (o.columns ?? []).map((c: any) => ({
              key: keys.object(sid, dbid, otype, it.name) + ":" + c.name,
              label: c.name,
              sub: c.computed ? t("computed") : c.data_type + (c.not_null ? " not null" : ""),
              icon: c.primary_key ? IconKey : IconColumn,
              selection: { kind: "column", serverId: sid, dbId: dbid, otype, name: it.name, column: c.name, schema },
            }));
          }
        : undefined,
    } satisfies NodeSpec;
  });
}

// server-wide objects, not part of a schema
const GLOBAL_TYPES = new Set(["users", "roles", "shadows", "blob_filters"]);

export function databaseFolders(sid: string, dbid: string, counts: Record<string, number>, schema?: string): NodeSpec[] {
  return OBJECT_TYPES.filter((ot) => !schema || schema === "PUBLIC" || !GLOBAL_TYPES.has(ot.type)).map((ot) => ({
    // "@schema" keeps React keys unique; it is stripped when matching the selection
    key: keys.folder(sid, dbid, ot.type) + (schema ? "@" + schema : ""),
    label: t(ot.label),
    count: counts[ot.type],
    icon: IconFolder,
    selection: { kind: "folder", serverId: sid, dbId: dbid, otype: ot.type, schema },
    load: async () => objectNodes(sid, dbid, ot.type, await withSchema(sid, dbid, schema, () => api.objects(sid, dbid, ot.type)), schema),
  }));
}

/** Firebird 6: one node per SQL schema, each with its own object folders. */
function schemaNodes(sid: string, dbid: string, schemas: Array<{ name: string; system_flag: number; description?: string }>): NodeSpec[] {
  return schemas.map((sc) => ({
    key: keys.db(sid, dbid) + "@" + sc.name,
    label: sc.name,
    sub: sc.system_flag ? t("system") : undefined,
    title: sc.description ?? undefined,
    icon: IconSchema,
    selection: { kind: "database", serverId: sid, dbId: dbid, schema: sc.name },
    load: async () => {
      const info = await withSchema(sid, dbid, sc.name, () => api.databaseInfo(sid, dbid));
      return databaseFolders(sid, dbid, info.counts, sc.name);
    },
  }));
}

export function Tree() {
  const app = useApp();
  const serversRef = useRef<Server[]>(app.servers);
  serversRef.current = app.servers;
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuItem[] } | null>(null);

  const serverSpec = (srv: Server): NodeSpec => ({
    key: keys.server(srv.id),
    label: srv.name,
    sub: `${srv.host}:${srv.port}`,
    icon: IconServer,
    selection: { kind: "server", serverId: srv.id },
    load: async () => {
      await api.connectServer(srv.id);
      const fresh = (await app.reloadServers()).find((s) => s.id === srv.id) ?? srv;
      return fresh.databases.map((d) => ({
        key: keys.db(srv.id, d.id),
        label: d.name,
        title: d.path,
        icon: IconDatabase,
        selection: { kind: "database", serverId: srv.id, dbId: d.id },
        load: async () => {
          await api.connectDatabase(srv.id, d.id);
          const extra = (await Promise.all(getTreeNodes("database").map((p) => p.build(srv.id, d.id).catch(() => [])))).flat();
          const sc = await api.schemas(srv.id, d.id).catch(() => ({ supported: false, schemas: [] }));
          if (sc.supported) return [...schemaNodes(srv.id, d.id, sc.schemas), ...extra];
          const info = await api.databaseInfo(srv.id, d.id);
          return [...databaseFolders(srv.id, d.id, info.counts), ...extra];
        },
      }));
    },
  });

  /** Keyboard navigation (WAI-ARIA tree pattern). */
  const onTreeKey = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const rows = Array.from(e.currentTarget.querySelectorAll<HTMLElement>(".tree-row"));
    const cur = (document.activeElement as HTMLElement)?.closest?.(".tree-row") as HTMLElement | null;
    const i = cur ? rows.indexOf(cur) : -1;
    const focus = (el?: HTMLElement) => {
      if (!el) return;
      el.focus();
      el.click(); // selects it
    };
    const level = cur ? Number(cur.dataset.level) : 0;
    switch (e.key) {
      case "ArrowDown": focus(rows[Math.min(rows.length - 1, i + 1)]); break;
      case "ArrowUp": focus(rows[Math.max(0, i - 1)]); break;
      case "Home": focus(rows[0]); break;
      case "End": focus(rows[rows.length - 1]); break;
      case "ArrowRight":
        if (!cur) return;
        if (cur.dataset.expandable === "1" && cur.dataset.open !== "1") (cur.querySelector(".twisty") as HTMLElement)?.click();
        else if (cur.dataset.open === "1") focus(rows[i + 1]);
        break;
      case "ArrowLeft":
        if (!cur) return;
        if (cur.dataset.open === "1") (cur.querySelector(".twisty") as HTMLElement)?.click();
        else for (let j = i - 1; j >= 0; j--) if (Number(rows[j].dataset.level) < level) { focus(rows[j]); break; }
        break;
      case "Enter": case " ":
        if (cur?.dataset.expandable === "1") (cur.querySelector(".twisty") as HTMLElement)?.click();
        break;
      case "ContextMenu": case "F10":
        if (e.key === "F10" && !e.shiftKey) return;
        if (cur) {
          const r = cur.getBoundingClientRect();
          cur.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, clientX: r.left + 40, clientY: r.bottom }));
        }
        break;
      default: return;
    }
    e.preventDefault();
  };

  const onContext = (e: React.MouseEvent, sel: Selection) => {
    setMenu({ x: e.clientX, y: e.clientY, items: buildMenu(app, sel) });
  };

  if (app.servers.length === 0) {
    return (
      <div className="tree">
        <div className="empty-state" style={{ height: "auto", paddingTop: 40 }}>
          <IconServer size={28} />
          <div>{t("No servers registered yet.")}</div>
        </div>
      </div>
    );
  }
  return (
    <div className="tree" role="tree" aria-label={t("Object Explorer")} onKeyDown={onTreeKey}>
      {app.servers.map((s) => (
        <TreeNode key={s.id} spec={serverSpec(s)} depth={0} onContext={onContext} />
      ))}
      {menu && <ContextMenu {...menu} onClose={() => setMenu(null)} />}
    </div>
  );
}
