from __future__ import annotations

import os

from .app import create_runtime_app


def main() -> None:
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("uvicorn runtime dependency unavailable") from exc
    host = os.environ.get("VOXQUARRY_WENQUAN_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("Wenquan domain candidate must bind loopback only")
    port = int(os.environ.get("VOXQUARRY_WENQUAN_PORT", "8792"))
    uvicorn.run(create_runtime_app(), host=host, port=port, access_log=False)


if __name__ == "__main__":
    main()
