# Charts from query results

The Query Tool can show a result set as a chart. Run a query, then click **Chart** next to
**Grid** above the results. The chart uses the rows the grid has fetched; nothing is queried
again.

## Chart types

| Type | Good for |
|---|---|
| Bar | comparing values across categories |
| Horizontal bar | the same, with long category names |
| Stacked bar | parts of a total per category (positive values stack up, negative values down) |
| Line | values over time or another ordered axis |
| Area | the same, with the area under each line shaded |
| Pie, Donut | shares of one total (at most 8 slices) |
| Scatter | one point per row, two numeric (or date) columns |

## Choosing the data

| Control | Meaning |
|---|---|
| Category (X axis for scatter) | The column along the axis. The first text or date column is picked first. |
| Values | One or more numeric columns, one series each (up to 8). |
| Series from | Instead of one series per value column, one series per value of this column (it uses the first value column). Example: `REGION` as category, `PRODUCT` as series, `AMOUNT` as value. |
| Combine | How to combine rows with the same category: sum (default), average, count rows, minimum, maximum, first value. |
| Sort | As fetched (dates and numbers in order), by category, largest first, smallest first. |
| Top | Keep the N largest categories and combine the rest into **Other**. Empty means all. |

Details:

- **NULL** categories are shown as *(null)*. On a time or number axis, and in scatter charts,
  rows without an X value are left out and counted in the note above the chart.
- Values that are not numbers (e.g. text in a numeric-looking column) are ignored and counted.
- Text columns whose fetched values are all numbers can be used as values too.
- **Dates:** with a `DATE` or `TIMESTAMP` category, line, area and scatter charts use a time axis
  (points are placed by date; labels show years, months, days or hours depending on the range).
  Bar charts list the dates in order.
- **Many categories:** a category axis shows at most 200 categories; the rest go into **Other**.
  Labels longer than 18 characters (26 for horizontal bars) are shortened; the tooltip and the
  data table show them in full. Crowded axis labels are tilted or thinned.
- **Many series:** more than 8 series (e.g. with **Series from**) keep the 7 largest and combine
  the rest into **Other**.
- Pie and donut charts leave out zero and negative values and say how many.
- The chart remembers its settings for results with the same columns, so running the query again
  keeps the chart as it was.

## Rows charted

The line under the controls says how many rows are charted. When the query has more rows than were
fetched, **Load all rows (up to 50,000)** fetches the rest from the open result set (the same
mechanism as **Load more** in the grid, in steps of 10,000). The grid then has those rows too.

## Reading the chart

- Point at a bar, point or slice for a tooltip with the exact values.
- With the keyboard: Tab to the chart, then use the arrow keys (Home / End for the first and last
  item, Escape to hide the tooltip). Screen readers hear the same values.
- **Data table** shows the charted data as a table under the chart.
- Series are named in the legend; lines and scatter points also use a different marker shape per
  series, so colour is never the only way to tell them apart.

The colours come from a fixed 8-colour palette checked for colour-vision deficiencies, with separate
steps for the light, dark and high-contrast themes.

## Export

- **SVG** downloads the chart as `chart-<type>.svg` (with the legend and a title, in the current
  theme's colours).
- **PNG** downloads it as a PNG image at twice the screen resolution.
- **TSV** copies the charted data (after combining, top N and grouping) to the clipboard, ready
  to paste into a spreadsheet.

## For developers

The chart is a *result view* registered with `registerResultView` (`frontend/src/registry.tsx`).
The Query Tool shows registered views as buttons next to **Grid** and passes them the columns,
rows, and a `loadMore(rows)` function. The data shaping is a pure module
(`frontend/src/features/charts/shape.ts`) covered by unit tests: `cd frontend && npm test`.
