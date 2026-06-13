from typing import Dict, Any, List, Tuple
from django.db import transaction

from base.helpers.response import ServiceResponse
from stock.models import StockCategory
from stock.repositories import StockCategoryRepository


class StockCategoryService:

    @classmethod
    def serialize(cls, category: StockCategory,
                  include_children: bool = False,
                  include_item_count: bool = False) -> Dict[str, Any]:
        data = {
            "id": category.id,
            "uuid": str(category.uuid),
            "name": category.name,
            "type": category.type,
            "type_display": category.get_type_display(),
            "parent_id": category.parent_id,
            "sort_order": category.sort_order,
            "is_active": category.is_active,
            "created_at": category.created_at.isoformat(),
        }

        if category.parent:
            data["parent"] = {
                "id": category.parent.id,
                "name": category.parent.name,
            }

        if include_children:
            children = StockCategoryRepository.get_children(category.id)
            data["children"] = [
                cls.serialize(child, include_children=False)
                for child in children.filter(is_active=True)
            ]

        if include_item_count:
            from stock.repositories import StockItemRepository
            data["item_count"] = StockItemRepository.count(
                category=category, is_active=True
            )

        return data


    @classmethod
    def list(cls,
             include_inactive: bool = False,
             type_filter: str = None,
             parent_id: int = None,
             include_item_count: bool = False) -> Tuple[Dict[str, Any], int]:
        if include_inactive:
            queryset = StockCategoryRepository.get_all()
        else:
            queryset = StockCategoryRepository.get_active()

        if type_filter:
            queryset = queryset.filter(type=type_filter)

        if parent_id is not None:
            if parent_id == 0:
                queryset = queryset.filter(parent__isnull=True)
            else:
                queryset = queryset.filter(parent_id=parent_id)

        queryset = queryset.order_by("sort_order", "name")

        categories = [
            cls.serialize(cat, include_item_count=include_item_count)
            for cat in queryset
        ]

        return ServiceResponse.success(data={
            "categories": categories,
            "count": len(categories),
            "types": [
                {"value": c[0], "label": c[1]}
                for c in StockCategory.CategoryType.choices
            ]
        })

    @classmethod
    def get_tree(cls, include_inactive: bool = False) -> Tuple[Dict[str, Any], int]:
        if include_inactive:
            queryset = StockCategoryRepository.get_all().filter(parent__isnull=True)
        else:
            queryset = StockCategoryRepository.get_root_categories().filter(is_active=True)

        queryset = queryset.order_by("sort_order", "name")

        tree = [
            cls.serialize(cat, include_children=True, include_item_count=True)
            for cat in queryset
        ]

        if include_inactive:
            total_count = StockCategoryRepository.count()
        else:
            total_count = StockCategoryRepository.count(is_active=True)

        return ServiceResponse.success(data={
            "tree": tree,
            "total_count": total_count,
        })

    @classmethod
    def search(cls, query: str, limit: int = 20) -> Tuple[Dict[str, Any], int]:
        queryset = StockCategoryRepository.get_active()
        categories = StockCategoryRepository.search(queryset, query).order_by("name")[:limit]

        return ServiceResponse.success(data={
            "categories": [cls.serialize(cat) for cat in categories],
            "count": len(categories),
        })

    @classmethod
    def get_by_type(cls, category_type: str) -> Tuple[Dict[str, Any], int]:
        valid_types = [c[0] for c in StockCategory.CategoryType.choices]
        if category_type not in valid_types:
            return ServiceResponse.validation_error(
                errors={"type": f"Invalid type. Valid: {valid_types}"},
            )

        categories = StockCategoryRepository.get_by_type(category_type)

        return ServiceResponse.success(data={
            "categories": [cls.serialize(cat, include_item_count=True) for cat in categories],
            "count": categories.count(),
        })


    @classmethod
    def get(cls, category_id: int,
            include_children: bool = True,
            include_item_count: bool = True) -> Tuple[Dict[str, Any], int]:
        category = StockCategoryRepository.get_by_id(category_id)
        if not category:
            return ServiceResponse.not_found(f"Category with id {category_id} not found")

        return ServiceResponse.success(data={
            "category": cls.serialize(
                category,
                include_children=include_children,
                include_item_count=include_item_count,
            )
        })


    @classmethod
    @transaction.atomic
    def create(cls,
               name: str,
               type: str,
               parent_id: int = None,
               sort_order: int = 0) -> Tuple[Dict[str, Any], int]:

        valid_types = [c[0] for c in StockCategory.CategoryType.choices]
        if type not in valid_types:
            return ServiceResponse.validation_error(
                errors={"type": f"Invalid type. Valid: {valid_types}"},
            )

        if parent_id:
            if StockCategoryRepository.name_exists(name, parent_id=parent_id):
                return ServiceResponse.validation_error(
                    errors={"name": f"Category '{name}' already exists at this level"},
                )
        else:
            if StockCategoryRepository.exists(name__iexact=name, parent__isnull=True):
                return ServiceResponse.validation_error(
                    errors={"name": f"Category '{name}' already exists at this level"},
                )

        parent = None
        if parent_id:
            parent = StockCategoryRepository.get_by_id(parent_id)
            if not parent:
                return ServiceResponse.not_found(f"Parent category with id {parent_id} not found")
            if not parent.is_active:
                return ServiceResponse.error("Cannot add child to inactive category")

        category = StockCategoryRepository.create(
            name=name,
            type=type,
            parent=parent,
            sort_order=sort_order,
        )

        return ServiceResponse.created(data={
            "id": category.id,
            "uuid": str(category.uuid),
            "category": cls.serialize(category),
        }, message=f"Category '{name}' created")


    @classmethod
    @transaction.atomic
    def update(cls, category_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        """Update category"""
        category = StockCategoryRepository.get_by_id(category_id)
        if not category:
            return ServiceResponse.not_found(f"Category with id {category_id} not found")

        if "type" in kwargs:
            valid_types = [c[0] for c in StockCategory.CategoryType.choices]
            if kwargs["type"] not in valid_types:
                return ServiceResponse.validation_error(
                    errors={"type": f"Invalid type. Valid: {valid_types}"},
                )

        # Check name uniqueness
        if "name" in kwargs and kwargs["name"] != category.name:
            new_parent_id = kwargs.get("parent_id", category.parent_id)
            if new_parent_id:
                if StockCategoryRepository.name_exists(kwargs["name"], parent_id=new_parent_id, exclude_id=category_id):
                    return ServiceResponse.validation_error(
                        errors={"name": f"Category '{kwargs['name']}' already exists at this level"},
                    )
            else:
                if StockCategoryRepository.filter(name__iexact=kwargs["name"], parent__isnull=True).exclude(id=category_id).exists():
                    return ServiceResponse.validation_error(
                        errors={"name": f"Category '{kwargs['name']}' already exists at this level"},
                    )

        if "parent_id" in kwargs:
            if kwargs["parent_id"]:
                parent = StockCategoryRepository.get_by_id(kwargs["parent_id"])
                if not parent:
                    return ServiceResponse.not_found(f"Parent category with id {kwargs['parent_id']} not found")
                if parent.id == category_id:
                    return ServiceResponse.error("Category cannot be its own parent")
                if cls._is_descendant(parent, category):
                    return ServiceResponse.error("Cannot create circular hierarchy")
                category.parent = parent
            else:
                category.parent = None

        update_fields = ["updated_at"]
        for field in ["name", "type", "sort_order"]:
            if field in kwargs:
                setattr(category, field, kwargs[field])
                update_fields.append(field)

        if "parent_id" in kwargs:
            update_fields.append("parent")

        category.save(update_fields=update_fields)

        return ServiceResponse.success(data={
            "category": cls.serialize(category),
        }, message="Category updated")

    @classmethod
    def _is_descendant(cls, category: StockCategory, potential_ancestor: StockCategory) -> bool:
        current = category
        while current.parent:
            if current.parent_id == potential_ancestor.id:
                return True
            current = current.parent
        return False


    @classmethod
    @transaction.atomic
    def deactivate(cls, category_id: int, cascade: bool = False) -> Tuple[Dict[str, Any], int]:
        category = StockCategoryRepository.get_by_id(category_id)
        if not category:
            return ServiceResponse.not_found(f"Category with id {category_id} not found")

        if StockCategoryRepository.has_active_items(category):
            if not cascade:
                return ServiceResponse.error(
                    "Cannot deactivate category with active items. Use cascade=True or reassign items first."
                )
            StockCategoryRepository.clear_items_category(category)

        if StockCategoryRepository.has_active_children(category):
            if not cascade:
                return ServiceResponse.error(
                    "Cannot deactivate category with active children. Use cascade=True."
                )
            for child in StockCategoryRepository.get_children(category.id).filter(is_active=True):
                cls.deactivate(child.id, cascade=True)

        category.is_active = False
        category.save(update_fields=["is_active", "updated_at"])

        return ServiceResponse.success(data={
            "id": category_id,
        }, message="Category deactivated")

    @classmethod
    @transaction.atomic
    def activate(cls, category_id: int) -> Tuple[Dict[str, Any], int]:
        category = StockCategoryRepository.get_by_id(category_id)
        if not category:
            return ServiceResponse.not_found(f"Category with id {category_id} not found")

        if category.parent and not category.parent.is_active:
            return ServiceResponse.error("Cannot activate category with inactive parent")

        category.is_active = True
        category.save(update_fields=["is_active", "updated_at"])

        return ServiceResponse.success(data={
            "category": cls.serialize(category),
        }, message="Category activated")


    @classmethod
    @transaction.atomic
    def reorder(cls, category_ids: List[int]) -> Tuple[Dict[str, Any], int]:
        StockCategoryRepository.reorder(category_ids)

        return ServiceResponse.success(data={
            "reordered": len(category_ids),
        }, message="Categories reordered")


    @classmethod
    @transaction.atomic
    def move(cls, category_id: int, new_parent_id: int = None) -> Tuple[Dict[str, Any], int]:
        return cls.update(category_id, parent_id=new_parent_id)
