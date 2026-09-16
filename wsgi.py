from __future__ import annotations

import atexit
import os

from app import AppOptions, create_application

runtime = create_application(
    options=AppOptions(require_proxy=os.getenv("REQUIRE_PROXY", "1").lower() not in ("0", "false", "no")),
)
app = runtime.app
atexit.register(runtime.stop)

if __name__ == "__main__":
    runtime.app.run()
