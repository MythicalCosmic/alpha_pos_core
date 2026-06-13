"""Channels consumers for the in-store realtime layer.

Sync consumers (run in a threadpool, so they can touch the ORM if needed).
Producers broadcast via ``core.realtime.publish`` using ``group_send`` with
``type='broadcast'``, which Channels routes to the ``broadcast()`` handler here.
"""
from asgiref.sync import async_to_sync
from channels.generic.websocket import JsonWebsocketConsumer

# Group names. Single-branch (one till) for now; multi-branch can suffix BRANCH_ID.
ORDERS_GROUP = 'orders'
KDS_GROUP = 'kds'
CASHIERS_GROUP = 'cashiers'


class _GroupConsumer(JsonWebsocketConsumer):
    group = None

    def connect(self):
        # OPEN_LAN appliance: the till trusts its LAN, so LAN clients are accepted.
        # TODO(auth): gate cross-WAN / server-control sockets on session + branch token.
        if self.group:
            async_to_sync(self.channel_layer.group_add)(self.group, self.channel_name)
        self.accept()
        self.send_json({'type': 'connected', 'group': self.group})

    def disconnect(self, code):
        if self.group:
            async_to_sync(self.channel_layer.group_discard)(self.group, self.channel_name)

    def broadcast(self, event):
        # event == {'type': 'broadcast', 'payload': {...}} from publish._send
        self.send_json(event['payload'])


class OrderQueueConsumer(_GroupConsumer):
    """Live order feed for the cashier / customer display."""
    group = ORDERS_GROUP


class KdsConsumer(_GroupConsumer):
    """Kitchen Display System feed."""
    group = KDS_GROUP


class CashierControlConsumer(_GroupConsumer):
    """Server -> till control channel (lock cashier / force logout)."""
    group = CASHIERS_GROUP
