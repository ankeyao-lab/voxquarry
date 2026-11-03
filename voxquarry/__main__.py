import argparse
import os
import uvicorn

parser = argparse.ArgumentParser(description="VoxQuarry local workbench")
parser.add_argument("--port", type=int, default=18763)
args = parser.parse_args()
os.environ.setdefault("VOXQUARRY_ORIGIN", f"http://127.0.0.1:{args.port}")
os.environ.setdefault("VOXQUARRY_INTERNAL_URL", f"http://127.0.0.1:{args.port}")
uvicorn.run("voxquarry.server:create_app", factory=True, host="127.0.0.1", port=args.port)
