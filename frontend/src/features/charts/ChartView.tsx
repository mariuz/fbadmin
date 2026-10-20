/**
 * "Chart" view of a Query Tool result: bar, horizontal bar, stacked bar, line, area, pie, donut and
 * scatter charts drawn as inline SVG (no chart library), from the rows the grid has fetched.
 * Legend, marker shapes, tooltips, keyboard focus and a data table make it readable without colour.
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { ResultViewContext } from "../../registry";
import { N_, t } from "../../i18n";
import { useApp } from "../../context";
import { IconCopy, IconDownload, IconSpinner } from "../../components/Icons";
import {
  AGG_FNS, CHART_TYPES, MAX_SERIES, defaultConfig, formatCompact, formatFull, formatTime, niceTicks,
  numericColumns, shapeData, shapedTsv, truncate, valueRange, type AggFn, type ChartConfig, type ChartType, type Shaped,
  type SortMode,
} from "./shape";

const TYPE_LABELS: Record<ChartType, string> = {
  bar: N_("Bar"), hbar: N_("Horizontal bar"), stacked: N_("Stacked bar"), line: N_("Line"), area: N_("Area"),
  pie: N_("Pie"), donut: N_("Donut"), scatter: N_("Scatter"),
};
const AGG_LABELS: Record<AggFn, string> = {
  sum: N_("Sum"), avg: N_("Average"), count: N_("Count rows"), min: N_("Minimum"), max: N_("Maximum"), first: N_("First value"),
};
const SORT_LABELS: Record<SortMode, string> = {
  auto: N_("As fetched (dates and numbers in order)"), label: N_("By category"), "value-desc": N_("Largest first"), "value-asc": N_("Smallest first"),
};
/** Rows fetched at most by "Load all rows". */
export const LOAD_ALL_CAP = 50000;
const CHAR_W = 6.6;
const FONT = "system-ui, -apple-system, Segoe UI, Roboto, sans-serif";

const color = (i: number) => `var(--qc-${(i % MAX_SERIES) + 1})`;
const SHAPES = ["circle", "square", "triangle", "diamond", "triangle-down", "cross", "star", "ring"] as const;

function Marker(props: { shape: number; x: number; y: number; r: number; fill: string }) {
  const { x, y, r, fill } = props;
  const s = SHAPES[props.shape % SHAPES.length];
  const common = { fill, stroke: "var(--panel)", strokeWidth: 1.5 };
  switch (s) {
    case "square": return <rect x={x - r} y={y - r} width={2 * r} height={2 * r} {...common} />;
    case "triangle": return <path d={`M${x},${y - r * 1.2}L${x + r * 1.1},${y + r * 0.8}L${x - r * 1.1},${y + r * 0.8}Z`} {...common} />;
    case "triangle-down": return <path d={`M${x},${y + r * 1.2}L${x + r * 1.1},${y - r * 0.8}L${x - r * 1.1},${y - r * 0.8}Z`} {...common} />;
    case "diamond": return <path d={`M${x},${y - r * 1.3}L${x + r * 1.3},${y}L${x},${y + r * 1.3}L${x - r * 1.3},${y}Z`} {...common} />;
    case "cross": return <path d={`M${x - r},${y - r}L${x + r},${y + r}M${x + r},${y - r}L${x - r},${y + r}`} stroke={fill} strokeWidth={2.5} />;
    case "star": return <path d={`M${x},${y - r * 1.3}L${x + r * 0.4},${y - r * 0.4}L${x + r * 1.3},${y - r * 0.3}L${x + r * 0.6},${y + r * 0.4}L${x + r * 0.8},${y + r * 1.3}L${x},${y + r * 0.8}L${x - r * 0.8},${y + r * 1.3}L${x - r * 0.6},${y + r * 0.4}L${x - r * 1.3},${y - r * 0.3}L${x - r * 0.4},${y - r * 0.4}Z`} {...common} />;
    case "ring": return <circle cx={x} cy={y} r={r} fill="var(--panel)" stroke={fill} strokeWidth={2.5} />;
    default: return <circle cx={x} cy={y} r={r} {...common} />;
  }
}

/** Remembered configuration per result shape (column names and types), so re-running keeps the chart. */
const remembered = new Map<string, ChartConfig>();

function resolveVars(svg: string): string {
  const cs = getComputedStyle(document.documentElement);
  return svg.replace(/var\((--[\w-]+)\)/g, (_, v) => (cs.getPropertyValue(v).trim() || "#888").replace(/"/g, "'"));
}
function saveBlob(name: string, blob: Blob) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

interface Tip { x: number; y: number; title: string; rows: Array<{ label: string; value: string; idx: number }> }
interface Layout {
  body: ReactNode;
  /** index of the item under (px, py), or null */
  hit: (px: number, py: number) => number | null;
  /** number of focusable items (categories, points or slices) */
  count: number;
  tip: (i: number) => Tip | null;
}
interface LegendItem { label: string; idx: number; extra?: string }

function legendLayout(items: LegendItem[], width: number, shapes: boolean) {
  const out: Array<{ x: number; y: number; item: LegendItem; text: string }> = [];
  let x = 0, y = 0;
  for (const item of items) {
    const text = truncate(item.label, 40) + (item.extra ? `  ${item.extra}` : "");
    const w = 20 + text.length * 7.2 + 18;
    if (x > 0 && x + w > width) { x = 0; y += 18; }
    out.push({ x, y, item, text });
    x += w;
  }
  const node = (ox: number, oy: number) => (
    <g transform={`translate(${ox},${oy})`} aria-hidden="true">
      {out.map((o, k) => (
        <g key={k} transform={`translate(${o.x},${o.y})`}>
          {shapes ? <Marker shape={o.item.idx} x={6} y={6} r={4.5} fill={color(o.item.idx)} />
            : <rect x={0} y={1} width={11} height={11} rx={2} fill={color(o.item.idx)} />}
          <text x={18} y={10.5} fontSize={12} fill="var(--text)">{o.text}</text>
        </g>
      ))}
    </g>
  );
  return { height: out.length ? y + 20 : 0, node };
}

// ------------------------------------------------------------------ cartesian charts
function cartesian(sh: Shaped, cfg: ChartConfig, xName: string, W: number, H: number, active: number | null): Layout {
  const type = cfg.type;
  const horizontal = type === "hbar";
  const scatter = type === "scatter";
  const lineLike = type === "line" || type === "area";
  const nSeries = sh.series.length;
  const legend = legendLayout(nSeries > 1 ? sh.series.map((s, i) => ({ label: s.label, idx: i })) : [], W - 10, lineLike || scatter);
  const top = 8 + (legend.height ? legend.height + 6 : 0);
  const [lo, hi] = valueRange(sh, type);
  const vTicks = niceTicks(lo, hi, Math.max(2, Math.floor((horizontal ? W : H) / (horizontal ? 110 : 55))));
  const vMin = vTicks[0], vMax = vTicks[vTicks.length - 1];
  const tickText = (v: number) => formatCompact(v);
  const cats = sh.categories;
  const n = scatter ? sh.points.length : cats.length;
  const continuous = sh.axis !== "category";
  const range = (() => {
    if (!continuous) return [0, 1];
    const xs = scatter ? sh.points.map((p) => p.x) : cats.map((c) => (sh.axis === "time" ? c.t! : c.n!));
    return xs.length ? [Math.min(...xs), Math.max(...xs)] : [0, 1];
  })();
  const xNumTicks = sh.axis === "number" ? niceTicks(range[0], range[1], Math.max(2, Math.floor(W / 110))) : [];
  const xMin = sh.axis === "number" ? xNumTicks[0] : range[0];
  const xMax = sh.axis === "number" ? xNumTicks[xNumTicks.length - 1] : range[1] === range[0] ? range[0] + 1 : range[1];

  // margins
  const catLabelMax = horizontal ? 26 : 18;
  const longestCat = Math.min(catLabelMax, Math.max(1, ...cats.map((c) => c.label.length)));
  const valueLabelW = Math.max(...vTicks.map((v) => tickText(v).length)) * CHAR_W;
  let left = horizontal ? Math.min(200, longestCat * CHAR_W + 12) : valueLabelW + 14;
  const right = 14;
  let plotW = Math.max(40, W - left - right);
  const band = !continuous && n ? plotW / n : 0;
  const rotate = !horizontal && !continuous && longestCat * CHAR_W + 6 > band;
  const bottom = horizontal ? 26 : rotate ? Math.min(130, longestCat * CHAR_W * 0.75 + 24) : 40;
  if (horizontal) left = Math.max(left, 40);
  plotW = Math.max(40, W - left - right);
  const plotH = Math.max(40, H - top - bottom);

  // scales
  const v2p = (v: number) => horizontal
    ? left + ((v - vMin) / (vMax - vMin)) * plotW
    : top + plotH - ((v - vMin) / (vMax - vMin)) * plotH;
  const bandSize = !continuous && n ? (horizontal ? plotH : plotW) / n : 0;
  const catCenter = (i: number) => (horizontal ? top : left) + bandSize * (i + 0.5);
  const xCont = (x: number) => left + ((x - xMin) / (xMax - xMin || 1)) * plotW;
  const xOf = (i: number) => (continuous ? xCont(sh.axis === "time" ? cats[i].t! : cats[i].n!) : catCenter(i));
  const base = v2p(Math.min(Math.max(0, vMin), vMax));

  const parts: ReactNode[] = [];
  // grid + value axis
  vTicks.forEach((v, k) => {
    const p = v2p(v);
    parts.push(horizontal
      ? <g key={`vt${k}`}><line x1={p} x2={p} y1={top} y2={top + plotH} stroke="var(--qc-grid)" /><text x={p} y={top + plotH + 16} fontSize={11} textAnchor="middle" fill="var(--muted)">{tickText(v)}</text></g>
      : <g key={`vt${k}`}><line x1={left} x2={left + plotW} y1={p} y2={p} stroke="var(--qc-grid)" /><text x={left - 6} y={p + 4} fontSize={11} textAnchor="end" fill="var(--muted)">{tickText(v)}</text></g>);
  });
  // category / x axis
  if (continuous) {
    const nt = Math.max(2, Math.min(8, Math.floor(plotW / 110)));
    const ticks: number[] = sh.axis === "number" ? xNumTicks
      : Array.from({ length: nt }, (_, k) => xMin + ((xMax - xMin) * k) / (nt - 1));
    ticks.forEach((x, k) => {
      const p = xCont(x);
      parts.push(<text key={`xt${k}`} x={p} y={top + plotH + 16} fontSize={11} textAnchor={k === 0 ? "start" : k === ticks.length - 1 ? "end" : "middle"} fill="var(--muted)">
        {sh.axis === "time" ? formatTime(x, xMax - xMin) : formatCompact(x)}</text>);
    });
  } else {
    const step = Math.max(1, Math.ceil(n / Math.max(1, Math.floor((horizontal ? plotH : plotW) / (horizontal ? 15 : rotate ? 15 : longestCat * CHAR_W + 8)))));
    cats.forEach((c, i) => {
      if (i % step && i !== active) return;
      const label = truncate(c.label, catLabelMax);
      const p = catCenter(i);
      const style = c.isNull || c.other ? { fontStyle: "italic" as const } : undefined;
      parts.push(horizontal
        ? <text key={`ct${i}`} x={left - 6} y={p + 4} fontSize={11} textAnchor="end" fill="var(--text)" style={style}>{label}</text>
        : rotate
          ? <text key={`ct${i}`} transform={`translate(${p + 3},${top + plotH + 10}) rotate(-40)`} fontSize={11} textAnchor="end" fill="var(--text)" style={style}>{label}</text>
          : <text key={`ct${i}`} x={p} y={top + plotH + 16} fontSize={11} textAnchor="middle" fill="var(--text)" style={style}>{label}</text>);
    });
  }
  // baseline
  parts.push(horizontal
    ? <line key="base" x1={base} x2={base} y1={top} y2={top + plotH} stroke="var(--qc-axis)" />
    : <line key="base" x1={left} x2={left + plotW} y1={base} y2={base} stroke="var(--qc-axis)" />);
  if (active !== null && !scatter) {
    const p = continuous ? xOf(active) : catCenter(active);
    parts.push(horizontal
      ? <rect key="hl" x={left} y={p - bandSize / 2} width={plotW} height={bandSize} fill="var(--hover)" opacity={0.7} />
      : continuous || lineLike
        ? <line key="hl" x1={p} x2={p} y1={top} y2={top + plotH} stroke="var(--muted)" strokeDasharray="3 3" />
        : <rect key="hl" x={p - bandSize / 2} y={top} width={bandSize} height={plotH} fill="var(--hover)" opacity={0.7} />);
  }

  // marks
  if (type === "bar" || type === "hbar") {
    const group = bandSize * 0.8;
    const w = Math.max(1, group / Math.max(1, nSeries));
    sh.series.forEach((s, si) => {
      s.values.forEach((v, i) => {
        if (v === null) return;
        const off = catCenter(i) - group / 2 + si * w;
        const a = v2p(v);
        const gap = w > 6 ? 1 : 0;
        parts.push(horizontal
          ? <rect key={`b${si}-${i}`} y={off + gap} height={Math.max(1, w - 2 * gap)} x={Math.min(a, base)} width={Math.max(1, Math.abs(a - base))} rx={w > 8 ? 2 : 0} fill={color(si)} />
          : <rect key={`b${si}-${i}`} x={off + gap} width={Math.max(1, w - 2 * gap)} y={Math.min(a, base)} height={Math.max(1, Math.abs(a - base))} rx={w > 8 ? 2 : 0} fill={color(si)} />);
      });
    });
  } else if (type === "stacked") {
    const w = bandSize * 0.7;
    cats.forEach((_, i) => {
      let pos = 0, neg = 0;
      sh.series.forEach((s, si) => {
        const v = s.values[i];
        if (v === null || v === 0) return;
        const from = v >= 0 ? pos : neg;
        const to = from + v;
        if (v >= 0) pos = to; else neg = to;
        const a = v2p(from), b = v2p(to);
        parts.push(<rect key={`s${si}-${i}`} x={catCenter(i) - w / 2} width={Math.max(1, w)} y={Math.min(a, b)} height={Math.max(1, Math.abs(a - b) - 1)} fill={color(si)} stroke="var(--panel)" strokeWidth={1} />);
      });
    });
  } else if (lineLike) {
    const showMarkers = n <= 60;
    sh.series.forEach((s, si) => {
      let d = "", pen = false, area = "", seg: Array<[number, number]> = [];
      const flush = () => {
        if (seg.length && type === "area") area += `M${seg[0][0]},${base}` + seg.map(([x, y]) => `L${x},${y}`).join("") + `L${seg[seg.length - 1][0]},${base}Z`;
        seg = [];
      };
      s.values.forEach((v, i) => {
        if (v === null) { pen = false; flush(); return; }
        const x = xOf(i), y = v2p(v);
        d += `${pen ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
        seg.push([+x.toFixed(1), +y.toFixed(1)]);
        pen = true;
      });
      flush();
      if (area) parts.push(<path key={`a${si}`} d={area} fill={color(si)} fillOpacity={0.18} stroke="none" />);
      parts.push(<path key={`l${si}`} d={d} fill="none" stroke={color(si)} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />);
      s.values.forEach((v, i) => {
        if (v === null || (!showMarkers && i !== active)) return;
        parts.push(<Marker key={`m${si}-${i}`} shape={si} x={xOf(i)} y={v2p(v)} r={i === active ? 5 : 3.5} fill={color(si)} />);
      });
    });
  } else if (scatter) {
    sh.points.forEach((p, i) => {
      parts.push(<Marker key={`p${i}`} shape={p.series} x={xCont(p.x)} y={v2p(p.y)} r={i === active ? 6 : 4} fill={color(p.series)} />);
    });
  }
  parts.push(legend.node(left, 6));
  // axis title for the category axis
  parts.push(<text key="xname" x={horizontal ? 4 : left + plotW} y={horizontal ? top - 2 + (legend.height ? 0 : 4) : H - 4} fontSize={11} textAnchor={horizontal ? "start" : "end"} fill="var(--muted)">{truncate(xName, 40)}</text>);

  const hit = (px: number, py: number): number | null => {
    if (!n) return null;
    if (scatter) {
      let best: number | null = null, bd = 16 * 16;
      sh.points.forEach((p, i) => {
        const dx = xCont(p.x) - px, dy = v2p(p.y) - py;
        if (dx * dx + dy * dy < bd) { bd = dx * dx + dy * dy; best = i; }
      });
      return best;
    }
    if (continuous) {
      let best = 0, bd = Infinity;
      cats.forEach((_, i) => { const d = Math.abs(xOf(i) - px); if (d < bd) { bd = d; best = i; } });
      return best;
    }
    const pos = horizontal ? py - top : px - left;
    const i = Math.floor(pos / bandSize);
    return i >= 0 && i < n ? i : null;
  };
  const tip = (i: number): Tip | null => {
    if (scatter) {
      const p = sh.points[i];
      if (!p) return null;
      return { x: xCont(p.x), y: v2p(p.y), title: p.label, rows: [{ label: sh.series[p.series]?.label ?? "", value: formatFull(p.y), idx: p.series }] };
    }
    const c = cats[i];
    if (!c) return null;
    const vals = sh.series.map((s, si) => ({ label: s.label, value: formatFull(s.values[i]), idx: si }));
    if (type === "stacked" && sh.series.length > 1) {
      const total = sh.series.reduce((a, s) => a + (s.values[i] ?? 0), 0);
      vals.push({ label: t("Total"), value: formatFull(total), idx: -1 });
    }
    const anchor = horizontal ? { x: left + plotW / 2, y: catCenter(i) } : { x: continuous ? xOf(i) : catCenter(i), y: top + 10 };
    return { ...anchor, title: c.label, rows: vals };
  };
  return { body: <>{parts}</>, hit, count: n, tip };
}

// ------------------------------------------------------------------ pie / donut
function pie(sh: Shaped, cfg: ChartConfig, W: number, H: number, active: number | null): Layout {
  const s = sh.series[0];
  const vals = s ? s.values.map((v) => (v !== null && v > 0 ? v : 0)) : [];
  const total = vals.reduce((a, b) => a + b, 0);
  const pct = (v: number) => (total ? `${((v / total) * 100).toFixed(1)}%` : "");
  const items = sh.categories.map((c, i) => ({ label: c.label, idx: i, extra: `${formatCompact(vals[i])} · ${pct(vals[i])}` }));
  const side = W >= 520;
  const legendW = side ? Math.min(320, W * 0.42) : W - 10;
  const legend = legendLayout(items, legendW, false);
  const r = Math.max(30, Math.min((side ? W - legendW - 30 : W - 20) / 2, (side ? H : H - legend.height - 16) / 2 - 10));
  const cx = side ? r + 16 : W / 2, cy = side ? H / 2 : r + 10;
  const inner = cfg.type === "donut" ? r * 0.58 : 0;
  const parts: ReactNode[] = [];
  let a0 = -Math.PI / 2;
  const angles: Array<[number, number]> = [];
  vals.forEach((v, i) => {
    const a1 = a0 + (total ? (v / total) * 2 * Math.PI : 0);
    angles.push([a0, a1]);
    const big = a1 - a0 > Math.PI ? 1 : 0;
    const rr = i === active ? r + 5 : r;
    const p = (a: number, rad: number) => `${(cx + rad * Math.cos(a)).toFixed(2)},${(cy + rad * Math.sin(a)).toFixed(2)}`;
    let d: string;
    if (a1 - a0 >= 2 * Math.PI - 1e-6) {
      d = `M${p(a0, rr)}A${rr},${rr} 0 1 1 ${p(a0 + Math.PI, rr)}A${rr},${rr} 0 1 1 ${p(a0, rr)}Z` +
        (inner ? `M${p(a0, inner)}A${inner},${inner} 0 1 0 ${p(a0 + Math.PI, inner)}A${inner},${inner} 0 1 0 ${p(a0, inner)}Z` : "");
    } else {
      d = inner
        ? `M${p(a0, rr)}A${rr},${rr} 0 ${big} 1 ${p(a1, rr)}L${p(a1, inner)}A${inner},${inner} 0 ${big} 0 ${p(a0, inner)}Z`
        : `M${cx},${cy}L${p(a0, rr)}A${rr},${rr} 0 ${big} 1 ${p(a1, rr)}Z`;
    }
    if (v > 0) parts.push(<path key={`sl${i}`} d={d} fill={color(i)} stroke="var(--panel)" strokeWidth={2} fillRule="evenodd" />);
    a0 = a1;
  });
  if (inner && s) {
    parts.push(<text key="tot" x={cx} y={cy + 5} textAnchor="middle" fontSize={15} fontWeight={600} fill="var(--text)">{formatCompact(total)}</text>);
  }
  parts.push(side ? legend.node(cx + r + 24, Math.max(8, cy - legend.height / 2)) : legend.node(8, cy + r + 14));
  const hit = (px: number, py: number) => {
    const dx = px - cx, dy = py - cy, d = Math.sqrt(dx * dx + dy * dy);
    if (d > r + 6 || d < inner) return null;
    let a = Math.atan2(dy, dx);
    if (a < -Math.PI / 2) a += 2 * Math.PI;
    const i = angles.findIndex(([x0, x1]) => a >= x0 && a < x1);
    return i >= 0 ? i : null;
  };
  const tip = (i: number): Tip | null => {
    const c = sh.categories[i];
    if (!c || !s) return null;
    const mid = (angles[i][0] + angles[i][1]) / 2;
    return {
      x: cx + (r * 0.7) * Math.cos(mid), y: cy + (r * 0.7) * Math.sin(mid), title: c.label,
      rows: [{ label: s.label, value: `${formatFull(s.values[i])} (${pct(vals[i])})`, idx: i }],
    };
  };
  return { body: <>{parts}</>, hit, count: sh.categories.length, tip };
}

// ------------------------------------------------------------------ component
export function ChartView({ ctx }: { ctx: ResultViewContext }) {
  const app = useApp();
  const { columns, rows } = ctx;
  const signature = columns.map((c) => `${c.name}:${c.type}`).join("|");
  const [cfg, setCfgState] = useState<ChartConfig>(() => {
    const r = remembered.get(signature);
    return r && r.x < columns.length ? r : defaultConfig(columns, rows);
  });
  const setCfg = (p: Partial<ChartConfig>) => setCfgState((c) => {
    const next = { ...c, ...p };
    remembered.set(signature, next);
    return next;
  });
  const [showTable, setShowTable] = useState(false);
  const [active, setActive] = useState<number | null>(null);
  const [tipPos, setTipPos] = useState<{ x: number; y: number } | null>(null);
  const [loadingAll, setLoadingAll] = useState(false);
  const plotRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const [size, setSize] = useState({ w: 600, h: 320 });
  useEffect(() => {
    const el = plotRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: Math.max(240, el.clientWidth), h: Math.max(200, el.clientHeight) }));
    ro.observe(el);
    return () => ro.disconnect();
  }, [showTable]);

  const numeric = useMemo(() => numericColumns(columns, rows), [columns, rows]);
  const shaped = useMemo(
    () => shapeData(columns, rows, cfg, { nullLabel: t("(null)"), otherLabel: t("Other") }),
    [columns, rows, cfg],
  );
  useEffect(() => { setActive(null); }, [shaped]);
  const xName = columns[cfg.x]?.name ?? "";
  const isPie = cfg.type === "pie" || cfg.type === "donut";
  const layout = useMemo(
    () => (isPie ? pie(shaped, cfg, size.w, size.h, active) : cartesian(shaped, cfg, xName, size.w, size.h, active)),
    [shaped, cfg, size, active, xName, isPie],
  );
  const tip = active !== null ? layout.tip(active) : null;
  const empty = isPie ? !shaped.categories.length : cfg.type === "scatter" ? !shaped.points.length : !shaped.categories.length;
  const charted = shaped.stats.rows;

  const typeName = t(TYPE_LABELS[cfg.type]);
  const yNames = cfg.group !== null ? columns[cfg.ys[0]]?.name ?? "" : cfg.ys.map((i) => columns[i]?.name).join(", ");
  const summary = t("{type} chart of {values} by {category}", { type: typeName, values: yNames, category: xName });

  const loadAll = async () => {
    setLoadingAll(true);
    try {
      let n = rows.length, more = ctx.hasMore;
      while (more && n < LOAD_ALL_CAP) {
        const r = await ctx.loadMore(Math.min(10000, LOAD_ALL_CAP - n));
        if (!r.fetched) break;
        n += r.fetched;
        more = r.hasMore;
      }
    } finally {
      setLoadingAll(false);
    }
  };

  const svgText = () => {
    const el = svgRef.current;
    if (!el) return "";
    const clone = el.cloneNode(true) as SVGSVGElement;
    clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    clone.removeAttribute("tabindex");
    clone.removeAttribute("class");
    const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
    title.textContent = summary;
    clone.insertBefore(title, clone.firstChild);
    return resolveVars(new XMLSerializer().serializeToString(clone));
  };
  const fileBase = `chart-${cfg.type}`;
  const exportSvg = () => saveBlob(`${fileBase}.svg`, new Blob([svgText()], { type: "image/svg+xml" }));
  const exportPng = () => {
    const url = URL.createObjectURL(new Blob([svgText()], { type: "image/svg+xml" }));
    const img = new Image();
    img.onload = () => {
      const scale = 2;
      const c = document.createElement("canvas");
      c.width = size.w * scale;
      c.height = size.h * scale;
      const g = c.getContext("2d")!;
      g.scale(scale, scale);
      g.drawImage(img, 0, 0, size.w, size.h);
      URL.revokeObjectURL(url);
      c.toBlob((b) => b && saveBlob(`${fileBase}.png`, b), "image/png");
    };
    img.onerror = () => { URL.revokeObjectURL(url); app.toast(t("Could not render the PNG"), "error"); };
    img.src = url;
  };
  const copyTsv = async () => {
    try {
      await navigator.clipboard.writeText(shapedTsv(shaped, xName));
      app.toast(t("Chart data copied as TSV"));
    } catch {
      app.toast(t("The browser did not allow copying to the clipboard"), "error");
    }
  };

  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - r.left, py = e.clientY - r.top;
    const i = layout.hit(px, py);
    setActive(i);
    setTipPos(i === null ? null : { x: px, y: py });
  };
  const onKey = (e: React.KeyboardEvent) => {
    const n = layout.count;
    if (!n) return;
    const next = (d: number) => setActive((a) => (a === null ? (d > 0 ? 0 : n - 1) : Math.min(n - 1, Math.max(0, a + d))));
    if (e.key === "ArrowRight" || e.key === "ArrowDown") { next(1); setTipPos(null); e.preventDefault(); }
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") { next(-1); setTipPos(null); e.preventDefault(); }
    else if (e.key === "Home") { setActive(0); setTipPos(null); e.preventDefault(); }
    else if (e.key === "End") { setActive(n - 1); setTipPos(null); e.preventDefault(); }
    else if (e.key === "Escape") setActive(null);
  };
  const tipAt = tip ? (tipPos ?? { x: tip.x, y: tip.y }) : null;

  const xCandidates = columns.map((_, i) => i).filter((i) => !/BLOB/i.test(columns[i].type));
  const notes: string[] = [];
  const st = shaped.stats;
  if (st.nullX && (shaped.axis !== "category" || cfg.type === "scatter")) notes.push(t("{count} rows without a {column} value are not shown", { count: st.nullX, column: xName }));
  if (st.nonNumeric) notes.push(t("{count} values that are not numbers are ignored", { count: st.nonNumeric }));
  if (st.collapsedCategories) notes.push(t("{count} smaller categories are combined into “Other”", { count: st.collapsedCategories }));
  if (st.collapsedSeries) notes.push(t("{count} series are combined into “Other”", { count: st.collapsedSeries }));
  if (st.nonPositive) notes.push(t("{count} categories with zero or negative values are left out of the pie", { count: st.nonPositive }));

  return (
    <div className="qchart">
      <div className="qchart-controls">
        <label>{t("Chart")}
          <select className="text" value={cfg.type} onChange={(e) => {
            const type = e.target.value as ChartType;
            const pieLike = (k: ChartType) => k === "pie" || k === "donut";
            setCfg({ type, topN: pieLike(type) ? 8 : pieLike(cfg.type) ? (type === "bar" || type === "hbar" ? 30 : null) : cfg.topN });
          }}>
            {CHART_TYPES.map((k) => <option key={k} value={k}>{t(TYPE_LABELS[k])}</option>)}
          </select>
        </label>
        <label>{cfg.type === "scatter" ? t("X axis") : t("Category")}
          <select className="text" value={cfg.x} onChange={(e) => setCfg({ x: Number(e.target.value) })}>
            {xCandidates.map((i) => <option key={i} value={i}>{columns[i].name}</option>)}
          </select>
        </label>
        <details className="ys">
          <summary className="btn small">{yNames ? t("Values: {columns}", { columns: yNames }) : t("Values: none")}</summary>
          <fieldset className="qchart-ys-pop">
            <legend className="sr-only">{t("Value columns")}</legend>
            {numeric.length === 0 && <span className="muted">{t("The result has no numeric columns.")}</span>}
            {numeric.map((i) => {
              const on = cfg.ys.includes(i);
              return (
                <label key={i} className="check">
                  <input type="checkbox" checked={on} disabled={!on && cfg.ys.length >= MAX_SERIES}
                    onChange={() => setCfg({ ys: on ? cfg.ys.filter((y) => y !== i) : [...cfg.ys, i].sort((a, b) => a - b) })} />
                  {" "}{columns[i].name}
                </label>
              );
            })}
          </fieldset>
        </details>
        {!isPie && (
          <label title={t("Make one series per value of this column (uses the first value column)")}>{t("Series from")}
            <select className="text" value={cfg.group ?? -1} onChange={(e) => setCfg({ group: Number(e.target.value) < 0 ? null : Number(e.target.value) })}>
              <option value={-1}>{t("value columns")}</option>
              {xCandidates.filter((i) => i !== cfg.x && !numeric.includes(i)).map((i) => <option key={i} value={i}>{columns[i].name}</option>)}
            </select>
          </label>
        )}
        {cfg.type !== "scatter" && (
          <label title={t("How to combine values when a category appears in several rows")}>{t("Combine")}
            <select className="text" value={cfg.agg} onChange={(e) => setCfg({ agg: e.target.value as AggFn })}>
              {AGG_FNS.map((k) => <option key={k} value={k}>{t(AGG_LABELS[k])}</option>)}
            </select>
          </label>
        )}
        {cfg.type !== "scatter" && shaped.axis === "category" && (
          <>
            <label>{t("Sort")}
              <select className="text" value={cfg.sort} onChange={(e) => setCfg({ sort: e.target.value as SortMode })}>
                {(Object.keys(SORT_LABELS) as SortMode[]).map((k) => <option key={k} value={k}>{t(SORT_LABELS[k])}</option>)}
              </select>
            </label>
            <label title={t("Show the largest categories and combine the rest into “Other” (empty: all)")}>{t("Top")}
              <input className="text" type="number" min={2} max={isPie ? MAX_SERIES : 500} style={{ width: 64 }} value={cfg.topN ?? ""} placeholder={t("all")}
                onChange={(e) => setCfg({ topN: e.target.value ? Math.max(2, Number(e.target.value)) : null })} />
            </label>
          </>
        )}
        <span className="grow" style={{ flex: 1 }} />
        <button className={"btn small" + (showTable ? " on" : "")} aria-pressed={showTable} onClick={() => setShowTable(!showTable)}>{t("Data table")}</button>
        <button className="btn small" onClick={exportSvg} disabled={empty} title={t("Download the chart as an SVG image")}><IconDownload size={13} /> SVG</button>
        <button className="btn small" onClick={exportPng} disabled={empty} title={t("Download the chart as a PNG image")}><IconDownload size={13} /> PNG</button>
        <button className="btn small" onClick={copyTsv} disabled={empty} title={t("Copy the charted data as TSV (paste into a spreadsheet)")}><IconCopy size={13} /> TSV</button>
      </div>
      <div className="qchart-info" aria-live="polite">
        <span>{ctx.hasMore
          ? t("Charting the {count} rows fetched so far; the query has more rows.", { count: charted.toLocaleString() })
          : t("Charting all {count} rows.", { count: charted.toLocaleString() })}</span>
        {ctx.hasMore && (
          <button className="btn small" disabled={loadingAll || ctx.loadingMore} onClick={loadAll}>
            {loadingAll ? <IconSpinner size={13} className="spin" /> : null} {t("Load all rows (up to {count})", { count: LOAD_ALL_CAP.toLocaleString() })}
          </button>
        )}
        {notes.map((n, i) => <span key={i}>· {n}</span>)}
      </div>
      <div className="qchart-body">
        <div className="qchart-plot" ref={plotRef}>
          {empty ? (
            <div className="qchart-empty">
              {!cfg.ys.length ? t("Pick at least one value column.")
                : cfg.type === "scatter" ? t("Scatter charts need a numeric or date column on the X axis and numeric values.")
                : t("Nothing to chart: no rows with numeric values.")}
            </div>
          ) : (
            <svg ref={svgRef} className="qchart-svg" width={size.w} height={size.h} viewBox={`0 0 ${size.w} ${size.h}`}
              role="img" aria-label={summary} tabIndex={0} fontFamily={FONT}
              onPointerMove={onMove} onPointerLeave={() => { setActive(null); setTipPos(null); }} onKeyDown={onKey}
              onBlur={() => setActive(null)}>
              <rect x={0} y={0} width={size.w} height={size.h} fill="var(--panel)" />
              {layout.body}
            </svg>
          )}
          {tip && tipAt && (
            <div className="qchart-tip" style={{ left: Math.min(Math.max(0, tipAt.x + 14), Math.max(0, size.w - 240)), top: Math.max(0, Math.min(tipAt.y + 10, size.h - 40 - 20 * tip.rows.length)) }}>
              <div className="h">{tip.title}</div>
              {tip.rows.map((r, i) => (
                <div className="r" key={i}>
                  {r.idx >= 0 && <svg width={10} height={10} aria-hidden="true"><rect width={10} height={10} rx={2} fill={color(r.idx)} /></svg>}
                  <span className="l">{r.label}</span><b>{r.value}</b>
                </div>
              ))}
            </div>
          )}
          <div className="sr-only" aria-live="polite">{tip ? `${tip.title}: ${tip.rows.map((r) => `${r.label} ${r.value}`).join(", ")}` : ""}</div>
        </div>
        {showTable && !empty && <DataTable shaped={shaped} xName={xName} />}
      </div>
    </div>
  );
}

function DataTable({ shaped, xName }: { shaped: Shaped; xName: string }) {
  const MAX = 1000;
  if (shaped.points.length) {
    return (
      <div className="qchart-table" tabIndex={0} role="region" aria-label={t("Chart data")}>
        <table className="grid">
          <thead><tr><th scope="col">{xName}</th><th scope="col">{t("Value")}</th><th scope="col">{t("Series")}</th></tr></thead>
          <tbody>
            {shaped.points.slice(0, MAX).map((p, i) => (
              <tr key={i}><td>{p.label}</td><td className="num">{formatFull(p.y)}</td><td>{shaped.series[p.series]?.label}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  return (
    <div className="qchart-table" tabIndex={0} role="region" aria-label={t("Chart data")}>
      <table className="grid">
        <thead><tr><th scope="col">{xName}</th>{shaped.series.map((s) => <th scope="col" key={s.key}>{s.label}</th>)}</tr></thead>
        <tbody>
          {shaped.categories.slice(0, MAX).map((c, i) => (
            <tr key={c.key}>
              <th scope="row" style={{ fontWeight: 400, textAlign: "left" }}>{c.label}</th>
              {shaped.series.map((s) => <td key={s.key} className="num">{formatFull(s.values[i])}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

