"""Entry point of the headless server build (``fbadmin-server``, issue #11): the web server and the command-line
tool, without the desktop window. Run by the Windows service (WinSW) and the systemd unit."""

import sys

from fbadmin.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
