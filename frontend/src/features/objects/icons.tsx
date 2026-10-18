const p = (d: string, color: string) => (props: { size?: number }) => (
  <svg width={props.size ?? 15} height={props.size ?? 15} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={1.8}
    strokeLinecap="round" strokeLinejoin="round" style={{ flex: "none" }}>
    <path d={d} />
  </svg>
);

/** "Aa" with a sort arrow */
export const IconCollation = p("M3 18l4-12 4 12M4.5 14h5M14 10h4a2 2 0 0 1 0 4h-4V8h3.5a2 2 0 0 1 0 4M20 15v5M18 18l2 2 2-2", "#0f766e");
/** "Aα" glyph block */
export const IconCharset = p("M4 18l4-12 4 12M5.5 14h5M15 11c3-2 6 0 5 4-1 3-4 3-5 1M20 10v8", "#7c3aed");
/** stacked disks */
export const IconShadow = p("M5 6h11a3 3 0 0 1 3 3v9H8a3 3 0 0 1-3-3zM8 3h11a3 3 0 0 1 3 3v9", "#475569");
/** funnel */
export const IconFilterBlob = p("M3 5h18l-7 8v6l-4 2v-8z", "#b45309");
