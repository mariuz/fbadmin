/**
 * Charts from query results: turn result rows into chart data (categories x series), with
 * aggregation of repeated categories, grouping into series, sorting, top-N with "Other",
 * NULL handling and time axes. Pure module (no imports) so `node --test` can test it
 * (frontend/tests/charts.test.ts).
 */

export type ChartType = "bar" | "hbar" | "stacked" | "line" | "area" | "pie" | "donut" | "scatter";
export type AggFn = "sum" | "avg" | "count" | "min" | "max" | "first";
export type SortMode = "auto" | "label" | "value-desc" | "value-asc";
export type AxisKind = "category" | "time" | "number";

export const CHART_TYPES: ChartType[] = ["bar", "hbar", "stacked", "line", "area", "pie", "donut", "scatter"];
export const AGG_FNS: AggFn[] = ["sum", "avg", "count", "min", "max", "first"];
/** Categorical colour slots (charts.css --qc-1..8); more series fold into "Other". */
export const MAX_SERIES = 8;
/** Categories drawn at most on a category axis; the rest fold into "Other". */
export const MAX_CATEGORIES = 200;

export interface ColumnLike { name: string; type: string }
export interface ChartConfig {
  type: ChartType;
  x: number;
  ys: number[];
  /** column whose values become the series (uses the first Y column) */
  group: number | null;
  agg: AggFn;
  sort: SortMode;
  /** keep the N largest categories, fold the rest into "Other" (null = all) */
  topN: number | null;
}
export interface Category { key: string; label: string; t?: number; n?: number; other?: boolean; isNull?: boolean }
export interface Series { key: string; label: string; values: (number | null)[]; other?: boolean }
export interface Point { x: number; y: number; series: number; label: string }
export interface ShapeStats {
  rows: number;
  /** rows whose X was NULL (charted as the "(null)" category, or skipped for scatter / time axes) */
  nullX: number;
  /** Y cells that were neither NULL nor numbers */
  nonNumeric: number;
  collapsedCategories: number;
  collapsedSeries: number;
  /** pie / donut: categories left out because their value is zero or negative */
  nonPositive: number;
}
export interface Shaped {
  axis: AxisKind;
  categories: Category[];
  series: Series[];
  points: Point[];
  stats: ShapeStats;
}
export interface Labels { nullLabel: string; otherLabel: string }

const NUMERIC_TYPE = /INT|NUMERIC|DECIMAL|DOUBLE|FLOAT|DECFLOAT|REAL|SMALLINT|BIGINT/i;
const DATE_TYPE = /DATE|TIMESTAMP/i;

export function isNumericType(type: string) { return NUMERIC_TYPE.test(type) && !/INTERVAL/i.test(type); }
export function isDateType(type: string) { return DATE_TYPE.test(type); }

/** A cell as a number, or null (NULL, empty, or not a number). */
export function toNumber(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "bigint") return Number(v);
  if (typeof v === "string" && /^\s*[-+]?(\d+\.?\d*|\.\d+)(e[-+]?\d+)?\s*$/i.test(v)) return parseFloat(v);
  return null;
}

/** A DATE / TIMESTAMP cell (ISO text from the backend) as epoch milliseconds, or null. */
export function toTime(v: unknown): number | null {
  if (typeof v !== "string") return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?/.exec(v);
  if (!m) return null;
  const ms = m[7] ? Math.round(Number("0." + m[7]) * 1000) : 0;
  return new Date(+m[1], +m[2] - 1, +m[3], +(m[4] ?? 0), +(m[5] ?? 0), +(m[6] ?? 0), ms).getTime();
}

function cellLabel(v: unknown): string {
  if (typeof v === "boolean") return v ? "true" : "false";
  return String(v).replace("T", " ");
}

/** Columns that can be Y series: numeric types, or text columns whose fetched values are all numbers. */
export function numericColumns(cols: ColumnLike[], rows: unknown[][]): number[] {
  return cols.map((_, i) => i).filter((i) => {
    if (isNumericType(cols[i].type)) return true;
    if (/BLOB|BOOLEAN|DATE|TIME/i.test(cols[i].type)) return false;
    let seen = 0;
    for (const r of rows.slice(0, 500)) {
      const v = r[i];
      if (v === null || v === undefined) continue;
      if (toNumber(v) === null) return false;
      seen++;
    }
    return seen > 0;
  });
}

/** A sensible starting configuration: first text/date column as X, first numeric columns as Y. */
export function defaultConfig(cols: ColumnLike[], rows: unknown[][]): ChartConfig {
  const nums = numericColumns(cols, rows);
  const nonNum = cols.map((_, i) => i).filter((i) => !nums.includes(i) && !/BLOB/i.test(cols[i].type));
  const dateCol = nonNum.find((i) => isDateType(cols[i].type));
  const x = nonNum[0] ?? 0;
  const ys = nums.filter((i) => i !== x).slice(0, 3);
  const type: ChartType = dateCol !== undefined && dateCol === x ? "line" : "bar";
  return { type, x, ys: ys.length ? ys : nums.slice(0, 1), group: null, agg: "sum", sort: "auto", topN: type === "bar" ? 30 : null };
}

interface Acc { sum: number; n: number; min: number; max: number; first: number | null; rows: number }
const newAcc = (): Acc => ({ sum: 0, n: 0, min: Infinity, max: -Infinity, first: null, rows: 0 });
function addAcc(a: Acc, v: number | null) {
  a.rows++;
  if (v === null) return;
  a.sum += v; a.n++;
  if (v < a.min) a.min = v;
  if (v > a.max) a.max = v;
  if (a.first === null) a.first = v;
}
function mergeAcc(a: Acc, b: Acc) {
  a.rows += b.rows; a.sum += b.sum; a.n += b.n;
  a.min = Math.min(a.min, b.min); a.max = Math.max(a.max, b.max);
  if (a.first === null) a.first = b.first;
}
function accValue(a: Acc | undefined, fn: AggFn): number | null {
  if (!a) return null;
  if (fn === "count") return a.rows;
  if (!a.n) return null;
  switch (fn) {
    case "sum": return a.sum;
    case "avg": return a.sum / a.n;
    case "min": return a.min;
    case "max": return a.max;
    default: return a.first;
  }
}

const NULL_KEY = "\u0000null";
const OTHER_KEY = "\u0000other";

/** Shape result rows for a chart. */
export function shapeData(cols: ColumnLike[], rows: unknown[][], cfg: ChartConfig, labels: Labels): Shaped {
  const stats: ShapeStats = { rows: rows.length, nullX: 0, nonNumeric: 0, collapsedCategories: 0, collapsedSeries: 0, nonPositive: 0 };
  const xCol = cols[cfg.x];
  const ys = cfg.ys.filter((i) => i >= 0 && i < cols.length);
  const empty: Shaped = { axis: "category", categories: [], series: [], points: [], stats };
  if (!xCol || !ys.length) return empty;
  const xDate = isDateType(xCol.type);
  const xNum = !xDate && isNumericType(xCol.type);
  const continuous = cfg.type === "line" || cfg.type === "area" || cfg.type === "scatter";
  const axis: AxisKind = continuous && xDate ? "time" : continuous && xNum ? "number" : "category";
  const grouped = cfg.group !== null && cfg.group !== undefined && cfg.group >= 0 && cfg.group < cols.length;
  const seriesOf = (r: unknown[]): Array<[string, string, unknown]> =>
    grouped
      ? [[r[cfg.group!] === null || r[cfg.group!] === undefined ? NULL_KEY : "g" + cellLabel(r[cfg.group!]),
        r[cfg.group!] === null || r[cfg.group!] === undefined ? labels.nullLabel : cellLabel(r[cfg.group!]), r[ys[0]]]]
      : ys.map((i) => ["c" + i, cols[i].name, r[i]] as [string, string, unknown]);
  const countNonNumeric = (v: unknown) => { if (v !== null && v !== undefined && v !== "" && toNumber(v) === null) stats.nonNumeric++; };

  // ---------------------------------------------------------------- scatter: one point per row
  if (cfg.type === "scatter") {
    const sIndex = new Map<string, number>();
    const series: Series[] = [];
    const points: Point[] = [];
    for (const r of rows) {
      const xv = xDate ? toTime(r[cfg.x]) : toNumber(r[cfg.x]);
      if (xv === null) { stats.nullX++; continue; }
      for (const [key, label, raw] of seriesOf(r)) {
        countNonNumeric(raw);
        const y = toNumber(raw);
        if (y === null) continue;
        let si = sIndex.get(key);
        if (si === undefined) { si = series.length; sIndex.set(key, si); series.push({ key, label, values: [] }); }
        points.push({ x: xv, y, series: si, label: cellLabel(r[cfg.x]) });
      }
    }
    const kept = foldSeriesByCount(series, points, labels.otherLabel, stats);
    return { axis: xDate ? "time" : "number", categories: [], series: kept.series, points: kept.points, stats };
  }

  // ---------------------------------------------------------------- categories x series
  const cats = new Map<string, Category>();
  const acc = new Map<string, Map<string, Acc>>(); // series key -> category key -> acc
  const seriesLabels = new Map<string, string>();
  for (const r of rows) {
    const xv = r[cfg.x];
    let key: string, cat: Category;
    if (xv === null || xv === undefined) {
      stats.nullX++;
      if (axis !== "category") continue; // no place for NULL on a time / number axis
      key = NULL_KEY;
      cat = { key, label: labels.nullLabel, isNull: true };
    } else {
      key = "v" + cellLabel(xv);
      cat = { key, label: cellLabel(xv) };
      if (xDate) cat.t = toTime(xv) ?? undefined;
      if (xNum) cat.n = toNumber(xv) ?? undefined;
      if (axis === "time" && cat.t === undefined) { stats.nullX++; continue; }
      if (axis === "number" && cat.n === undefined) { stats.nullX++; continue; }
    }
    if (!cats.has(key)) cats.set(key, cat);
    for (const [sk, sl, raw] of seriesOf(r)) {
      countNonNumeric(raw);
      if (!seriesLabels.has(sk)) seriesLabels.set(sk, sl);
      let m = acc.get(sk);
      if (!m) acc.set(sk, (m = new Map()));
      let a = m.get(key);
      if (!a) m.set(key, (a = newAcc()));
      addAcc(a, toNumber(raw));
    }
  }

  let seriesKeys = [...seriesLabels.keys()];
  // more series than colour slots: keep the largest, fold the rest into "Other"
  if (seriesKeys.length > MAX_SERIES) {
    const total = (sk: string) => { let s = 0; for (const a of acc.get(sk)!.values()) s += Math.abs(cfg.agg === "count" ? a.rows : a.sum); return s; };
    const ranked = [...seriesKeys].sort((a, b) => total(b) - total(a));
    const keep = new Set(ranked.slice(0, MAX_SERIES - 1));
    const other = new Map<string, Acc>();
    for (const sk of seriesKeys) {
      if (keep.has(sk)) continue;
      stats.collapsedSeries++;
      for (const [ck, a] of acc.get(sk)!) { let o = other.get(ck); if (!o) other.set(ck, (o = newAcc())); mergeAcc(o, a); }
    }
    seriesKeys = seriesKeys.filter((k) => keep.has(k));
    seriesKeys.push(OTHER_KEY);
    acc.set(OTHER_KEY, other);
    seriesLabels.set(OTHER_KEY, labels.otherLabel);
  }
  const pie = cfg.type === "pie" || cfg.type === "donut";
  if (pie) seriesKeys = seriesKeys.slice(0, 1);

  let catList = [...cats.values()];
  const catTotal = (c: Category) => {
    let s = 0;
    for (const sk of seriesKeys) { const v = accValue(acc.get(sk)?.get(c.key), cfg.agg); if (v !== null) s += v; }
    return s;
  };
  if (pie) {
    const before = catList.length;
    catList = catList.filter((c) => catTotal(c) > 0);
    stats.nonPositive = before - catList.length;
  }

  // ordering
  const byLabel = (a: Category, b: Category) =>
    a.isNull ? 1 : b.isNull ? -1
      : a.t !== undefined && b.t !== undefined ? a.t - b.t
      : a.n !== undefined && b.n !== undefined ? a.n - b.n
      : a.label.localeCompare(b.label, undefined, { numeric: true });
  let sort = cfg.sort;
  if (sort === "auto") sort = axis !== "category" || xDate || xNum ? "label" : pie ? "value-desc" : "auto";
  if (axis !== "category") sort = "label"; // a time / number axis is always in order
  if (sort === "label") catList.sort(byLabel);
  else if (sort === "value-desc") catList.sort((a, b) => catTotal(b) - catTotal(a));
  else if (sort === "value-asc") catList.sort((a, b) => catTotal(a) - catTotal(b));

  // top N (and a hard cap for category axes): keep the largest, fold the rest into "Other"
  let limit = cfg.topN && cfg.topN > 0 ? cfg.topN : null;
  if (pie) limit = Math.min(limit ?? MAX_SERIES, MAX_SERIES);
  if (axis === "category" && catList.length > MAX_CATEGORIES) limit = Math.min(limit ?? MAX_CATEGORIES, MAX_CATEGORIES);
  if (limit && catList.length > limit && axis === "category") {
    const ranked = [...catList].sort((a, b) => Math.abs(catTotal(b)) - Math.abs(catTotal(a)));
    const keep = new Set(ranked.slice(0, limit - 1).map((c) => c.key));
    const dropped = catList.filter((c) => !keep.has(c.key));
    stats.collapsedCategories = dropped.length;
    for (const sk of seriesKeys) {
      const m = acc.get(sk);
      if (!m) continue;
      const o = newAcc();
      for (const c of dropped) { const a = m.get(c.key); if (a) mergeAcc(o, a); }
      m.set(OTHER_KEY, o);
    }
    catList = catList.filter((c) => keep.has(c.key));
    catList.push({ key: OTHER_KEY, label: labels.otherLabel, other: true });
  }

  const series: Series[] = seriesKeys.map((sk) => ({
    key: sk, label: seriesLabels.get(sk)!, other: sk === OTHER_KEY || undefined,
    values: catList.map((c) => accValue(acc.get(sk)?.get(c.key), cfg.agg)),
  }));
  return { axis, categories: catList, series, points: [], stats };
}

function foldSeriesByCount(series: Series[], points: Point[], otherLabel: string, stats: ShapeStats) {
  if (series.length <= MAX_SERIES) return { series, points };
  const counts = series.map((_, i) => points.filter((p) => p.series === i).length);
  const ranked = series.map((_, i) => i).sort((a, b) => counts[b] - counts[a]);
  const keep = ranked.slice(0, MAX_SERIES - 1).sort((a, b) => a - b);
  const remap = new Map(keep.map((old, i) => [old, i]));
  stats.collapsedSeries = series.length - keep.length;
  const other = keep.length;
  return {
    series: [...keep.map((i) => series[i]), { key: OTHER_KEY, label: otherLabel, values: [], other: true }],
    points: points.map((p) => ({ ...p, series: remap.get(p.series) ?? other })),
  };
}

// ------------------------------------------------------------------ axes and formatting
/** About ``count`` round tick values covering [min, max] (always including 0 when in range). */
export function niceTicks(min: number, max: number, count = 5): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [0, 1];
  if (min === max) {
    if (min === 0) return [0, 1];
    const pad = Math.abs(min) * 0.1;
    min -= pad; max += pad;
  }
  const raw = (max - min) / Math.max(1, count);
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * p).find((s) => s >= raw) ?? 10 * p;
  const lo = Math.floor(min / step + 1e-9) * step, hi = Math.ceil(max / step - 1e-9) * step;
  const out: number[] = [];
  for (let v = lo; v <= hi + step / 2; v += step) out.push(Math.abs(v) < step / 1e6 ? 0 : +v.toPrecision(12));
  return out;
}

/** Value range of the drawn marks (stacked sums for stacked bars and areas; 0 included for bars). */
export function valueRange(sh: Shaped, type: ChartType): [number, number] {
  let lo = Infinity, hi = -Infinity;
  if (type === "scatter") {
    for (const p of sh.points) { lo = Math.min(lo, p.y); hi = Math.max(hi, p.y); }
  } else if (type === "stacked") {
    sh.categories.forEach((_, i) => {
      let pos = 0, neg = 0;
      for (const s of sh.series) { const v = s.values[i]; if (v === null) continue; if (v >= 0) pos += v; else neg += v; }
      lo = Math.min(lo, neg); hi = Math.max(hi, pos);
    });
  } else {
    for (const s of sh.series) for (const v of s.values) if (v !== null) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  }
  if (lo === Infinity) return [0, 1];
  if (type !== "line" && type !== "scatter") { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
  return [lo, hi];
}

/** Short axis label: 1.2K, 3.4M… (locale aware). */
export function formatCompact(v: number, locale?: string): string {
  if (Math.abs(v) < 1000) return (+v.toPrecision(6)).toLocaleString(locale, { maximumFractionDigits: 6 });
  return new Intl.NumberFormat(locale, { notation: "compact", maximumFractionDigits: 1 }).format(v);
}
export function formatFull(v: number | null, locale?: string): string {
  return v === null ? "–" : v.toLocaleString(locale, { maximumFractionDigits: 6 });
}
/** Time axis label, as detailed as the visible range needs. */
export function formatTime(t: number, range: number, locale?: string): string {
  const d = new Date(t);
  const DAY = 86400000;
  if (range > 3 * 365 * DAY) return String(d.getFullYear());
  if (range > 90 * DAY) return d.toLocaleDateString(locale, { year: "numeric", month: "short" });
  if (range > 2 * DAY) return d.toLocaleDateString(locale, { month: "short", day: "numeric" });
  return d.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" });
}
/** Shorten a label to ``max`` characters with an ellipsis. */
export function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, Math.max(1, max - 1)) + "…" : s;
}

/** The charted data as TSV (paste into a spreadsheet). */
export function shapedTsv(sh: Shaped, xName: string): string {
  const esc = (v: string) => v.replace(/[\t\n\r]/g, " ");
  if (sh.points.length) {
    return ["x\ty\tseries", ...sh.points.map((p) => `${esc(p.label)}\t${p.y}\t${esc(sh.series[p.series]?.label ?? "")}`)].join("\n") + "\n";
  }
  const head = [xName, ...sh.series.map((s) => s.label)].map(esc).join("\t");
  return [head, ...sh.categories.map((c, i) => [esc(c.label), ...sh.series.map((s) => (s.values[i] === null ? "" : String(s.values[i])))].join("\t"))].join("\n") + "\n";
}
