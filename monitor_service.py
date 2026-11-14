import os
import sys

if getattr(sys, "frozen", False):
    root = os.path.dirname(sys.executable)
else:
    root = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("PRICE_MONITOR_ROOT", root)

from monitor.main import main


if __name__ == "__main__":
    raise SystemExit(main())
