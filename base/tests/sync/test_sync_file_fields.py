import json

from django.test import TestCase

from base.models import SyncMixin
from hr.models import ContractDocument


class SyncFileFieldSerializationTests(TestCase):
    def test_file_field_is_serialized_as_storage_relative_name(self):
        document = ContractDocument(file="hr/contracts/2026/07/agreement.pdf")

        payload = SyncMixin.to_sync_dict(document)

        self.assertEqual(
            payload["file"], "hr/contracts/2026/07/agreement.pdf"
        )
        # This is the operation the durable sync queue performs. A FieldFile
        # object here used to raise TypeError and abort the originating save.
        json.dumps(payload)
