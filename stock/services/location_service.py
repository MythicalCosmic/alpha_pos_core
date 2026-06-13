from typing import Dict, Any, Optional, List, Tuple
from django.db import transaction
from django.db.models import Count, Sum

from base.helpers.response import ServiceResponse
from stock.models import StockLocation, StockLevel, StockSettings
from stock.repositories import StockLocationRepository


class StockLocationService:

    @classmethod
    def serialize(cls, location: StockLocation, include_children: bool = False,
                  include_stats: bool = False) -> Dict[str, Any]:
        data = {
            "id": location.id,
            "uuid": str(location.uuid),
            "name": location.name,
            "type": location.type,
            "type_display": location.get_type_display(),
            "parent_id": location.parent_location_id,
            "is_default": location.is_default,
            "is_production_area": location.is_production_area,
            "is_active": location.is_active,
            "sort_order": location.sort_order,
            "created_at": location.created_at.isoformat(),
        }

        if include_children:
            data["children"] = [
                cls.serialize(child, include_children=False)
                for child in location.children.filter(is_active=True).order_by("sort_order", "name")
            ]

        if include_stats:
            stats = StockLevel.objects.filter(location=location).aggregate(
                total_items=Count("id"),
                total_quantity=Sum("quantity"),
                reserved_quantity=Sum("reserved_quantity"),
            )
            data["stats"] = {
                "item_count": stats["total_items"] or 0,
                "total_quantity": str(stats["total_quantity"] or 0),
                "reserved_quantity": str(stats["reserved_quantity"] or 0),
            }

        return data

    @classmethod
    def list(cls,
             include_inactive: bool = False,
             type_filter: str = None,
             parent_id: int = None,
             production_only: bool = False,
             include_children: bool = False,
             include_stats: bool = False) -> Tuple[Dict[str, Any], int]:

        queryset = StockLocationRepository.get_all()

        if not include_inactive:
            queryset = queryset.filter(is_active=True)

        if type_filter:
            queryset = queryset.filter(type=type_filter)

        if parent_id is not None:
            if parent_id == 0:
                queryset = queryset.filter(parent_location__isnull=True)
            else:
                queryset = queryset.filter(parent_location_id=parent_id)

        if production_only:
            queryset = queryset.filter(is_production_area=True)

        queryset = queryset.order_by("sort_order", "name")

        locations = [
            cls.serialize(loc, include_children=include_children, include_stats=include_stats)
            for loc in queryset
        ]

        return ServiceResponse.success(data={
            "locations": locations,
            "count": len(locations),
            "types": [
                {"value": c[0], "label": c[1]}
                for c in StockLocation.LocationType.choices
            ]
        })

    @classmethod
    def get_tree(cls, include_inactive: bool = False) -> Tuple[Dict[str, Any], int]:
        queryset = StockLocationRepository.get_root_locations()

        if not include_inactive:
            queryset = queryset.filter(is_active=True)

        queryset = queryset.order_by("sort_order", "name")

        tree = [
            cls.serialize(loc, include_children=True)
            for loc in queryset
        ]

        total_count = (
            StockLocationRepository.count(is_active=True)
            if not include_inactive
            else StockLocationRepository.count()
        )

        return ServiceResponse.success(data={
            "tree": tree,
            "total_count": total_count,
        })

    @classmethod
    def search(cls, query: str, limit: int = 20) -> Tuple[Dict[str, Any], int]:
        queryset = StockLocationRepository.get_active()
        queryset = StockLocationRepository.search(queryset, query)
        locations = queryset.order_by("name")[:limit]

        return ServiceResponse.success(data={
            "locations": [cls.serialize(loc) for loc in locations],
            "count": len(locations),
        })

    @classmethod
    def get(cls, location_id: int, include_children: bool = True,
            include_stats: bool = True) -> Tuple[Dict[str, Any], int]:
        location = StockLocationRepository.get_by_id(location_id)
        if not location:
            return ServiceResponse.not_found("Location not found")

        return ServiceResponse.success(data={
            "location": cls.serialize(location, include_children=include_children,
                                      include_stats=include_stats)
        })

    @classmethod
    def get_default(cls) -> Optional[StockLocation]:
        location = StockLocationRepository.get_default()
        if location:
            return location
        return StockLocationRepository.first(is_active=True)

    @classmethod
    def get_production_locations(cls) -> List[StockLocation]:
        return list(StockLocationRepository.get_production_areas())

    @classmethod
    @transaction.atomic
    def create(cls,
               name: str,
               type: str,
               parent_id: int = None,
               is_default: bool = False,
               is_production_area: bool = False,
               sort_order: int = 0) -> Tuple[Dict[str, Any], int]:

        valid_types = [c[0] for c in StockLocation.LocationType.choices]
        if type not in valid_types:
            return ServiceResponse.validation_error(
                errors={"type": f"Invalid type. Valid: {valid_types}"},
            )

        if StockLocationRepository.name_exists(name):
            return ServiceResponse.validation_error(
                errors={"name": f"Location with name '{name}' already exists"},
            )

        parent = None
        if parent_id:
            parent = StockLocationRepository.get_by_id(parent_id)
            if not parent:
                return ServiceResponse.not_found("Parent location not found")
            if not parent.is_active:
                return ServiceResponse.error("Cannot add child to inactive location")

        if is_default:
            StockLocationRepository.clear_default()

        location = StockLocationRepository.create(
            name=name,
            type=type,
            parent_location=parent,
            is_default=is_default,
            is_production_area=is_production_area,
            sort_order=sort_order,
        )

        return ServiceResponse.success(data={
            "id": location.id,
            "uuid": str(location.uuid),
            "location": cls.serialize(location),
        }, message=f"Location '{name}' created")

    @classmethod
    @transaction.atomic
    def update(cls, location_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        location = StockLocationRepository.get_by_id(location_id)
        if not location:
            return ServiceResponse.not_found("Location not found")

        if "type" in kwargs:
            valid_types = [c[0] for c in StockLocation.LocationType.choices]
            if kwargs["type"] not in valid_types:
                return ServiceResponse.validation_error(
                    errors={"type": f"Invalid type. Valid: {valid_types}"},
                )

        if "name" in kwargs and kwargs["name"] != location.name:
            if StockLocationRepository.name_exists(kwargs["name"], exclude_id=location_id):
                return ServiceResponse.validation_error(
                    errors={"name": f"Location with name '{kwargs['name']}' already exists"},
                )

        if "parent_id" in kwargs:
            if kwargs["parent_id"]:
                parent = StockLocationRepository.get_by_id(kwargs["parent_id"])
                if not parent:
                    return ServiceResponse.not_found("Parent location not found")
                if parent.id == location_id:
                    return ServiceResponse.error("Location cannot be its own parent")
                if cls._is_descendant(parent, location):
                    return ServiceResponse.error("Cannot create circular hierarchy")
                location.parent_location = parent
            else:
                location.parent_location = None

        if kwargs.get("is_default"):
            StockLocationRepository.clear_default(exclude_id=location_id)

        update_fields = ["updated_at"]
        for field in ["name", "type", "is_default", "is_production_area", "sort_order"]:
            if field in kwargs:
                setattr(location, field, kwargs[field])
                update_fields.append(field)

        if "parent_id" in kwargs:
            update_fields.append("parent_location")

        location.save(update_fields=update_fields)

        return ServiceResponse.success(data={
            "location": cls.serialize(location),
        }, message="Location updated")

    @classmethod
    def _is_descendant(cls, location: StockLocation, potential_ancestor: StockLocation) -> bool:
        current = location
        while current.parent_location:
            if current.parent_location_id == potential_ancestor.id:
                return True
            current = current.parent_location
        return False

    @classmethod
    @transaction.atomic
    def deactivate(cls, location_id: int) -> Tuple[Dict[str, Any], int]:
        location = StockLocationRepository.get_by_id(location_id)
        if not location:
            return ServiceResponse.not_found("Location not found")

        has_stock = StockLevel.objects.filter(
            location=location,
            quantity__gt=0,
        ).exists()

        if has_stock:
            return ServiceResponse.error("Cannot deactivate location with stock. Transfer stock first.")

        if location.is_default:
            return ServiceResponse.error("Cannot deactivate default location. Set another location as default first.")

        location.is_active = False
        location.save(update_fields=["is_active", "updated_at"])

        children_count = StockLocationRepository.deactivate_children(location)

        return ServiceResponse.success(data={
            "id": location_id,
            "children_deactivated": children_count,
        }, message="Location deactivated")

    @classmethod
    @transaction.atomic
    def activate(cls, location_id: int) -> Tuple[Dict[str, Any], int]:
        location = StockLocationRepository.get_by_id(location_id)
        if not location:
            return ServiceResponse.not_found("Location not found")

        if location.parent_location and not location.parent_location.is_active:
            return ServiceResponse.error("Cannot activate location with inactive parent")

        location.is_active = True
        location.save(update_fields=["is_active", "updated_at"])

        return ServiceResponse.success(data={
            "location": cls.serialize(location),
        }, message="Location activated")

    @classmethod
    @transaction.atomic
    def set_default(cls, location_id: int) -> Tuple[Dict[str, Any], int]:
        location = StockLocationRepository.get_by_id(location_id)
        if not location:
            return ServiceResponse.not_found("Location not found")

        if not location.is_active:
            return ServiceResponse.error("Cannot set inactive location as default")

        StockLocationRepository.clear_default()

        location.is_default = True
        location.save(update_fields=["is_default", "updated_at"])

        settings = StockSettings.load()
        settings.default_location = location
        settings.save(update_fields=["default_location", "updated_at"])

        return ServiceResponse.success(data={
            "location": cls.serialize(location),
        }, message=f"'{location.name}' set as default location")

    @classmethod
    @transaction.atomic
    def reorder(cls, location_ids: List[int]) -> Tuple[Dict[str, Any], int]:
        StockLocationRepository.reorder(location_ids)

        return ServiceResponse.success(data={
            "reordered": len(location_ids),
        }, message="Locations reordered")
