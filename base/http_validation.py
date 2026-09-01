from datetime import date


class QueryValidationError(ValueError):
    def __init__(self, errors):
        super().__init__('Invalid query parameters')
        self.errors = errors


def iso_date(params, name, default=None):
    value = params.get(name)
    if value in (None, ''):
        return default
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise QueryValidationError({name: ['Use YYYY-MM-DD.']}) from exc
    if parsed.isoformat() != value:
        raise QueryValidationError({name: ['Use YYYY-MM-DD.']})
    return parsed


def positive_int(params, name, default=None, maximum=None):
    value = params.get(name)
    if value in (None, ''):
        return default
    if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit():
        raise QueryValidationError({name: ['Use a positive integer.']})
    parsed = int(value)
    if parsed < 1 or (maximum is not None and parsed > maximum):
        message = 'Use a positive integer.'
        if maximum is not None:
            message = f'Use an integer from 1 through {maximum}.'
        raise QueryValidationError({name: [message]})
    return parsed


def optional_int(params, name):
    value = params.get(name)
    if value in (None, ''):
        return None
    return positive_int(params, name)


def boolean(params, name, default=None):
    value = params.get(name)
    if value in (None, ''):
        return default
    normalized = str(value).strip().lower()
    if normalized == 'true':
        return True
    if normalized == 'false':
        return False
    raise QueryValidationError({name: ['Use true or false.']})


def validate_period(date_from, date_to, maximum_days=366):
    if date_to < date_from:
        raise QueryValidationError({
            'date_to': ['Must be on or after date_from.'],
        })
    if (date_to - date_from).days + 1 > maximum_days:
        raise QueryValidationError({
            'date_to': [f'Period cannot exceed {maximum_days} days.'],
        })
