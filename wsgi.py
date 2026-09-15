from __future__ import annotations

import atexit

from app import create_application

runtime = create_application()
app = runtime.app
atexit.register(runtime.stop)

if __name__ == "__main__":
    runtime.app.run()
