# alpha_pos_core

The **shared spine** of Alpha POS. Both editions (`alpha_pos_server`,
`alpha_pos_local`) consume this repo as a **git submodule + editable install**, so
every synced model, the sync engine, auth, and the shared business apps are defined
**once** and can never drift between editions.

## What lives here

| Package | Role |
|---------|------|
| `base` | Core models (User, Order, Product, Shift, CashRegister, …), auth, security, **the entire sync engine** `base/services/sync/*` |
| `stock` | Inventory — sell-side decrement (local) + purchasing/recipes/production (server), same tables |
| `discounts` | Discount catalog + apply-at-POS |
| `cashbox` | Cash drawer / reconciliation / settlement |
| `fiscalization` | Fiscal receipt signing (provider fires local; table read server) |
| `licensing` | License kill-switch middleware + heartbeat (required on both editions) |
| `notifications` | Templates / routing / worker / loyalty (the order-taking half routes local-only) |
| `core/` | **New shared shims introduced by the split** (see below) |
| `alpha_pos_core/` | `settings_base.py` + shared config (per-edition settings extend it) |

## `core/` — new shims (being filled during the migration)

- `core/shifts/` — `ShiftService` relocated out of `admins` (used by the local till).
- `core/attendance/` — `pos_hook.py`: writes the `AUTO_POS` attendance row at login
  so the local POS no longer imports `hr.services`; no-op if `hr` isn't installed.
- `core/realtime/` — Channels consumers (`OrderQueue`, `KDS`, `TableMap`, `Drawer`,
  `License`, `CashierControl`) + `publish.py` (`group_send` helpers producers call).
- `core/sync_ws/` — websocket transport + consumer that **reuse** the existing
  durable queue/cursor/idempotent-receiver in `base/services/sync/*` (not a rewrite).

## Golden rule

**Trim `urls.py`, never trim `MODEL_MAP`.** Every model in
`base/services/sync/config.py` ships its table to both editions, even when its UI does
not. See `../WORKSPACE.md`.

## Status

Apps copied from the monolith. Next: extract `settings_base.py`, add the `core/` shim
implementations, make this pip-installable into both editions, get `manage.py check`
green.
