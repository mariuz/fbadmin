import { useEffect, useRef } from "react";
import { EditorState, Compartment } from "@codemirror/state";
import {
  EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter, drawSelection,
  rectangularSelection, placeholder as cmPlaceholder,
} from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { sql, SQLDialect } from "@codemirror/lang-sql";
import { syntaxHighlighting, defaultHighlightStyle, bracketMatching, indentOnInput, HighlightStyle } from "@codemirror/language";
import { autocompletion, closeBrackets, closeBracketsKeymap, completionKeymap } from "@codemirror/autocomplete";
import { searchKeymap, highlightSelectionMatches } from "@codemirror/search";
import { tags as t } from "@lezer/highlight";

const FB_KEYWORDS =
  "absolute action active add admin after all alter always and any as asc ascending at auto autonomous avg " +
  "backup before begin between bin_and bin_or bin_xor bit_length block body both break by caller cascade case cast " +
  "char_length character_length check close coalesce collate collation column comment commit committed computed " +
  "conditional connect constraint containing continue count create cross cstring current current_connection " +
  "current_date current_role current_time current_timestamp current_transaction current_user cursor database " +
  "datediff dateadd day ddl deallocate debug declare default delete deleting descending desc deterministic " +
  "disconnect distinct do domain drop else end engine entry_point escape exception execute exists exit external " +
  "extract fetch filter first first_value for foreign free_it from full function gdscode generated generator " +
  "gen_id global grant granted group having hour identity if iif ignore in inactive increment index inner " +
  "input_type insensitive insert inserting into is isolation join key lag last last_value lateral lead leading " +
  "leave left length level like limbo list local lock long lower manual matched matching max maximum_segment " +
  "merge message min minute module_name month names national natural next no not nullif null nulls numeric " +
  "octet_length of offset on only open option or order outer output_type over overflow overlay package " +
  "pad parameter partition password percent_rank plan position post_event preserve primary privileges " +
  "procedure protected rank rdb$db_key read record_version recreate recursive references release replace " +
  "restart restrict retain return returning returns returning_values revoke right role rollback row_count " +
  "row_number rows savepoint scalar_array schema second segment select sensitive sequence set shadow shared " +
  "similar singular size skip snapshot some sort source sqlcode sqlstate stability start starting starts " +
  "statement statistics sub_type substring suspend table temporary then ties to trailing transaction trigger " +
  "trim true false type uncommitted union unique unknown update updating upper user using value values variable " +
  "varying view wait week when where while with without work write year sql security definer invoker skip locked";
const FB_TYPES =
  "bigint blob boolean char character date decfloat decimal double precision float int int128 integer nchar " +
  "numeric real smallint time timestamp varchar binary varbinary zone";

export const firebirdDialect = SQLDialect.define({
  keywords: FB_KEYWORDS,
  types: FB_TYPES,
  builtin: "rdb$get_context rdb$set_context abs ceil floor round trunc mod power sqrt exp ln log lpad rpad " +
    "replace reverse position char_to_uuid uuid_to_char gen_uuid hash crypt_hash list " +
    "first_day last_day blob_append",
  operatorChars: "*+-%<>!=&|^~/:",
  specialVar: ":",
  identifierQuotes: '"',
  hashComments: false,
  doubleDollarQuotedStrings: false,
});

const highlight = HighlightStyle.define([
  { tag: t.keyword, color: "var(--accent)", fontWeight: "600" },
  { tag: t.typeName, color: "#0e7490" },
  { tag: t.string, color: "#15803d" },
  { tag: t.number, color: "#7c3aed" },
  { tag: t.comment, color: "var(--muted)", fontStyle: "italic" },
  { tag: t.standard(t.name), color: "#2563eb" },
]);

const baseTheme = EditorView.theme({
  "&": { backgroundColor: "var(--panel)", color: "var(--text)" },
  ".cm-gutters": { backgroundColor: "var(--panel-2)", color: "var(--muted)", border: "none", borderRight: "1px solid var(--border)" },
  ".cm-activeLine": { backgroundColor: "color-mix(in srgb, var(--accent-soft) 45%, transparent)" },
  ".cm-activeLineGutter": { backgroundColor: "var(--accent-soft)" },
  ".cm-cursor": { borderLeftColor: "var(--text)" },
  "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection": {
    backgroundColor: "color-mix(in srgb, var(--accent-2) 30%, transparent) !important",
  },
  ".cm-tooltip": { backgroundColor: "var(--panel)", border: "1px solid var(--border)" },
  ".cm-tooltip-autocomplete ul li[aria-selected]": { backgroundColor: "var(--select)", color: "var(--text)" },
});

export interface SqlEditorProps {
  value: string;
  onChange?: (v: string) => void;
  readOnly?: boolean;
  /** table name -> column names for autocompletion */
  schema?: Record<string, string[]>;
  /** Ctrl/Cmd+Enter or F5 */
  onExecute?: (selectionOrAll: string) => void;
  placeholder?: string;
  editorRef?: (view: EditorView | null) => void;
}

/** Returns the selected text, or the whole document if nothing is selected. */
export function selectionOrAll(view: EditorView): string {
  const sel = view.state.selection.main;
  return sel.empty ? view.state.doc.toString() : view.state.sliceDoc(sel.from, sel.to);
}

export function SqlEditor(props: SqlEditorProps) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const langComp = useRef(new Compartment());
  const cbs = useRef(props);
  cbs.current = props;

  useEffect(() => {
    const exec = (v: EditorView) => {
      cbs.current.onExecute?.(selectionOrAll(v));
      return true;
    };
    const state = EditorState.create({
      doc: props.value,
      extensions: [
        lineNumbers(),
        highlightActiveLineGutter(),
        history(),
        drawSelection(),
        rectangularSelection(),
        indentOnInput(),
        bracketMatching(),
        closeBrackets(),
        highlightActiveLine(),
        highlightSelectionMatches(),
        autocompletion(),
        syntaxHighlighting(highlight),
        syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
        baseTheme,
        langComp.current.of(sql({ dialect: firebirdDialect, upperCaseKeywords: true, schema: props.schema })),
        keymap.of([
          { key: "Mod-Enter", run: exec },
          { key: "F5", run: exec },
          ...closeBracketsKeymap,
          ...defaultKeymap,
          ...searchKeymap,
          ...historyKeymap,
          ...completionKeymap,
          indentWithTab,
        ]),
        EditorState.readOnly.of(!!props.readOnly),
        EditorView.editable.of(!props.readOnly),
        props.placeholder ? cmPlaceholder(props.placeholder) : [],
        EditorView.updateListener.of((u) => {
          if (u.docChanged) cbs.current.onChange?.(u.state.doc.toString());
        }),
      ],
    });
    view.current = new EditorView({ state, parent: host.current! });
    props.editorRef?.(view.current);
    return () => {
      props.editorRef?.(null);
      view.current?.destroy();
    };
  }, []);

  // external value changes (e.g. read-only DDL view or "open file")
  useEffect(() => {
    const v = view.current;
    if (v && props.value !== v.state.doc.toString()) {
      v.dispatch({ changes: { from: 0, to: v.state.doc.length, insert: props.value } });
    }
  }, [props.value]);

  useEffect(() => {
    view.current?.dispatch({
      effects: langComp.current.reconfigure(
        sql({ dialect: firebirdDialect, upperCaseKeywords: true, schema: props.schema }),
      ),
    });
  }, [props.schema]);

  return <div className="cm-host" ref={host} />;
}
