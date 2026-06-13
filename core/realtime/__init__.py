"""Realtime layer (Channels) — replaces polling.

Consumers (channel layer = InMemory on local, Redis on server):
  * OrderQueueConsumer  — order.created/item_added/status_changed/paid/cancelled
  * KdsConsumer         — item.ready / order.ready
  * TableMapConsumer    — table.status_changed
  * DrawerConsumer      — drawer.updated / shift.opened/closed
  * LicenseConsumer     — license.status_changed
  * CashierControlConsumer — server->till lock_cashier / force_logout (Active Cashiers)
  * DashboardConsumer / AlertsConsumer — server back-office live tiles + alerts

``publish.py`` holds the ``async_to_sync(group_send)`` helpers the existing order/
shift/stock services call at the same points they fire notifications today.

TODO(Phase 2): implement consumers, publish.py, routing.py.
"""
