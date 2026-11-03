"""Docker HEALTHCHECK: unhealthy when the scheduler has not completed a sync for too long."""
import os
import sys
import urllib.request

try:
    port = os.environ.get("DUBARR_PORT", "8791")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
