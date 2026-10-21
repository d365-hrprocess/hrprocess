"""Private administrator console actions. Never an HTTP route."""
from __future__ import annotations

import argparse
import sys

from .errors import FinanceError
from .settings import Settings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Finance demo administrator console")
    parser.add_argument("command", choices=["show-connector-key"])
    args = parser.parse_args(argv)
    settings = Settings.load()
    if args.command == "show-connector-key":
        if settings.authentication_mode != "derived_api_key":
            print("The finance module is not configured for a derived connector key.", file=sys.stderr)
            return 1
        try:
            # Import lazily to keep administrator usage independent of API startup.
            import hashlib
            import hmac
            message = f"finance-demo/copilot/v1|{settings.tenant_id}|{settings.client_id}"
            key = hmac.new(settings.secret().encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
        except FinanceError:
            print("The finance client secret VALUE is missing or invalid. Configure FINANCE_DEMO_CLIENT_SECRET privately.", file=sys.stderr)
            return 1
        # Intentional administrator-only output; never called from web routes/logs.
        print(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
