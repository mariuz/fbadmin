import { useEffect, useRef, useState, type ReactElement } from "react";
import { api } from "../api";
import { errMsg, keys, selectionKey, useApp } from "../context";
import { OBJECT_TYPES, type Selection, type Server } from "../types";
import {
  IconChevron, IconChevronDown, IconColumn, IconDatabase, IconFolder, IconKey, IconServer, IconSpinner, TYPE_ICONS,
} from "./Icons";
import { ContextMenu, type MenuItem } from "./ContextMenu";
import { buildMenu } from "./menus";

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
  const selected = selectionKey(app.selection) === spec.key;

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
        <div className="tree-empty" style={{ paddingLeft: 26 + (props.depth + 1) * 16 }}>No items</div>
      )}
      {open && children?.map((c) => <TreeNode key={c.key} spec={c} depth={props.depth + 1} onContext={props.onContext} />)}
    </>
  );
}

const RELATIONS = new Set(["tables", "views", "system_tables"]);

function objectNodes(sid: string, dbid: string, otype: string, items: Array<Record<string, any>>): NodeSpec[] {
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
      selection: { kind: "object", serverId: sid, dbId: dbid, otype, name: it.name },
      load: RELATIONS.has(otype)
        ? async () => {
            const o = await api.object(sid, dbid, otype, it.name);
            return (o.columns ?? []).map((c: any) => ({
              key: keys.object(sid, dbid, otype, it.name) + ":" + c.name,
              label: c.name,
              sub: c.computed ? "computed" : c.data_type + (c.not_null ? " not null" : ""),
              icon: c.primary_key ? IconKey : IconColumn,
              selection: { kind: "column", serverId: sid, dbId: dbid, otype, name: it.name, column: c.name },
            }));
          }
        : undefined,
    } satisfies NodeSpec;
  });
}

export function databaseFolders(sid: string, dbid: string, counts: Record<string, number>): NodeSpec[] {
  return OBJECT_TYPES.map((t) => ({
    key: keys.folder(sid, dbid, t.type),
    label: t.label,
    count: counts[t.type],
    icon: IconFolder,
    selection: { kind: "folder", serverId: sid, dbId: dbid, otype: t.type },
    load: async () => objectNodes(sid, dbid, t.type, await api.objects(sid, dbid, t.type)),
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
          const info = await api.databaseInfo(srv.id, d.id);
          return databaseFolders(srv.id, d.id, info.counts);
        },
      }));
    },
  });

  const onContext = (e: React.MouseEvent, sel: Selection) => {
    setMenu({ x: e.clientX, y: e.clientY, items: buildMenu(app, sel) });
  };

  if (app.servers.length === 0) {
    return (
      <div className="tree">
        <div className="empty-state" style={{ height: "auto", paddingTop: 40 }}>
          <IconServer size={28} />
          <div>No servers registered yet.</div>
        </div>
      </div>
    );
  }
  return (
    <div className="tree">
      {app.servers.map((s) => (
        <TreeNode key={s.id} spec={serverSpec(s)} depth={0} onContext={onContext} />
      ))}
      {menu && <ContextMenu {...menu} onClose={() => setMenu(null)} />}
    </div>
  );
}
