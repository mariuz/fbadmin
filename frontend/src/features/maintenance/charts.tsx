/** Lightweight inline SVG charts for gstat (no chart library). */
import { useState } from "react";
import { t } from "../../i18n";

export interface BarDatum { label: string; value: number; display?: string; title?: string; flag?: boolean }

/** Horizontal bars, one series: label | bar | value. Sorted by the caller. */
export function BarList(props: { data: BarDatum[]; max?: number; unit?: string; onPick?: (label: string) => void; empty?: string }) {
  const [hover, setHover] = useState<number | null>(null);
  const data = props.data;
  if (!data.length) return <div className="muted">{props.empty ?? t("No data")}</div>;
  const max = props.max ?? Math.max(1, ...data.map((d) => d.value));
  const rowH = 22, labelW = 170, valueW = 92, w = 640;
  const barW = w - labelW - valueW - 12;
  return (
    <svg className="mchart" viewBox={`0 0 ${w} ${data.length * rowH + 4}`} width="100%" style={{ maxWidth: w }}
      role="img" aria-label={t("bar chart")}>
      {data.map((d, i) => {
        const y = i * rowH + 2;
        const bw = Math.max(d.value > 0 ? 3 : 0, (d.value / max) * barW);
        return (
          <g key={d.label} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}
            onClick={() => props.onPick?.(d.label)} style={{ cursor: props.onPick ? "pointer" : "default" }}>
            <title>{d.title ?? `${d.label}: ${d.display ?? d.value}`}</title>
            <rect x={0} y={y - 1} width={w} height={rowH} className={hover === i ? "hit on" : "hit"} />
            <text x={labelW - 8} y={y + rowH / 2 + 3} textAnchor="end" className="lbl">
              {(d.flag ? "⚠ " : "") + (d.label.length > 24 ? d.label.slice(0, 23) + "…" : d.label)}
            </text>
            <rect x={labelW} y={y + 4} width={barW} height={rowH - 10} rx={3} className="track" />
            <rect x={labelW} y={y + 4} width={bw} height={rowH - 10} rx={3} className={d.flag ? "bar flag" : "bar"} />
            <text x={labelW + barW + 8} y={y + rowH / 2 + 3} className="val">{d.display ?? d.value.toLocaleString()}</text>
          </g>
        );
      })}
    </svg>
  );
}

const BUCKETS = ["0–19%", "20–39%", "40–59%", "60–79%", "80–99%"];

/** Five-bucket page fill histogram (sequential shade: emptier pages lighter). */
export function FillHistogram(props: { counts: number[]; width?: number; height?: number; caption?: string }) {
  const w = props.width ?? 150, h = props.height ?? 40;
  const total = props.counts.reduce((a, b) => a + b, 0);
  const max = Math.max(1, ...props.counts);
  const gap = 2, bw = (w - gap * 4) / 5;
  return (
    <svg className="mchart" width={w} height={h + 12} viewBox={`0 0 ${w} ${h + 12}`} role="img"
      aria-label={t("fill distribution {counts}", { counts: props.counts.join(", ") })}>
      {props.counts.map((c, i) => {
        const bh = c ? Math.max(2, (c / max) * h) : 0;
        return (
          <g key={i}>
            <title>{(c === 1 ? t("{range} full: {count} page", { range: BUCKETS[i], count: c }) : t("{range} full: {count} pages", { range: BUCKETS[i], count: c }))
              + (total ? ` (${Math.round((c * 100) / total)}%)` : "")}</title>
            <rect x={i * (bw + gap)} y={0} width={bw} height={h} className="hit" />
            <rect x={i * (bw + gap)} y={h - bh} width={bw} height={bh} rx={2} className={`seq s${i}`} />
          </g>
        );
      })}
      <line x1={0} x2={w} y1={h + 0.5} y2={h + 0.5} className="axis" />
      <text x={0} y={h + 11} className="tick">0%</text>
      <text x={w} y={h + 11} className="tick" textAnchor="end">99%</text>
    </svg>
  );
}

/** Percentage meter (e.g. encryption progress). */
export function Meter(props: { value: number | null; label?: string }) {
  const v = Math.max(0, Math.min(100, props.value ?? 0));
  return (
    <div className="mmeter" title={props.label ?? `${v}%`}>
      <div className="fill" style={{ width: `${v}%` }} />
      <span>{props.label ?? `${v.toFixed(1)}%`}</span>
    </div>
  );
}
