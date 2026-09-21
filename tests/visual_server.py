from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import create_app


app = create_app(
    {
        "TESTING": True,
        "AUTH_DISABLED": True,
        "DATABASE": str(ROOT / "data" / "traceability.db"),
    }
)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5081, use_reloader=False)
