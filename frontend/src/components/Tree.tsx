import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactElement, type ReactNode } from "react";
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
import { useCoarsePointer, useLongPress } from "../features/mobile/responsive";

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

/** Expanded state of rows that a virtual list may unmount (key -> state). */
type NodeStore = Map<string, { open: boolean; children: NodeSpec[] | null }>;

/** Folders with more children than this render only the rows in view (very large schemas). */
export const VIRTUAL_MIN = 400;
const OVERSCAN_PX = 600;

function TreeNode(props: {
  spec: NodeSpec;
  depth: number;
  onContext: (e: { clientX: number; clientY: number }, sel: Selection) => void;
  store?: NodeStore;
  pos?: [number, number]; // aria-posinset, aria-setsize (virtual lists)
}) {
  const app = useApp();
  const { spec } = props;
  const saved = props.store?.get(spec.key);
  const [open, setOpen] = useState(saved?.open ?? false);
  const [children, setChildren] = useState<NodeSpec[] | null>(saved?.children ?? null);
  useEffect(() => {
    if (props.store && (open || children)) props.store.set(spec.key, { open, children });
  }, [open, children]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // touch: long-press opens the context menu; the selected row also gets a "⋮" button
  const [pressing, setPressing] = useState(false);
  const coarse = useCoarsePointer();
  const longPress = useLongPress((x, y) => {
    app.select(spec.selection);
    props.onContext({ clientX: x, clientY: y }, spec.selection);
  }, setPressing);
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
        className={"tree-row" + (selected ? " selected" : "") + (spec.dim ? " dim" : "") + (pressing ? " pressing" : "")}
        style={{ paddingLeft: 6 + props.depth * 16 }}
        title={spec.title}
        role="treeitem"
        aria-level={props.depth + 1}
        aria-selected={selected}
        aria-expanded={spec.load ? open : undefined}
        aria-posinset={props.pos?.[0]}
        aria-setsize={props.pos?.[1]}
        tabIndex={selected ? 0 : -1}
        data-level={props.depth}
        data-expandable={spec.load ? "1" : "0"}
        data-open={open ? "1" : "0"}
        onClick={() => app.select(spec.selection)}
        onDoubleClick={toggle}
        // tables and views can be dragged onto other tools (e.g. the query builder canvas)
        draggable={spec.selection.kind === "object" && RELATIONS.has(spec.selection.otype ?? "") ? true : undefined}
        onDragStart={(e) => e.dataTransfer.setData("application/x-fbadmin-object", JSON.stringify(spec.selection))}
        {...longPress.handlers}
        onContextMenu={(e) => {
          e.preventDefault();
          if (longPress.shouldIgnoreContextMenu()) return;
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
        {coarse && selected && (
          // touch path to the context menu (keyboard users have Shift+F10 / the Menu key)
          <button type="button" className="tree-more" tabIndex={-1} aria-hidden="true" title={t("Actions")}
            onClick={(e) => {
              e.stopPropagation();
              const r = e.currentTarget.getBoundingClientRect();
              props.onContext({ clientX: r.left, clientY: r.bottom }, spec.selection);
            }}>⋮</button>
        )}
      </div>
      {open && error && (
        <div className="tree-error" style={{ paddingLeft: 26 + props.depth * 16 }}>{error}</div>
      )}
      {open && children && children.length === 0 && !error && (
        <div className="tree-empty" style={{ paddingLeft: 26 + (props.depth + 1) * 16 }}>{t("No items")}</div>
      )}
      {open && children && children.length > VIRTUAL_MIN
        ? <VirtualChildren items={children} depth={props.depth + 1} onContext={props.onContext} />
        : open && children?.map((c) => <TreeNode key={c.key} spec={c} depth={props.depth + 1} onContext={props.onContext} />)}
    </>
  );
}

/** A row whose height is reported (rows grow when expanded, and are taller on touch screens). */
function Measured(props: { onHeight: (h: number) => void; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const cb = useRef(props.onHeight);
  cb.current = props.onHeight;
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    cb.current(el.offsetHeight);
    const ro = new ResizeObserver(() => cb.current(el.offsetHeight));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return <div ref={ref} role="none">{props.children}</div>;
}

/**
 * The children of a very large folder (tens of thousands of tables): only the rows in and near the
 * visible part of the tree are rendered; spacers stand in for the others. Heights are measured, so
 * expanded tables (their columns) and touch-sized rows work; expanded state survives scrolling.
 */
function VirtualChildren(props: { items: NodeSpec[]; depth: number; onContext: (e: { clientX: number; clientY: number }, sel: Selection) => void }) {
  const { items } = props;
  const box = useRef<HTMLDivElement>(null);
  const store = useRef<NodeStore>(new Map());
  const heights = useRef(new Map<string, number>());
  const [est, setEst] = useState(24);
  const [version, setVersion] = useState(0);
  const [range, setRange] = useState<[number, number]>([0, 80]);
  const pending = useRef(false);

  const offsets = useMemo(() => {
    const out = new Float64Array(items.length + 1);
    for (let i = 0; i < items.length; i++) out[i + 1] = out[i] + (heights.current.get(items[i].key) ?? est);
    return out;
  }, [items, version, est]);

  const update = () => {
    const el = box.current;
    const sc = el?.closest(".tree") as HTMLElement | null;
    if (!el || !sc) return;
    const top = el.getBoundingClientRect().top - sc.getBoundingClientRect().top; // box top within the viewport
    const from = -top - OVERSCAN_PX;
    const to = -top + sc.clientHeight + OVERSCAN_PX;
    const find = (y: number) => { // first index whose bottom is below y
      let lo = 0, hi = items.length;
      while (lo < hi) { const mid = (lo + hi) >> 1; if (offsets[mid + 1] <= y) lo = mid + 1; else hi = mid; }
      return lo;
    };
    const start = find(from);
    const end = Math.min(items.length, Math.max(start + 1, find(to) + 1));
    setRange((r) => (r[0] === start && r[1] === end ? r : [start, end]));
  };

  useLayoutEffect(update, [offsets]);
  useEffect(() => {
    const sc = box.current?.closest(".tree") as HTMLElement | null;
    if (!sc) return;
    const on = () => update();
    sc.addEventListener("scroll", on, { passive: true });
    const ro = new ResizeObserver(on);
    ro.observe(sc);
    return () => { sc.removeEventListener("scroll", on); ro.disconnect(); };
  }, [offsets]);

  const onHeight = (key: string, h: number) => {
    if (!h || heights.current.get(key) === h) return;
    if (!heights.current.size) setEst(h); // the first row tells the usual row height
    heights.current.set(key, h);
    if (!pending.current) {
      pending.current = true;
      requestAnimationFrame(() => { pending.current = false; setVersion((v) => v + 1); });
    }
  };

  const [start, end] = range;
  return (
    <div ref={box} role="none" className="tree-virtual">
      <div role="none" aria-hidden="true" style={{ height: offsets[start] }} />
      {items.slice(start, end).map((c, i) => (
        <Measured key={c.key} onHeight={(h) => onHeight(c.key, h)}>
          <TreeNode spec={c} depth={props.depth} onContext={props.onContext} store={store.current}
            pos={[start + i + 1, items.length]} />
        </Measured>
      ))}
      <div role="none" aria-hidden="true" style={{ height: offsets[items.length] - offsets[end] }} />
    </div>
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
    sub: srv.embedded ? t("embedded engine") : `${srv.host}:${srv.port}`,
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
      case "Home": case "End": {
        // virtual lists render only rows in view: scroll to the end first, then focus the row there
        const tree = e.currentTarget;
        tree.scrollTop = e.key === "Home" ? 0 : tree.scrollHeight;
        requestAnimationFrame(() => requestAnimationFrame(() => {
          const now = Array.from(tree.querySelectorAll<HTMLElement>(".tree-row"));
          focus(e.key === "Home" ? now[0] : now[now.length - 1]);
        }));
        break;
      }
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

  const onContext = (e: { clientX: number; clientY: number }, sel: Selection) => {
    setMenu({ x: e.clientX, y: e.clientY, items: buildMenu(app, sel) });
  };

  const emptyHint = (
    <div className="empty-state" style={{ height: "auto", paddingTop: 40 }}>
      <IconServer size={28} />
      <div>{t("No servers registered yet.")}</div>
    </div>
  );
  if (app.servers.length === 0) return <div className="tree">{emptyHint}</div>;
  return (
    <>
      {/* with nothing selected no row is a tab stop: the tree is one and hands focus to its first row */}
      <div className="tree" role="tree" aria-label={t("Object Explorer")} onKeyDown={onTreeKey}
        tabIndex={app.selection ? undefined : 0}
        onFocus={(e) => {
          if (e.target === e.currentTarget) e.currentTarget.querySelector<HTMLElement>(".tree-row")?.focus();
        }}>
        {app.servers.map((s) => (
          <TreeNode key={s.id} spec={serverSpec(s)} depth={0} onContext={onContext} />
        ))}
        {menu && <ContextMenu {...menu} onClose={() => setMenu(null)} />}
      </div>
      {/* only local (embedded) databases so far: still invite to register a server */}
      {app.servers.every((s) => s.embedded) && <div className="tree-hint">{emptyHint}</div>}
    </>
  );
}
