from notifications.services.sender_service import SenderService


class HRNotification:

    @classmethod
    def on_contract_expiring(cls, contract, days_until):
        emp = contract.employee
        SenderService.send('hr.contract_expiry', {
            'employee_name': f'{emp.user.first_name} {emp.user.last_name}',
            'contract_number': contract.contract_number,
            'end_date': str(contract.end_date),
            'days_until': days_until,
        })

    @classmethod
    def on_probation_ending(cls, contract, days_until):
        emp = contract.employee
        SenderService.send('hr.probation_end', {
            'employee_name': f'{emp.user.first_name} {emp.user.last_name}',
            'probation_end_date': str(contract.probation_end_date),
            'days_until': days_until,
        })

    @classmethod
    def on_document_expiring(cls, doc, days_until):
        emp = doc.employee
        SenderService.send('hr.document_expiry', {
            'employee_name': f'{emp.user.first_name} {emp.user.last_name}',
            'document_title': doc.title,
            'document_type': doc.get_document_type_display(),
            'expiry_date': str(doc.expiry_date),
            'days_until': days_until,
        })
