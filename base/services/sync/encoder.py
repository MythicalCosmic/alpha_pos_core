import json
import uuid as uuid_module
from decimal import Decimal
from datetime import datetime, date


class SyncEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, uuid_module.UUID):
            return str(obj)
        return super().default(obj)


def serialize_payload(data):
    return json.loads(json.dumps(data, cls=SyncEncoder))
