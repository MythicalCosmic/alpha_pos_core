from django.db import transaction
from discounts.repositories import DiscountTypeRepository
from base.helpers.response import ServiceResponse


def _serialize_discount_type(dt):
    return {
        'id': dt.id,
        'uuid': str(dt.uuid),
        'name': dt.name,
        'code': dt.code,
        'description': dt.description,
        'discount_method': dt.discount_method,
        'discount_method_display': dt.get_discount_method_display(),
        'is_active': dt.is_active,
        'discount_count': dt.discounts.filter(is_deleted=False).count(),
        'created_at': dt.created_at.isoformat() if dt.created_at else None,
    }


class DiscountTypeService:

    @staticmethod
    def list(page=1, per_page=20, is_active=None):
        if is_active is not None:
            queryset = DiscountTypeRepository.filter(is_active=is_active)
        else:
            queryset = DiscountTypeRepository.get_all()

        queryset = queryset.order_by('name')
        page_obj, paginator = DiscountTypeRepository.paginate(queryset, page, per_page)

        return ServiceResponse.success(data={
            'discount_types': [_serialize_discount_type(dt) for dt in page_obj.object_list],
            'pagination': {
                'current_page': page_obj.number,
                'total_pages': paginator.num_pages,
                'total_items': paginator.count,
                'per_page': per_page,
                'has_next': page_obj.has_next(),
                'has_previous': page_obj.has_previous(),
            },
        })

    @staticmethod
    def get(type_id):
        dt = DiscountTypeRepository.get_by_id(type_id)
        if not dt:
            return ServiceResponse.not_found("Discount type not found")

        return ServiceResponse.success(data={
            'discount_type': _serialize_discount_type(dt),
        })

    @staticmethod
    @transaction.atomic
    def create(name, code, description='', discount_method='PERCENTAGE'):
        name = name.strip()
        code = code.strip()

        if not name:
            return ServiceResponse.validation_error(
                errors={'name': 'Name is required'},
                message='Name is required',
            )

        if not code:
            return ServiceResponse.validation_error(
                errors={'code': 'Code is required'},
                message='Code is required',
            )

        if DiscountTypeRepository.code_exists(code):
            return ServiceResponse.error("Discount type with this code already exists")

        from discounts.models import DiscountType
        valid_methods = [c[0] for c in DiscountType.Method.choices]
        if discount_method not in valid_methods:
            return ServiceResponse.validation_error(
                errors={'discount_method': f'Must be one of: {", ".join(valid_methods)}'},
                message='Invalid discount method',
            )

        dt = DiscountTypeRepository.create(
            name=name,
            code=code,
            description=description or '',
            discount_method=discount_method,
        )

        return ServiceResponse.created(
            data={'discount_type': _serialize_discount_type(dt)},
            message="Discount type created successfully",
        )

    @staticmethod
    @transaction.atomic
    def update(type_id, **kwargs):
        dt = DiscountTypeRepository.get_by_id(type_id)
        if not dt:
            return ServiceResponse.not_found("Discount type not found")

        if 'code' in kwargs and kwargs['code']:
            code = kwargs['code'].strip()
            if DiscountTypeRepository.code_exists(code, exclude_id=type_id):
                return ServiceResponse.error("Discount type with this code already exists")
            kwargs['code'] = code

        if 'name' in kwargs and kwargs['name']:
            kwargs['name'] = kwargs['name'].strip()

        if 'discount_method' in kwargs:
            from discounts.models import DiscountType
            valid_methods = [c[0] for c in DiscountType.Method.choices]
            if kwargs['discount_method'] not in valid_methods:
                return ServiceResponse.validation_error(
                    errors={'discount_method': f'Must be one of: {", ".join(valid_methods)}'},
                    message='Invalid discount method',
                )

        allowed_fields = {'name', 'code', 'description', 'discount_method', 'is_active'}
        for key, value in kwargs.items():
            if key in allowed_fields and hasattr(dt, key):
                setattr(dt, key, value)

        dt.save()

        return ServiceResponse.success(
            data={'discount_type': _serialize_discount_type(dt)},
            message="Discount type updated successfully",
        )

    @staticmethod
    @transaction.atomic
    def delete(type_id):
        dt = DiscountTypeRepository.get_by_id(type_id)
        if not dt:
            return ServiceResponse.not_found("Discount type not found")

        discount_count = dt.discounts.filter(is_deleted=False).count()
        if discount_count > 0:
            return ServiceResponse.error(
                f"Cannot delete discount type with {discount_count} active discount(s)"
            )

        dt.is_deleted = True
        dt.save(update_fields=['is_deleted', 'synced_at', 'sync_version'])

        return ServiceResponse.success(message="Discount type deleted successfully")
