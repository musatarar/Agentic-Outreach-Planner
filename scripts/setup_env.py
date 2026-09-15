"""Create .env from .env.example with a freshly generated DJANGO_SECRET_KEY.

Standard library only, and no Django import, so it runs on a clean clone before
any dependency is installed. Idempotent: an existing .env is never touched.
"""

import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"
TARGET = ROOT / ".env"
KEY_LINE = "DJANGO_SECRET_KEY="


def main():
    if TARGET.exists():
        print(f"{TARGET} already exists; leaving it alone.")
        return 0

    lines = EXAMPLE.read_text(encoding="utf-8").splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith(KEY_LINE):
            lines[i] = f"{KEY_LINE}{secrets.token_urlsafe(50)}\n"
            break
    TARGET.write_text("".join(lines), encoding="utf-8")
    print(f"Wrote {TARGET} from {EXAMPLE.name} with a freshly generated DJANGO_SECRET_KEY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
