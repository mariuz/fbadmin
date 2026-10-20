/**
 * ERD export: a standalone SVG (colours and fonts written into the file, so it looks the same outside
 * fbadmin) or a PNG rendered from that SVG at 1× or 2×, downloaded or copied to the clipboard.
 */
import { useEffect, useMemo, useState } from "react";
import { errMsg, useApp } from "../../context";
import { Modal } from "../../components/Modal";
import { IconCopy, IconDownload } from "../../components/Icons";
import { saveBlob } from "./api";
import { t } from "../../i18n";

export interface ErdExportOptions {
  scope: "all" | "view";
  theme: "light" | "current";
  background: "solid" | "transparent";
  title: boolean;
}
export interface Box { x: number; y: number; w: number; h: number }

const SVG_NS = "http://www.w3.org/2000/svg";
/** Fonts found on most systems; the app's own --mono stack starts with names only browsers know. */
export const EXPORT_FONT = "'DejaVu Sans Mono', Menlo, Consolas, 'Liberation Mono', 'Courier New', monospace";
/** The light theme's colours (styles.css :root), used whatever theme the app shows. */
const LIGHT: Record<string, string> = {
  "--bg": "#f4f5f7", "--panel": "#ffffff", "--border": "#c9ced5", "--text": "#1f2328", "--muted": "#646d78",
  "--accent": "#c2410c", "--accent-soft": "#fff1e8", "--ok": "#2e7d32", "--warn": "#b26a00", "--new": "#e6f6e8",
  "--danger": "#c62828",
};
const MARGIN = 20;
const TITLE_H = 46;
/** Browsers refuse (or silently blank) bigger canvases; Safari's limit is the lowest. */
export const MAX_SIDE = 16384;
export const MAX_PIXELS = 64_000_000;

function palette(theme: ErdExportOptions["theme"]): (name: string) => string {
  if (theme === "light") return (v) => (v === "--mono" ? EXPORT_FONT : LIGHT[v] ?? "#888888");
  const cs = getComputedStyle(document.documentElement);
  return (v) => (v === "--mono" ? EXPORT_FONT : cs.getPropertyValue(v).trim() || "#888888");
}

export function safeFileName(name: string) {
  return (name || "database").replace(/[^\w.-]+/g, "_").replace(/^\.+/, "") || "database";
}

/** The diagram as SVG text. ``world`` is the area to export in diagram coordinates. */
export function buildErdSvg(svg: SVGSVGElement, world: Box, opts: ErdExportOptions, meta: { db: string; date: string; tables: number }) {
  const clone = svg.cloneNode(true) as SVGSVGElement;
  clone.querySelectorAll("[data-noexport]").forEach((x) => x.remove());
  // no selection highlight in the picture
  clone.querySelectorAll("[data-sel]").forEach((x) => { x.setAttribute("stroke", x.getAttribute("data-sel") || "var(--border)"); x.removeAttribute("data-sel"); x.setAttribute("stroke-width", "1"); });
  clone.querySelectorAll("[data-hl]").forEach((x) => {
    const paths = x.querySelectorAll("path");
    paths.forEach((p) => p.setAttribute("stroke", x.getAttribute("data-hl") || "var(--muted)"));
    paths[0]?.setAttribute("stroke-width", "1.3");
    x.removeAttribute("data-hl");
  });
  clone.querySelectorAll("[style]").forEach((x) => x.removeAttribute("style"));
  clone.querySelectorAll("[class]").forEach((x) => x.removeAttribute("class"));
  clone.querySelectorAll("[data-table],[data-col]").forEach((x) => { x.removeAttribute("data-table"); x.removeAttribute("data-col"); });
  const g = clone.querySelector("g") as SVGGElement | null;
  g?.removeAttribute("transform");
  for (const a of ["class", "style", "width", "height"]) clone.removeAttribute(a);

  const x0 = world.x - MARGIN, w = world.w + 2 * MARGIN;
  const top = opts.title ? TITLE_H : 0;
  const y0 = world.y - MARGIN - top, h = world.h + 2 * MARGIN + top;
  clone.setAttribute("xmlns", SVG_NS);
  clone.setAttribute("viewBox", `${r(x0)} ${r(y0)} ${r(w)} ${r(h)}`);
  clone.setAttribute("width", String(Math.ceil(w)));
  clone.setAttribute("height", String(Math.ceil(h)));
  clone.setAttribute("font-family", "var(--mono)");
  clone.setAttribute("role", "img");
  const el = (name: string, attrs: Record<string, string | number>, text?: string) => {
    const e = document.createElementNS(SVG_NS, name);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v));
    if (text !== undefined) e.textContent = text;
    return e;
  };
  const first = clone.firstChild;
  clone.insertBefore(el("title", {}, t("{db} entity-relationship diagram", { db: meta.db })), first);
  if (opts.background === "solid") {
    const fill = opts.theme === "light" ? "#ffffff" : "var(--bg)";
    clone.insertBefore(el("rect", { x: r(x0), y: r(y0), width: r(w), height: r(h), fill }), first);
  }
  if (opts.title) {
    const tx = x0 + MARGIN, ty = y0 + MARGIN;
    clone.insertBefore(el("text", { x: r(tx), y: r(ty + 6), "font-size": 16, "font-weight": 700, fill: "var(--text)", "font-family": "var(--mono)" }, meta.db), first);
    clone.insertBefore(el("text", { x: r(tx), y: r(ty + 24), "font-size": 11, fill: "var(--muted)", "font-family": "var(--mono)" },
      t("{date} · {n} tables", { date: meta.date, n: meta.tables })), first);
  }
  const resolve = palette(opts.theme);
  // values go into XML attributes: no double quotes
  const text = new XMLSerializer().serializeToString(clone).replace(/var\((--[\w-]+)\)/g, (_, v) => resolve(v).replace(/"/g, "'"));
  return { text: `<?xml version="1.0" encoding="UTF-8"?>\n${text}`, w, h };
}

const r = (n: number) => Math.round(n * 100) / 100;

/** PNG size for a requested scale, reduced to stay within the canvas limits. */
export function pngSize(w: number, h: number, scale: number) {
  const s = Math.min(scale, MAX_SIDE / w, MAX_SIDE / h, Math.sqrt(MAX_PIXELS / (w * h)));
  return { scale: s, width: Math.max(1, Math.floor(w * s)), height: Math.max(1, Math.floor(h * s)), limited: s < scale - 1e-9 };
}

export function renderPng(svgText: string, w: number, h: number, scale: number): Promise<Blob> {
  return new Promise((resolve, reject) => {
    const size = pngSize(w, h, scale);
    const img = new Image();
    img.onload = () => {
      const canvas = document.createElement("canvas");
      canvas.width = size.width;
      canvas.height = size.height;
      const ctx = canvas.getContext("2d");
      if (!ctx) return reject(new Error(t("The browser cannot create a canvas this large")));
      ctx.scale(size.width / w, size.height / h);
      ctx.drawImage(img, 0, 0, w, h);
      canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error(t("The browser cannot create a canvas this large")))), "image/png");
    };
    img.onerror = () => reject(new Error(t("PNG export failed")));
    img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svgText);
  });
}

export const canCopyImage = () => typeof window !== "undefined" && "ClipboardItem" in window && !!navigator.clipboard?.write;

export function ErdExportDialog(props: {
  dbName: string;
  tables: number;
  build: (opts: ErdExportOptions, meta: { db: string; date: string; tables: number }) => { text: string; w: number; h: number };
}) {
  const app = useApp();
  const [format, setFormat] = useState<"svg" | "png">("svg");
  const [opts, setOpts] = useState<ErdExportOptions>({ scope: "all", theme: "light", background: "solid", title: false });
  const [scale, setScale] = useState(2);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const close = () => app.showDialog(null);
  const date = useMemo(() => {
    const d = new Date(), p = (n: number) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }, []);
  const out = useMemo(() => {
    try { return props.build(opts, { db: props.dbName, date, tables: props.tables }); } catch { return null; }
  }, [opts]);
  const size = out ? pngSize(out.w, out.h, scale) : null;
  const [preview, setPreview] = useState("");
  useEffect(() => { setPreview(out ? "data:image/svg+xml;charset=utf-8," + encodeURIComponent(out.text) : ""); }, [out]);
  const base = safeFileName(props.dbName);

  const run = async (copy: boolean) => {
    if (!out) return;
    setBusy(true);
    setError(null);
    try {
      if (copy) {
        // Safari wants the ClipboardItem created synchronously, with a promise for the data
        await navigator.clipboard.write([new ClipboardItem({ "image/png": renderPng(out.text, out.w, out.h, scale) })]);
        app.toast(t("Diagram copied as an image"), "info");
      } else if (format === "svg") {
        saveBlob(new Blob([out.text], { type: "image/svg+xml" }), `${base}-erd.svg`);
      } else {
        saveBlob(await renderPng(out.text, out.w, out.h, scale), `${base}-erd.png`);
        if (size?.limited) app.toast(t("The PNG was scaled down to {w} × {h} px to stay within the browser's canvas limits", { w: size.width, h: size.height }), "info");
      }
      close();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  const set = <K extends keyof ErdExportOptions>(k: K, v: ErdExportOptions[K]) => setOpts({ ...opts, [k]: v });
  const seg = <K extends keyof ErdExportOptions>(k: K, items: Array<[ErdExportOptions[K], string]>, label: string) => (
    <div className="seg" role="group" aria-label={label}>
      {items.map(([v, l]) => <button key={String(v)} type="button" className={opts[k] === v ? "on" : ""} aria-pressed={opts[k] === v} onClick={() => set(k, v)}>{l}</button>)}
    </div>
  );

  return (
    <Modal title={t("Export diagram")} onClose={close}
      footer={<>
        {error && <span className="err">{error}</span>}
        {canCopyImage() && <button className="btn" disabled={busy || !out} onClick={() => run(true)}><IconCopy /> {t("Copy as image")}</button>}
        <button className="btn" onClick={close}>{t("Cancel")}</button>
        <button className="btn primary" disabled={busy || !out} onClick={() => run(false)}><IconDownload /> {t("Download")}</button>
      </>}>
      <div className="form erd-export">
        <label className="l">{t("Format")}</label>
        <div className="seg" role="group" aria-label={t("Format")}>
          <button type="button" className={format === "svg" ? "on" : ""} aria-pressed={format === "svg"} onClick={() => setFormat("svg")}>SVG</button>
          <button type="button" className={format === "png" ? "on" : ""} aria-pressed={format === "png"} onClick={() => setFormat("png")}>PNG</button>
        </div>
        <label className="l">{t("Area")}</label>
        {seg("scope", [["all", t("Whole diagram")], ["view", t("Visible area")]], t("Area"))}
        <label className="l">{t("Colours")}</label>
        {seg("theme", [["light", t("Light")], ["current", t("Current theme")]], t("Colours"))}
        <label className="l">{t("Background")}</label>
        {seg("background", [["solid", opts.theme === "light" ? t("White") : t("Theme background")], ["transparent", t("Transparent")]], t("Background"))}
        <span />
        <label className="check"><input type="checkbox" checked={opts.title} onChange={(e) => set("title", e.target.checked)} /> {t("Title block (database name and date)")}</label>
        {format === "png" && <>
          <label className="l">{t("Resolution")}</label>
          <div className="seg" role="group" aria-label={t("Resolution")}>
            {[1, 2].map((s) => <button key={s} type="button" className={scale === s ? "on" : ""} aria-pressed={scale === s} onClick={() => setScale(s)}>{s}×</button>)}
          </div>
        </>}
        <span />
        <div className="muted" style={{ fontSize: 12 }}>
          {out && (format === "svg" || !size
            ? t("{w} × {h} px · {file}", { w: Math.ceil(out.w), h: Math.ceil(out.h), file: `${base}-erd.svg` })
            : t("{w} × {h} px · {file}", { w: size.width, h: size.height, file: `${base}-erd.png` }))}
          {format === "png" && size?.limited && <div style={{ color: "var(--warn)" }}>{t("Scaled down: a PNG is limited to {side} px per side and {mp} megapixels. Use SVG for very large diagrams.", { side: MAX_SIDE, mp: MAX_PIXELS / 1e6 })}</div>}
        </div>
        <div className="full erd-export-preview" style={{ background: opts.background === "transparent" ? undefined : opts.theme === "light" ? "#fff" : "var(--bg)" }}>
          {preview && <img src={preview} alt={t("Preview")} />}
        </div>
      </div>
    </Modal>
  );
}
