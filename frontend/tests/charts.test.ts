// Unit tests of the chart data shaping: `npm test` (node --test, no extra dependencies).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  defaultConfig, niceTicks, numericColumns, shapeData, shapedTsv, toTime, truncate, valueRange,
  type ChartConfig, type ColumnLike,
} from "../src/features/charts/shape.ts";

const L = { nullLabel: "(null)", otherLabel: "Other" };
const cols: ColumnLike[] = [
  { name: "CITY", type: "VARCHAR(30)" }, { name: "AMOUNT", type: "NUMERIC(12,2)" }, { name: "QTY", type: "INTEGER" },
  { name: "KIND", type: "VARCHAR(5)" }, { name: "DAY", type: "DATE" },
];
const rows: unknown[][] = [
  ["Rome", "10.50", 1, "a", "2024-01-03"],
  ["Oslo", "-4", 2, "b", "2024-01-01"],
  ["Rome", "5", 3, "b", "2024-01-02"],
  [null, "7", 4, "a", null],
  ["Lima", "not a number", 5, null, "2024-01-02"],
];
const cfg = (p: Partial<ChartConfig>): ChartConfig => ({ type: "bar", x: 0, ys: [1], group: null, agg: "sum", sort: "auto", topN: null, ...p });

test("numeric columns and default configuration", () => {
  assert.deepEqual(numericColumns(cols, rows), [1, 2]);
  assert.deepEqual(numericColumns([{ name: "S", type: "VARCHAR(9)" }], [["1"], ["2.5"], [null]]), [0]);
  const d = defaultConfig(cols, rows);
  assert.equal(d.x, 0);
  assert.deepEqual(d.ys, [1, 2]);
  assert.equal(d.type, "bar");
});

test("repeated categories are aggregated; NULL becomes its own category; order follows the rows", () => {
  const s = shapeData(cols, rows, cfg({}), L);
  assert.deepEqual(s.categories.map((c) => c.label), ["Rome", "Oslo", "(null)", "Lima"]);
  assert.deepEqual(s.series[0].values, [15.5, -4, 7, null]);
  assert.equal(s.stats.nullX, 1);
  assert.equal(s.stats.nonNumeric, 1);
  const avg = shapeData(cols, rows, cfg({ agg: "avg" }), L);
  assert.deepEqual(avg.series[0].values, [7.75, -4, 7, null]);
  const count = shapeData(cols, rows, cfg({ agg: "count" }), L);
  assert.deepEqual(count.series[0].values, [2, 1, 1, 1]);
});

test("sorting and top N with Other", () => {
  const s = shapeData(cols, rows, cfg({ sort: "value-desc", topN: 3 }), L);
  assert.deepEqual(s.categories.map((c) => c.label), ["Rome", "(null)", "Other"]);
  assert.deepEqual(s.series[0].values, [15.5, 7, -4]); // Other = Oslo (-4) + Lima (no number)
  assert.equal(s.stats.collapsedCategories, 2);
  const byLabel = shapeData(cols, rows, cfg({ sort: "label" }), L);
  assert.deepEqual(byLabel.categories.map((c) => c.label), ["Lima", "Oslo", "Rome", "(null)"]);
});

test("grouping column turns into series", () => {
  const s = shapeData(cols, rows, cfg({ group: 3, ys: [2] }), L);
  assert.deepEqual(s.series.map((x) => x.label), ["a", "b", "(null)"]);
  assert.deepEqual(s.series[0].values, [1, null, 4, null]);
  assert.deepEqual(s.series[1].values, [3, 2, null, null]);
});

test("more than 8 series fold into Other", () => {
  const many = Array.from({ length: 12 }, (_, i) => ["x", i + 1, i + 1, `g${i}`, null]);
  const s = shapeData(cols, many, cfg({ group: 3, ys: [1] }), L);
  assert.equal(s.series.length, 8);
  assert.equal(s.series[7].label, "Other");
  assert.deepEqual(s.series[7].values, [1 + 2 + 3 + 4 + 5]);
  assert.equal(s.stats.collapsedSeries, 5);
});

test("pie keeps positive values of one series, at most 8 slices", () => {
  const s = shapeData(cols, rows, cfg({ type: "pie", ys: [1, 2] }), L);
  assert.equal(s.series.length, 1);
  assert.deepEqual(s.categories.map((c) => c.label), ["Rome", "(null)"]); // largest first
  assert.equal(s.stats.nonPositive, 2);
});

test("line chart over a DATE column uses a time axis in chronological order", () => {
  const s = shapeData(cols, rows, cfg({ type: "line", x: 4, ys: [2] }), L);
  assert.equal(s.axis, "time");
  assert.deepEqual(s.categories.map((c) => c.label), ["2024-01-01", "2024-01-02", "2024-01-03"]);
  assert.deepEqual(s.series[0].values, [2, 8, 1]);
  assert.equal(s.stats.nullX, 1);
  assert.equal(toTime("2024-01-02T10:30:00"), new Date(2024, 0, 2, 10, 30).getTime());
});

test("scatter makes one point per row", () => {
  const s = shapeData(cols, rows, cfg({ type: "scatter", x: 2, ys: [1] }), L);
  assert.equal(s.axis, "number");
  assert.deepEqual(s.points.map((p) => [p.x, p.y]), [[1, 10.5], [2, -4], [3, 5], [4, 7]]);
});

test("stacked range sums positives and negatives separately", () => {
  const s = shapeData(cols, [["a", 5, -2, "x", null], ["b", 3, 4, "x", null]], cfg({ type: "stacked", ys: [1, 2] }), L);
  assert.deepEqual(valueRange(s, "stacked"), [-2, 7]);
  assert.deepEqual(valueRange(s, "line"), [-2, 5]);
});

test("ticks, truncation and TSV", () => {
  assert.deepEqual(niceTicks(0, 97), [0, 20, 40, 60, 80, 100]);
  assert.deepEqual(niceTicks(-4, 15.5, 4), [-5, 0, 5, 10, 15, 20]);
  assert.deepEqual(niceTicks(0, 0), [0, 1]);
  assert.equal(truncate("a very long category label", 10), "a very lo…");
  const s = shapeData(cols, rows.slice(0, 2), cfg({ ys: [1, 2] }), L);
  assert.equal(shapedTsv(s, "CITY"), "CITY\tAMOUNT\tQTY\nRome\t10.5\t1\nOslo\t-4\t2\n");
});
