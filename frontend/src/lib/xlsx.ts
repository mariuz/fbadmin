/** Minimal XLSX (Office Open XML) writer: one sheet, typed numbers/booleans, bold header row. */
import { strToU8, zipSync } from "fflate";

const esc = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;")
    // XML 1.0 forbids most control characters
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g, "");

function colName(i: number): string {
  let s = "";
  for (i += 1; i > 0; i = Math.floor((i - 1) / 26)) s = String.fromCharCode(65 + ((i - 1) % 26)) + s;
  return s;
}

function cell(ref: string, v: unknown, numeric: boolean, style = 0): string {
  if (v === null || v === undefined) return "";
  const s = style ? ` s="${style}"` : "";
  if (typeof v === "boolean") return `<c r="${ref}" t="b"${s}><v>${v ? 1 : 0}</v></c>`;
  if ((typeof v === "number" && isFinite(v)) || (numeric && typeof v === "string" && /^-?\d+(\.\d+)?$/.test(v) && v.length < 16)) {
    return `<c r="${ref}"${s}><v>${v}</v></c>`;
  }
  return `<c r="${ref}" t="inlineStr"${s}><is><t xml:space="preserve">${esc(String(v))}</t></is></c>`;
}

export function buildXlsx(columns: Array<{ name: string; type?: string }>, rows: unknown[][], sheet = "Result"): Uint8Array {
  const numeric = columns.map((c) => /INT|NUMERIC|DOUBLE|FLOAT|DECFLOAT|SMALLINT|BIGINT/.test(c.type ?? ""));
  const head = `<row r="1">${columns.map((c, i) => cell(`${colName(i)}1`, c.name, false, 1)).join("")}</row>`;
  const body = rows
    .map((r, ri) => `<row r="${ri + 2}">${r.map((v, ci) => cell(`${colName(ci)}${ri + 2}`, v, numeric[ci])).join("")}</row>`)
    .join("");
  const widths = columns.map((c, i) => {
    const max = Math.min(60, Math.max(c.name.length, ...rows.slice(0, 200).map((r) => String(r[i] ?? "").length)) + 2);
    return `<col min="${i + 1}" max="${i + 1}" width="${max}" customWidth="1"/>`;
  }).join("");
  const sheetXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>${columns.length ? `<cols>${widths}</cols>` : ""}<sheetData>${head}${body}</sheetData></worksheet>`;
  const files: Record<string, Uint8Array> = {
    "[Content_Types].xml": strToU8(`<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>`),
    "_rels/.rels": strToU8(`<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>`),
    "xl/workbook.xml": strToU8(`<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="${esc(sheet.slice(0, 31).replace(/[\\/?*[\]:]/g, "_"))}" sheetId="1" r:id="rId1"/></sheets></workbook>`),
    "xl/_rels/workbook.xml.rels": strToU8(`<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>`),
    "xl/styles.xml": strToU8(`<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf fontId="0" xfId="0"/><xf fontId="1" xfId="0" applyFont="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>`),
    "xl/worksheets/sheet1.xml": strToU8(sheetXml),
  };
  return zipSync(files, { level: 6 });
}
