import type { ReactElement } from "react";

const p = (d: string | ReactElement, color?: string) => (props: { size?: number; className?: string }) => (
  <svg
    width={props.size ?? 15}
    height={props.size ?? 15}
    viewBox="0 0 24 24"
    fill="none"
    stroke={color ?? "currentColor"}
    strokeWidth={1.8}
    strokeLinecap="round"
    strokeLinejoin="round"
    className={props.className}
    style={{ flex: "none" }}
  >
    {typeof d === "string" ? <path d={d} /> : d}
  </svg>
);

export const Flame = (props: { size?: number }) => (
  <svg width={props.size ?? 20} height={props.size ?? 20} viewBox="0 0 32 32">
    <path d="M16 2c2 6 9 9 9 17a9 9 0 0 1-18 0c0-5 3-7 4-11 1 3 2 4 4 5-1-4 0-8 1-11z" fill="#ea580c" />
    <path d="M16 15c1 3 4 4 4 8a4 4 0 0 1-8 0c0-2 1-3 2-5 .5 1 1 1.5 2 2-.5-2 0-3.5 0-5z" fill="#fbbf24" />
  </svg>
);
export const IconServer = p(<><rect x="3" y="4" width="18" height="7" rx="1.5" /><rect x="3" y="13" width="18" height="7" rx="1.5" /><path d="M7 7.5h.01M7 16.5h.01" /></>, "#5b7083");
export const IconDatabase = p(<><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5" /><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3" /></>, "#c2410c");
export const IconFolder = p("M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z", "#c9a227");
export const IconSchema = p(<><path d="M12 3l9 5-9 5-9-5z" /><path d="M3 13l9 5 9-5" /></>, "#7c3aed");
export const IconTable = p(<><rect x="3" y="4" width="18" height="16" rx="1.5" /><path d="M3 10h18M3 15h18M9 10v10" /></>, "#3b82f6");
export const IconView = p(<><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" /><circle cx="12" cy="12" r="3" /></>, "#0891b2");
export const IconProc = p(<><path d="M8 6l-6 6 6 6M16 6l6 6-6 6" /></>, "#7c3aed");
export const IconFunc = p("M14 4h-2a3 3 0 0 0-3 3v13M6 11h7M15 14l5 6M20 14l-5 6", "#9333ea");
export const IconPackage = p(<><path d="M21 8l-9-5-9 5v8l9 5 9-5z" /><path d="M3 8l9 5 9-5M12 13v8" /></>, "#a16207");
export const IconTrigger = p("M13 2L4 14h7l-1 8 9-12h-7z", "#e11d48");
export const IconSequence = p("M4 7h4M6 5v4M10 17h4M12 15v4M16 7h4M4 17h2M17 15l2 4", "#0d9488");
export const IconDomain = p(<><path d="M4 4h16v16H4z" /><path d="M9 9h6v6H9z" /></>, "#64748b");
export const IconException = p(<><circle cx="12" cy="12" r="9" /><path d="M12 7v6M12 16.5h.01" /></>, "#dc2626");
export const IconIndex = p("M4 6h16M4 12h10M4 18h6", "#2563eb");
export const IconRole = p(<><circle cx="9" cy="8" r="3.5" /><path d="M2.5 20a6.5 6.5 0 0 1 13 0" /><path d="M16 4.5a3.5 3.5 0 0 1 0 7M18 14a6 6 0 0 1 3.5 6" /></>, "#15803d");
export const IconUser = p(<><circle cx="12" cy="8" r="4" /><path d="M4 21a8 8 0 0 1 16 0" /></>, "#15803d");
export const IconColumn = p(<><rect x="8" y="3" width="8" height="18" rx="1.5" /></>, "#64748b");
export const IconKey = p(<><circle cx="8" cy="15" r="4" /><path d="M11 12l9-9M17 6l3 3" /></>, "#d97706");
export const IconPlay = p("M6 4l14 8-14 8z", "#16a34a");
export const IconPlus = p("M12 5v14M5 12h14");
export const IconRefresh = p(<><path d="M20 11a8 8 0 1 0-2.3 5.7" /><path d="M20 4v7h-7" /></>);
export const IconTrash = p("M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13");
export const IconChevron = p("M9 6l6 6-6 6");
export const IconChevronDown = p("M6 9l6 6 6-6");
export const IconSpinner = p("M12 3a9 9 0 1 0 9 9");
export const IconTerminal = p(<><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M7 9l3 3-3 3M13 15h4" /></>);
export const IconActivity = p("M3 12h4l3 8 4-16 3 8h4");
export const IconSave = p(<><path d="M5 3h11l4 4v14H5z" /><path d="M8 3v5h7M8 21v-7h8v7" /></>);
export const IconX = p("M6 6l12 12M18 6L6 18");
export const IconSun = p(<><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></>);
export const IconMoon = p("M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z");
export const IconDownload = p("M12 4v12M6 10l6 6 6-6M4 20h16");
export const IconUpload = p("M12 20V8M6 14l6-6 6 6M4 4h16");
export const IconCopy = p(<><rect x="8" y="8" width="12" height="12" rx="2" /><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3" /></>);
export const IconPlan = p("M4 4v6h6M4 10l6 6h6v4h4");
export const IconInfo = p(<><circle cx="12" cy="12" r="9" /><path d="M12 11v6M12 7.5h.01" /></>);
export const IconFilter = p("M3 5h18l-7 8v6l-4 2v-8z");
export const IconLog = p(<><path d="M6 3h9l4 4v14H6z" /><path d="M9 12h7M9 16h7M9 8h3" /></>);

export const TYPE_ICONS: Record<string, (p: { size?: number }) => ReactElement> = {
  tables: IconTable,
  system_tables: IconTable,
  views: IconView,
  procedures: IconProc,
  functions: IconFunc,
  packages: IconPackage,
  triggers: IconTrigger,
  sequences: IconSequence,
  domains: IconDomain,
  exceptions: IconException,
  indices: IconIndex,
  roles: IconRole,
  users: IconUser,
};
