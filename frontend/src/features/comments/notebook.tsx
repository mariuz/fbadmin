/** The comments of a notebook, below its cells (features/notebooks/Notebook.tsx). */
import { useState } from "react";
import { t } from "../../i18n";
import { CommentThread } from "./index";

export function NotebookComments(props: { id: string }) {
  const [n, setN] = useState<number | null>(null);
  return (
    <details className="nb-comments" open>
      <summary>{n ? t("Comments ({n})", { n }) : t("Comments")}</summary>
      <CommentThread label={t("Comments on this notebook")} target={{ kind: "notebook", notebook_id: props.id }} onCount={setN} />
    </details>
  );
}
