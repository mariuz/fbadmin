/**
 * Lightweight inline-SVG line chart (no chart library): one y axis, hairline grid, 2px lines,
 * crosshair + tooltip listing every series, legend with line keys for 2+ series, keyboard focus
 * (arrow keys move the crosshair) and a table view twin.
 */
import { useEffect, useMemo, useRef, useState } from "react";

export interface Series {
  key: string;
  label: string;
  /** categorical slot 1..3 - follows the entity, never the rank */
  slot: 1 | 2 | 3;
  values: (number | null)[];
}

export interface LineChartProps {
  title: string;
  unit?: string;
  times: number[]; // unix seconds
  series: Series[];
  format?: (v: number | null) => string;
  height?: number;
  loading?: boolean;
  /** show dates on the x axis (history over days) */
  longRange?: boolean;
  /** values are bytes: round the axis maximum in powers of 1024 */
  binary?: boolean;
}

const PAD = { l: 44, r: 10, t: 8, b: 20 };

function niceMax(v: number, binary = false): number {
  if (v <= 0) return 1;
  if (binary && v >= 1024) {
    const unit = Math.pow(1024, Math.floor(Math.log(v) / Math.log(1024)));
    return niceMax(v / unit) * unit;
  }
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

function fmtTime(ts: number, long: boolean): string {
  const d = new Date(ts * 1000);
  const hm = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: long ? undefined : "2-digit" });
  return long ? `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${hm}` : hm;
}

const color = (slot: number) => `var(--series-${slot})`;

export function LineChart(props: LineChartProps) {
  const { times, series } = props;
  const fmt = props.format ?? ((v: number | null) => (v === null ? "–" : v.toLocaleString(undefined, { maximumFractionDigits: 1 })));
  const height = props.height ?? 150;
  const wrap = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(400);
  const [hover, setHover] = useState<number | null>(null);
  const [table, setTable] = useState(false);

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(Math.max(200, el.clientWidth)));
    ro.observe(el);
    setWidth(Math.max(200, el.clientWidth));
    return () => ro.disconnect();
  }, [table]);

  const n = times.length;
  const max = useMemo(() => {
    let m = 0;
    for (const s of series) for (const v of s.values) if (v !== null && v > m) m = v;
    return niceMax(m, props.binary);
  }, [series, props.binary]);
  const t0 = n ? times[0] : 0;
  const t1 = n ? times[n - 1] : 1;
  const iw = width - PAD.l - PAD.r;
  const ih = height - PAD.t - PAD.b;
  const x = (t: number) => PAD.l + (t1 === t0 ? iw : ((t - t0) / (t1 - t0)) * iw);
  const y = (v: number) => PAD.t + ih - (v / max) * ih;

  const paths = series.map((s) => {
    let d = "";
    let pen = false;
    s.values.forEach((v, i) => {
      if (v === null || v === undefined) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(times[i]).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  });

  const last = (s: Series) => {
    for (let i = s.values.length - 1; i >= 0; i--) if (s.values[i] !== null) return s.values[i];
    return null;
  };

  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    if (!n) return;
    const r = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - r.left;
    let best = 0;
    let bd = Infinity;
    for (let i = 0; i < n; i++) {
      const d = Math.abs(x(times[i]) - px);
      if (d < bd) { bd = d; best = i; }
    }
    setHover(best);
  };
  const onKey = (e: React.KeyboardEvent) => {
    if (!n) return;
    if (e.key === "ArrowLeft") { setHover((h) => Math.max(0, (h ?? n) - 1)); e.preventDefault(); }
    if (e.key === "ArrowRight") { setHover((h) => Math.min(n - 1, (h ?? -1) + 1)); e.preventDefault(); }
    if (e.key === "Escape") setHover(null);
  };

  const ticks = [0, max / 2, max];
  const xTicks = n > 1 ? [t0, t1] : n ? [t0] : [];
  const headline = series.length === 1 ? last(series[0]) : null;
  const hx = hover !== null && n ? x(times[hover]) : 0;

  return (
    <div className="chart-card">
      <div className="chart-head">
        <span className="chart-title">{props.title}</span>
        {props.unit && <span className="chart-unit">{props.unit}</span>}
        {series.length === 1 && <span className="chart-now">{fmt(headline)}</span>}
      </div>
      {series.length > 1 && (
        <div className="chart-legend">
          {series.map((s) => (
            <span key={s.key} className="item">
              <span className="key-line" style={{ background: color(s.slot) }} />
              {s.label} <b>{fmt(last(s))}</b>
            </span>
          ))}
        </div>
      )}
      {table ? (
        <div className="chart-table">
          <table className="grid">
            <thead><tr><th>Time</th>{series.map((s) => <th key={s.key}>{s.label}</th>)}</tr></thead>
            <tbody>
              {times.map((t, i) => ({ t, i })).reverse().slice(0, 200).map(({ t, i }) => (
                <tr key={i}><td>{fmtTime(t, !!props.longRange)}</td>{series.map((s) => <td key={s.key} className="num">{fmt(s.values[i])}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div ref={wrap} className={"chart-wrap" + (props.loading ? " loading" : "")}>
          {n === 0 ? (
            <div className="chart-empty" style={{ height }}>Waiting for data…</div>
          ) : (
            <svg
              className="chart-svg"
              width={width}
              height={height}
              role="img"
              aria-label={`${props.title} line chart`}
              tabIndex={0}
              onPointerMove={onMove}
              onPointerLeave={() => setHover(null)}
              onKeyDown={onKey}
              onBlur={() => setHover(null)}
            >
              {ticks.map((t, i) => (
                <g key={i}>
                  <line className={i === 0 ? "axis" : "grid"} x1={PAD.l} x2={width - PAD.r} y1={Math.round(y(t)) + 0.5} y2={Math.round(y(t)) + 0.5} />
                  <text className="tick" x={PAD.l - 6} y={y(t) + 3.5} textAnchor="end">{fmt(t)}</text>
                </g>
              ))}
              {xTicks.map((t, i) => (
                <text key={i} className="tick" x={x(t)} y={height - 5} textAnchor={i === 0 ? "start" : "end"}>{fmtTime(t, !!props.longRange)}</text>
              ))}
              {paths.map((d, i) => <path key={series[i].key} className="line" d={d} stroke={color(series[i].slot)} />)}
              {hover !== null && (
                <g>
                  <line className="cross" x1={Math.round(hx) + 0.5} x2={Math.round(hx) + 0.5} y1={PAD.t} y2={PAD.t + ih} />
                  {series.map((s) => s.values[hover] !== null && s.values[hover] !== undefined ? (
                    <circle key={s.key} className="dot" cx={hx} cy={y(s.values[hover]!)} r={4} fill={color(s.slot)} />
                  ) : null)}
                </g>
              )}
            </svg>
          )}
          {hover !== null && n > 0 && (
            <div className="chart-tip" style={hx > width / 2 ? { right: width - hx + 10 } : { left: hx + 10 }}>
              <div className="t">{fmtTime(times[hover], !!props.longRange)}</div>
              {series.map((s) => (
                <div className="row" key={s.key}>
                  <span className="key-line" style={{ background: color(s.slot) }} />
                  <b>{fmt(s.values[hover])}</b>
                  <span className="l">{s.label}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      <div className="chart-foot">
        <button onClick={() => setTable((v) => !v)} aria-pressed={table}>{table ? "Chart" : "Table"}</button>
      </div>
    </div>
  );
}
