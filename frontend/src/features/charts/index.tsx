/** Charts from query results: a "Chart" view next to the Query Tool's result grid. */
import "./charts.css";
import { registerResultView } from "../../registry";
import { N_ } from "../../i18n";
import { ChartView } from "./ChartView";

const IconChart = () => (
  <svg width={13} height={13} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" aria-hidden="true">
    <path d="M4 20V4M4 20h16M8 16v-5M12 16V8M16 16v-3" />
  </svg>
);

registerResultView({ id: "chart", title: N_("Chart"), icon: <IconChart />, render: (ctx) => <ChartView ctx={ctx} /> });
