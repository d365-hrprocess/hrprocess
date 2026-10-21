"""Read-only deployed reporting checks. Does not claim live ERP reconciliation or Copilot tests."""

import argparse
import hashlib
import hmac
import json
from pathlib import Path
import sys
import time

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from finance_demo_api.settings import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", default="AST-001")
    parser.add_argument("--currency")
    parser.add_argument("--as-of-date")
    parser.add_argument("--invoice-number")
    args = parser.parse_args()
    settings = Settings.load()
    key = hmac.new(
        settings.secret().encode(),
        f"finance-demo/copilot/v1|{settings.tenant_id}|{settings.client_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    checks = []
    with httpx.Client(
        base_url=settings.public_base_url + "/finance/",
        timeout=25,
        follow_redirects=False,
        headers={"X-Finance-Demo-Key": key},
    ) as client:

        def call(path, params=None):
            deadline = time.monotonic() + 150
            while True:
                response = client.get(path, params=params)
                if not response.headers.get("content-type", "").startswith(
                    "application/json"
                ):
                    raise RuntimeError(
                        "Non-JSON response; verify ingress and deployment."
                    )
                value = response.json()
                code = value.get("error", {}).get("code")
                if (
                    code in {"report_preparing", "report_busy"}
                    and time.monotonic() < deadline
                ):
                    time.sleep(5)
                    continue
                if response.status_code != 200 or value.get("success") is not True:
                    raise RuntimeError(
                        f"HTTP {response.status_code}, code={code}; no complete result verified."
                    )
                checks.append(
                    {
                        "path": path,
                        "passed": True,
                        "complete": value.get("meta", {}).get("complete"),
                        "source_records": value.get("meta", {}).get(
                            "source_record_count"
                        ),
                    }
                )
                return value

        try:
            call("ready")
            common = {"currency": args.currency} if args.currency else {}
            overdue = {
                **common,
                **({"as_of_date": args.as_of_date} if args.as_of_date else {}),
            }
            from urllib.parse import quote

            account = quote(args.account, safe="")
            for suffix in ["balance", "open-invoices", "payments"]:
                call("customers/" + account + "/" + suffix, common)
            call("customers/" + account + "/overdue-invoices", overdue)
            call("reports/customer-balances", common)
            call("reports/overdue-customers", overdue)
            if args.invoice_number:
                call(
                    "customers/"
                    + account
                    + "/invoices/"
                    + quote(args.invoice_number, safe=""),
                    common,
                )
        except (httpx.HTTPError, RuntimeError, ValueError) as error:
            print(
                json.dumps(
                    {
                        "passed": False,
                        "checks": checks,
                        "error_type": type(error).__name__,
                        "message": str(error)
                        if isinstance(error, RuntimeError)
                        else "Deployed verification could not finish.",
                    }
                )
            )
            return 1
    print(
        json.dumps(
            {
                "passed": True,
                "checks": checks,
                "live_erp_amount_reconciliation": False,
                "copilot_tested": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
