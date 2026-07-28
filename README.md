# Alpha POS Core

Shared backend packages used by both `alpha_pos_server` and `alpha_pos_local`.
Each edition pins this repository as a Git submodule and installs it as a Python
package.

## Packages

| Package | Responsibility |
| --- | --- |
| `base` | Orders, users, shifts, payments, treasury, authentication, and synchronization |
| `core.shifts` | Shared shift lifecycle and settlement rules |
| `core.realtime` | Shared Channels consumers and transaction-safe event publishing |
| `stock` | Inventory, purchasing, recipes, production, transfers, and stock accounting |
| `hr` | Employees, attendance, contracts, payroll, expenses, and documents |
| `discounts` | Discount configuration, validation, and application |
| `cashbox` | Cash-drawer expenses and shift cashbox operations |
| `fiscalization` | Fiscal receipt queue and provider interfaces |
| `licensing` | License state, validation, heartbeat, and enforcement |
| `notifications` | Telegram notifications, loyalty, carts, and QR ordering |
| `alpha_pos_core` | Shared Django settings, URLs, WSGI, and ASGI configuration |

Both editions install the same shared Django applications. Edition-specific URL
configuration controls which APIs are exposed; synchronized models must remain
available in both databases.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

Tests for each application live under that application's `tests` package.
Financial, synchronization, authorization, and migration tests are regression
contracts and must pass before either edition updates its submodule pointer.
