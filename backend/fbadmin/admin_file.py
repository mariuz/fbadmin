"""``fbadmin-server write-admin-file PATH --user=NAME --email=EMAIL --password=PASSWORD`` (internal, issue #11).

The Windows server MSI runs this as a custom action whose command line it hides in its log, to write the one-time
administrator file (``FBADMIN_ADMIN_FILE``, read and deleted by fbadmin at its first start in server mode). MSI's
own IniFile action would log the password, and MSI cannot escape a value for a command line, so the options are
read from the raw command line: everything after ``--password=`` is the password, quotes, spaces and backslashes
included.
"""

from __future__ import annotations

import sys
from pathlib import Path

MARKERS = (" --user=", " --email=", " --password=")


def _raw_command_line() -> str:
    if sys.platform == "win32":
        import ctypes

        get = ctypes.windll.kernel32.GetCommandLineW  # type: ignore[attr-defined]
        get.restype = ctypes.c_wchar_p
        return get()
    return " ".join(sys.argv)


def parse(raw: str) -> tuple[str, dict[str, str]]:
    """The file path and the values, from ``... write-admin-file "PATH" --user=... --email=... --password=...``."""
    rest = raw.split("write-admin-file", 1)[1]
    found = sorted((rest.find(m), m) for m in MARKERS if rest.find(m) >= 0)
    if not found or found[-1][1] != " --password=":
        raise ValueError("--password= must come last")
    path = rest[: found[0][0]].strip().strip('"')
    values: dict[str, str] = {}
    for i, (pos, marker) in enumerate(found):
        end = found[i + 1][0] if i + 1 < len(found) else len(rest)
        value = rest[pos + len(marker): end]
        values[marker.strip(" -=")] = value if marker == " --password=" else value.strip().strip('"')
    values["password"] = values["password"].rstrip("\r\n")
    if not path:
        raise ValueError("no file given")
    return path, values


def write(path: Path, values: dict[str, str]) -> None:
    lines = ["[admin]"]
    for key, out in (("user", "username"), ("email", "email"), ("password", "password")):
        if values.get(key):
            lines.append(f'{out}="{values[key]}"')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", "utf-8")


def main(raw: str | None = None) -> int:
    try:
        path, values = parse(raw if raw is not None else _raw_command_line())
    except (ValueError, IndexError) as exc:
        print(f"write-admin-file: {exc}", file=sys.stderr)
        return 2
    if not values.get("password"):
        return 0  # nothing to write
    write(Path(path), values)
    return 0
