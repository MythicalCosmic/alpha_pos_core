from base.repositories.base import BaseSyncRepository
from hr.models import ContractDocument


class ContractDocumentRepository(BaseSyncRepository):
    model = ContractDocument

    @classmethod
    def get_for_contract(cls, contract_id):
        return cls.model.objects.filter(
            contract_id=contract_id, is_deleted=False
        ).select_related('uploaded_by')
