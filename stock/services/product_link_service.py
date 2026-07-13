from typing import Dict, Any, List, Tuple
from decimal import Decimal
from django.db import transaction

from base.helpers.response import ServiceResponse
from stock.models import (
    ProductStockLink, ProductComponentStock
)
from stock.services.base_service import to_decimal
from stock.repositories import (
    ProductStockLinkRepository, ProductComponentStockRepository,
    RecipeRepository,
    StockItemRepository, StockUnitRepository,
    StockSettingsRepository,
)


def _pagination_data(page_obj, paginator):
    return {
        "page": page_obj.number,
        "per_page": paginator.per_page,
        "total": paginator.count,
        "total_pages": paginator.num_pages,
        "has_next": page_obj.has_next(),
        "has_previous": page_obj.has_previous(),
    }


class ProductStockLinkService:

    @classmethod
    def has_cost_definition(cls, link: ProductStockLink) -> bool:
        """Whether a live link has enough stock metadata to calculate COGS."""
        if not link or link.is_deleted or not link.is_active:
            return False
        if link.link_type == ProductStockLink.LinkType.RECIPE:
            return bool(
                link.recipe and not link.recipe.is_deleted
                and link.recipe.is_active and link.recipe.is_active_version
                and not link.recipe.output_item.is_deleted
                and link.recipe.output_item.is_active
                and to_decimal(link.recipe.output_quantity) > 0
                and to_decimal(link.recipe.yield_percentage) > 0
                and link.recipe.ingredients.filter(
                    is_deleted=False,
                    stock_item__is_deleted=False,
                    stock_item__is_active=True,
                ).exists()
            )
        if link.link_type == ProductStockLink.LinkType.DIRECT_ITEM:
            return bool(
                link.stock_item and not link.stock_item.is_deleted
                and link.stock_item.is_active
            )
        if link.link_type == ProductStockLink.LinkType.COMPONENT_BASED:
            return ProductComponentStockRepository.get_defaults(link.id).filter(
                stock_item__is_deleted=False, stock_item__is_active=True,
            ).exists()
        return False

    @classmethod
    def serialize(cls, link: ProductStockLink, include_components: bool = False) -> Dict[str, Any]:
        data = {
            "id": link.id,
            "uuid": str(link.uuid),
            "product_id": link.product_id,

            "link_type": link.link_type,
            "link_type_display": link.get_link_type_display(),

            "recipe_id": link.recipe_id,
            "recipe_name": link.recipe.name if link.recipe else None,

            "stock_item_id": link.stock_item_id,
            "stock_item_name": link.stock_item.name if link.stock_item else None,

            "quantity_per_sale": str(link.quantity_per_sale),
            "unit_id": link.unit_id,
            "unit_short": link.unit.short_name if link.unit else None,

            "deduct_on_status": link.deduct_on_status,
            "deduct_on_status_display": link.get_deduct_on_status_display(),

            "is_active": link.is_active,
            "created_at": link.created_at.isoformat(),
            "updated_at": link.updated_at.isoformat(),
        }

        if include_components and link.link_type == "COMPONENT_BASED":
            data["components"] = [
                ProductComponentService.serialize(comp)
                for comp in link.components.select_related("stock_item", "unit")
            ]

        return data

    @classmethod
    def list(cls,
             page: int = 1,
             per_page: int = 50,
             link_type: str = None,
             active_only: bool = True,
             unlinked_only: bool = False) -> Tuple[Dict[str, Any], int]:
        queryset = ProductStockLinkRepository.get_all().select_related(
            "recipe", "stock_item", "unit"
        )

        if active_only:
            queryset = queryset.filter(is_active=True)

        if link_type:
            queryset = queryset.filter(link_type=link_type)

        queryset = queryset.order_by("product_id")

        page_obj, paginator = ProductStockLinkRepository.paginate(queryset, page, per_page)

        return ServiceResponse.success(data={
            "links": [cls.serialize(link) for link in page_obj],
            "pagination": _pagination_data(page_obj, paginator),
            "link_types": [{"value": c[0], "label": c[1]} for c in ProductStockLink.LinkType.choices],
            "deduct_statuses": [{"value": c[0], "label": c[1]} for c in ProductStockLink.DeductOn.choices],
        })

    @classmethod
    def get(cls, link_id: int, include_components: bool = True) -> Tuple[Dict[str, Any], int]:
        link = ProductStockLinkRepository.get_with_components(link_id)
        if not link:
            return ServiceResponse.not_found(f"Product link with id {link_id} not found")

        return ServiceResponse.success(data={
            "link": cls.serialize(link, include_components=include_components)
        })

    @classmethod
    def get_by_product(cls, product_id: int) -> Tuple[Dict[str, Any], int]:
        link = ProductStockLinkRepository.get_for_product(product_id)

        if not link:
            return ServiceResponse.success(data={
                "link": None,
                "is_linked": False
            })

        return ServiceResponse.success(data={
            "link": cls.serialize(link, include_components=True),
            "is_linked": True
        })

    @classmethod
    @transaction.atomic
    def link_to_recipe(cls,
                       product_id: int,
                       recipe_id: int,
                       deduct_on_status: str = "PREPARING") -> Tuple[Dict[str, Any], int]:

        if ProductStockLinkRepository.product_has_link(product_id):
            return ServiceResponse.error("Product already has a stock link. Remove existing link first.")

        recipe = RecipeRepository.get_by_id(recipe_id)
        if not recipe:
            return ServiceResponse.not_found(f"Recipe with id {recipe_id} not found")
        if not recipe.is_active:
            return ServiceResponse.error("Recipe is not active")

        valid_statuses = [c[0] for c in ProductStockLink.DeductOn.choices]
        if deduct_on_status not in valid_statuses:
            return ServiceResponse.validation_error(
                errors={"deduct_on_status": f"Invalid status. Valid: {valid_statuses}"}
            )

        link = ProductStockLinkRepository.create(
            product_id=product_id,
            link_type=ProductStockLink.LinkType.RECIPE,
            recipe=recipe,
            quantity_per_sale=Decimal("1"),
            unit=recipe.output_unit,
            deduct_on_status=deduct_on_status,
        )

        return ServiceResponse.created(data={
            "id": link.id,
            "link": cls.serialize(link)
        }, message=f"Product linked to recipe '{recipe.name}'")

    @classmethod
    @transaction.atomic
    def link_to_item(cls,
                     product_id: int,
                     stock_item_id: int,
                     quantity_per_sale: Decimal = Decimal("1"),
                     unit_id: int = None,
                     deduct_on_status: str = "PREPARING") -> Tuple[Dict[str, Any], int]:

        if ProductStockLinkRepository.product_has_link(product_id):
            return ServiceResponse.error("Product already has a stock link. Remove existing link first.")

        stock_item = StockItemRepository.get_by_id(stock_item_id)
        if not stock_item:
            return ServiceResponse.not_found(f"Stock item with id {stock_item_id} not found")
        if not stock_item.is_active:
            return ServiceResponse.error("Stock item is not active")

        if unit_id:
            unit = StockUnitRepository.get_by_id(unit_id)
            if not unit:
                return ServiceResponse.not_found(f"Unit with id {unit_id} not found")
            if not unit.is_active:
                return ServiceResponse.error("Unit is not active")
        else:
            unit = stock_item.base_unit

        valid_statuses = [c[0] for c in ProductStockLink.DeductOn.choices]
        if deduct_on_status not in valid_statuses:
            return ServiceResponse.validation_error(
                errors={"deduct_on_status": f"Invalid status. Valid: {valid_statuses}"}
            )

        link = ProductStockLinkRepository.create(
            product_id=product_id,
            link_type=ProductStockLink.LinkType.DIRECT_ITEM,
            stock_item=stock_item,
            quantity_per_sale=to_decimal(quantity_per_sale),
            unit=unit,
            deduct_on_status=deduct_on_status,
        )

        return ServiceResponse.created(data={
            "id": link.id,
            "link": cls.serialize(link)
        }, message=f"Product linked to stock item '{stock_item.name}'")

    @classmethod
    @transaction.atomic
    def link_with_components(cls,
                             product_id: int,
                             components: List[Dict],
                             deduct_on_status: str = "PREPARING") -> Tuple[Dict[str, Any], int]:

        if ProductStockLinkRepository.product_has_link(product_id):
            return ServiceResponse.error("Product already has a stock link. Remove existing link first.")

        if not components:
            return ServiceResponse.validation_error(
                errors={"components": "At least one component required"}
            )

        valid_statuses = [c[0] for c in ProductStockLink.DeductOn.choices]
        if deduct_on_status not in valid_statuses:
            return ServiceResponse.validation_error(
                errors={"deduct_on_status": f"Invalid status. Valid: {valid_statuses}"}
            )

        link = ProductStockLinkRepository.create(
            product_id=product_id,
            link_type=ProductStockLink.LinkType.COMPONENT_BASED,
            quantity_per_sale=Decimal("1"),
            deduct_on_status=deduct_on_status,
        )

        for comp_data in components:
            result, status = ProductComponentService.add_component(
                link_id=link.id,
                stock_item_id=comp_data["stock_item_id"],
                quantity=comp_data["quantity"],
                component_name=comp_data.get("name", ""),
                unit_id=comp_data.get("unit_id"),
                is_default=comp_data.get("is_default", True),
                is_addable=comp_data.get("is_addable", True),
                is_removable=comp_data.get("is_removable", True),
                price_modifier=comp_data.get("price_modifier", 0),
            )
            if status >= 400:
                return result, status

        return ServiceResponse.created(data={
            "id": link.id,
            "link": cls.serialize(link, include_components=True)
        }, message="Product linked with components")

    @classmethod
    @transaction.atomic
    def update(cls, link_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        link = ProductStockLinkRepository.get_by_id(link_id)
        if not link:
            return ServiceResponse.not_found(f"Product link with id {link_id} not found")

        update_fields = ["updated_at"]

        if "quantity_per_sale" in kwargs:
            link.quantity_per_sale = to_decimal(kwargs["quantity_per_sale"])
            update_fields.append("quantity_per_sale")

        if "deduct_on_status" in kwargs:
            valid_statuses = [c[0] for c in ProductStockLink.DeductOn.choices]
            if kwargs["deduct_on_status"] not in valid_statuses:
                return ServiceResponse.validation_error(
                    errors={"deduct_on_status": f"Invalid status. Valid: {valid_statuses}"}
                )
            link.deduct_on_status = kwargs["deduct_on_status"]
            update_fields.append("deduct_on_status")

        if "is_active" in kwargs:
            link.is_active = kwargs["is_active"]
            update_fields.append("is_active")

        link.save(update_fields=update_fields)

        return ServiceResponse.success(data={
            "link": cls.serialize(link, include_components=True)
        }, message="Link updated")

    @classmethod
    @transaction.atomic
    def unlink(cls, product_id: int) -> Tuple[Dict[str, Any], int]:
        link = ProductStockLinkRepository.get_for_product(product_id)

        if not link:
            return ServiceResponse.not_found(f"Product link for product with id {product_id} not found")

        link.delete()

        return ServiceResponse.success(message="Product unlinked from stock")

    @classmethod
    def calculate_unit_cost(cls, link: ProductStockLink) -> Decimal:
        """Canonical stock COGS for one POS sale represented by ``link``.

        All item quantities are converted to the item's base unit before applying
        ``avg_cost_price``. Recipe links additionally account for full-batch
        output, yield, waste, and ``quantity_per_sale``. Repository access keeps
        soft-deleted components/ingredients out of the result.
        """
        if not cls.has_cost_definition(link):
            return Decimal('0')

        from stock.services.recipe_service import RecipeService
        from stock.services.unit_service import StockItemUnitService

        if link.link_type == ProductStockLink.LinkType.RECIPE:
            return RecipeService.calculate_portion_cost(
                link.recipe,
                quantity=link.quantity_per_sale,
                unit_id=link.unit_id or link.recipe.output_unit_id,
            )

        if link.link_type == ProductStockLink.LinkType.DIRECT_ITEM:
            base_qty = StockItemUnitService.convert_for_item(
                link.stock_item_id,
                link.quantity_per_sale,
                link.unit_id or link.stock_item.base_unit_id,
            )
            return base_qty * to_decimal(link.stock_item.avg_cost_price)

        if link.link_type == ProductStockLink.LinkType.COMPONENT_BASED:
            total = Decimal('0')
            for comp in ProductComponentStockRepository.get_defaults(link.id):
                if comp.stock_item.is_deleted or not comp.stock_item.is_active:
                    continue
                base_qty = StockItemUnitService.convert_for_item(
                    comp.stock_item_id, comp.quantity, comp.unit_id,
                )
                total += base_qty * to_decimal(comp.stock_item.avg_cost_price)
            return total

        return Decimal('0')

    @classmethod
    def get_deduction_items(cls, product_id: int, quantity: int = 1) -> List[Dict]:
        link = ProductStockLinkRepository.get_active_for_product(product_id)

        if not cls.has_cost_definition(link):
            return []

        deductions = []
        sale_qty = to_decimal(quantity)

        if link.link_type == "DIRECT_ITEM":
            if link.stock_item:
                deductions.append({
                    "stock_item_id": link.stock_item_id,
                    "quantity": link.quantity_per_sale * sale_qty,
                    "unit_id": link.unit_id,
                })

        elif link.link_type == "RECIPE":
            if link.recipe:
                from stock.services.recipe_service import RecipeService

                portion_ratio = RecipeService.output_portion_ratio(
                    link.recipe,
                    quantity=link.quantity_per_sale * sale_qty,
                    unit_id=link.unit_id or link.recipe.output_unit_id,
                )
                for row in RecipeService.ingredient_cost_breakdown(link.recipe):
                    ingredient = row['ingredient']
                    deductions.append({
                        "stock_item_id": ingredient.stock_item_id,
                        "quantity": row['quantity_with_waste'] * portion_ratio,
                        "unit_id": ingredient.unit_id,
                    })

        elif link.link_type == "COMPONENT_BASED":
            defaults = ProductComponentStockRepository.get_defaults(link.id).filter(
                stock_item__is_deleted=False, stock_item__is_active=True,
            )
            for comp in defaults:
                deductions.append({
                    "stock_item_id": comp.stock_item_id,
                    "quantity": comp.quantity * sale_qty,
                    "unit_id": comp.unit_id,
                })

        return deductions

    @classmethod
    def should_deduct(cls, product_id: int, order_status: str) -> bool:
        settings = StockSettingsRepository.load()

        if not settings.stock_enabled or not settings.auto_deduct_on_sale:
            return False

        link = ProductStockLinkRepository.get_active_for_product(product_id)

        if not link:
            return False

        return link.deduct_on_status == order_status


class ProductComponentService:

    @classmethod
    def serialize(cls, comp: ProductComponentStock) -> Dict[str, Any]:
        return {
            "id": comp.id,
            "uuid": str(comp.uuid),
            "component_name": comp.component_name,
            "stock_item_id": comp.stock_item_id,
            "stock_item_name": comp.stock_item.name,
            "quantity": str(comp.quantity),
            "unit_id": comp.unit_id,
            "unit_short": comp.unit.short_name,
            "is_default": comp.is_default,
            "is_addable": comp.is_addable,
            "is_removable": comp.is_removable,
            "price_modifier": str(comp.price_modifier),
        }

    @classmethod
    @transaction.atomic
    def add_component(cls,
                      link_id: int,
                      stock_item_id: int,
                      quantity: Decimal,
                      component_name: str = "",
                      unit_id: int = None,
                      is_default: bool = True,
                      is_addable: bool = True,
                      is_removable: bool = True,
                      price_modifier: Decimal = Decimal("0")) -> Tuple[Dict[str, Any], int]:

        link = ProductStockLinkRepository.get_by_id(link_id)
        if not link:
            return ServiceResponse.not_found(f"Product link with id {link_id} not found")

        if link.link_type != "COMPONENT_BASED":
            return ServiceResponse.error("Can only add components to COMPONENT_BASED links")

        stock_item = StockItemRepository.get_by_id(stock_item_id)
        if not stock_item:
            return ServiceResponse.not_found(f"Stock item with id {stock_item_id} not found")
        if not stock_item.is_active:
            return ServiceResponse.error("Stock item is not active")

        if unit_id:
            unit = StockUnitRepository.get_by_id(unit_id)
            if not unit:
                return ServiceResponse.not_found(f"Unit with id {unit_id} not found")
            if not unit.is_active:
                return ServiceResponse.error("Unit is not active")
        else:
            unit = stock_item.base_unit

        comp = ProductComponentStockRepository.create(
            product_stock_link=link,
            component_name=component_name or stock_item.name,
            stock_item=stock_item,
            quantity=to_decimal(quantity),
            unit=unit,
            is_default=is_default,
            is_addable=is_addable,
            is_removable=is_removable,
            price_modifier=to_decimal(price_modifier),
        )

        return ServiceResponse.created(data={
            "id": comp.id,
            "component": cls.serialize(comp)
        }, message="Component added")

    @classmethod
    @transaction.atomic
    def update_component(cls, component_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        comp = ProductComponentStockRepository.get_by_id(component_id)
        if not comp:
            return ServiceResponse.not_found(f"Component with id {component_id} not found")

        for field in ["component_name", "quantity", "is_default", "is_addable", "is_removable", "price_modifier"]:
            if field in kwargs:
                value = kwargs[field]
                if field in ["quantity", "price_modifier"]:
                    value = to_decimal(value)
                setattr(comp, field, value)

        comp.save()

        return ServiceResponse.success(data={
            "component": cls.serialize(comp)
        }, message="Component updated")

    @classmethod
    @transaction.atomic
    def remove_component(cls, component_id: int) -> Tuple[Dict[str, Any], int]:
        comp = ProductComponentStockRepository.get_by_id(component_id)
        if not comp:
            return ServiceResponse.not_found(f"Component with id {component_id} not found")

        comp.delete()

        return ServiceResponse.success(message="Component removed")

    @classmethod
    def get_for_link(cls, link_id: int) -> Tuple[Dict[str, Any], int]:
        components = ProductComponentStockRepository.get_for_link(link_id)

        return ServiceResponse.success(data={
            "components": [cls.serialize(c) for c in components],
            "count": components.count()
        })
