import os
import uvicorn

from .app import create_app


def main():
    host = os.environ.get("MESH_HOST", "127.0.0.1")
    port = int(os.environ.get("MESH_PORT", "8765"))
    uvicorn.run(create_app(), host=host, port=port, workers=1)


if __name__ == "__main__":
    main()
