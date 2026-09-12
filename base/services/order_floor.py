"""Table occupancy follows unpaid active tickets under the table row lock."""
from django.db import transaction

from base.helpers.response import ServiceResponse
from base.models import Order, Place, Table


def live_orders(table_id):
    return Order.objects.filter(table_id=table_id, is_deleted=False, is_paid=False,
                                status__in=['OPEN', 'PREPARING', 'READY'])


def claim_table(*, table_id=None, place_id=None, order_type='HALL', branch_id='', require_table=False):
    if order_type != 'HALL' and (table_id or place_id):
        return None, None, ServiceResponse.validation_error(
            {'table_id': 'Only dine-in orders may use a table or place.'})
    if require_table and order_type == 'HALL' and not table_id:
        return None, None, ServiceResponse.validation_error({'table_id': 'Choose a table.'})
    place = None
    if place_id:
        place = Place.objects.filter(pk=place_id, is_active=True, is_deleted=False).first()
        if not place or (branch_id and place.branch_id not in ('', branch_id)):
            return None, None, ServiceResponse.not_found('Active place not found')
    if not table_id:
        return place, None, None
    table = Table.objects.select_for_update().filter(pk=table_id, is_deleted=False, is_active=True).first()
    if not table or (branch_id and table.branch_id not in ('', branch_id)):
        return None, None, ServiceResponse.not_found('Active table not found')
    table_place = Place.objects.filter(pk=table.place_id, is_deleted=False, is_active=True).first()
    if not table_place or (branch_id and table_place.branch_id not in ('', branch_id)):
        return None, None, ServiceResponse.not_found('Active place not found')
    if place and table.place_id != place.pk:
        return None, None, ServiceResponse.validation_error({'table_id': 'Table belongs to another place.'})
    if table.status != 'AVAILABLE' or live_orders(table.pk).exists():
        return None, None, ({'success': False, 'code': 'TABLE_UNAVAILABLE',
                            'message': 'This table already has an active ticket or is unavailable.'}, 409)
    return table_place, table, None


@transaction.atomic
def reconcile_table(table_id):
    if not table_id:
        return
    table = Table.objects.select_for_update().filter(pk=table_id, is_deleted=False).first()
    if table is None:
        return
    if live_orders(table.pk).exists():
        status = 'OCCUPIED'
    else:
        status = table.status if table.status in ('RESERVED', 'OUT_OF_SERVICE') else 'AVAILABLE'
    if table.status != status:
        table.status = status
        table.save(update_fields=['status'])
