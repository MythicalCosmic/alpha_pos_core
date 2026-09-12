from django.db import transaction

from base.helpers.response import ServiceResponse
from base.models import AppSettings
from base.services.waiter_policy import current_policy


def policy_payload(policy=None):
    policy = policy or current_policy()
    return {
        'waiter_enabled': policy.waiter_enabled,
        'waiter_payment_mode': policy.waiter_payment_mode,
        'waiter_require_shift': policy.waiter_require_shift,
        'waiter_payment_mode_choices': [
            {'value': value, 'label': label} for value, label in AppSettings.WaiterPaymentMode.choices
        ],
    }


@transaction.atomic
def update_policy(values):
    current_policy()
    policy = AppSettings.objects.select_for_update().get(pk=1)
    changed, errors = [], {}
    for field in ('waiter_enabled', 'waiter_payment_mode', 'waiter_require_shift'):
        if field not in values:
            continue
        value = values[field]
        if field == 'waiter_payment_mode':
            valid = value in AppSettings.WaiterPaymentMode.values
        else:
            valid = type(value) is bool
        if not valid:
            errors[field] = 'Choose a supported payment mode.' if field == 'waiter_payment_mode' else 'Use true or false.'
        else:
            setattr(policy, field, value)
            changed.append(field)
    if errors:
        return ServiceResponse.validation_error(errors)
    if changed:
        policy.save(update_fields=[*changed, 'updated_at'])
    return ServiceResponse.success(data={'settings': policy_payload(policy)})
