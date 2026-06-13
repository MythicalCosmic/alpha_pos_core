from typing import Dict, Any, Optional, Tuple
from django.db import transaction

from base.helpers.response import ServiceResponse
from stock.models import StockSettings, StockAlertConfig, StockLocation
from stock.repositories import StockSettingsRepository, StockAlertConfigRepository, StockLocationRepository


class StockSettingsService:

    @classmethod
    def load(cls) -> StockSettings:
        return StockSettingsRepository.load()

    @classmethod
    def is_enabled(cls) -> bool:
        """Check if stock system is enabled"""
        return cls.load().stock_enabled

    @classmethod
    def is_production_enabled(cls) -> bool:
        settings = cls.load()
        return settings.stock_enabled and settings.production_enabled

    @classmethod
    def is_purchasing_enabled(cls) -> bool:
        settings = cls.load()
        return settings.stock_enabled and settings.purchasing_enabled

    @classmethod
    def is_multi_location_enabled(cls) -> bool:
        settings = cls.load()
        return settings.stock_enabled and settings.multi_location_enabled

    @classmethod
    def get_all(cls) -> Tuple[Dict[str, Any], int]:
        settings = cls.load()

        return ServiceResponse.success(data={
            "stock_enabled": settings.stock_enabled,
            "production_enabled": settings.production_enabled,
            "purchasing_enabled": settings.purchasing_enabled,
            "multi_location_enabled": settings.multi_location_enabled,

            "track_cost": settings.track_cost,
            "track_batches": settings.track_batches,
            "track_expiry": settings.track_expiry,
            "track_serial_numbers": settings.track_serial_numbers,

            "allow_negative_stock": settings.allow_negative_stock,
            "auto_deduct_on_sale": settings.auto_deduct_on_sale,
            "deduct_on_order_status": settings.deduct_on_order_status,
            "reserve_on_order_create": settings.reserve_on_order_create,
            "auto_create_production": settings.auto_create_production,

            "costing_method": settings.costing_method,
            "include_waste_in_cost": settings.include_waste_in_cost,

            "low_stock_alert_enabled": settings.low_stock_alert_enabled,
            "expiry_alert_enabled": settings.expiry_alert_enabled,
            "expiry_alert_days": settings.expiry_alert_days,
            "negative_stock_alert": settings.negative_stock_alert,

            "default_location_id": settings.default_location_id,
            "default_production_location_id": settings.default_production_location_id,
            "default_receiving_location_id": settings.default_receiving_location_id,

            "require_po_approval": settings.require_po_approval,
            "require_transfer_approval": settings.require_transfer_approval,
            "require_adjustment_approval": settings.require_adjustment_approval,
            "require_count_approval": settings.require_count_approval,
        })

    @classmethod
    def get_status(cls) -> Tuple[Dict[str, Any], int]:
        settings = cls.load()

        return ServiceResponse.success(data={
            "enabled": settings.stock_enabled,
            "modules": {
                "production": settings.production_enabled,
                "purchasing": settings.purchasing_enabled,
                "multi_location": settings.multi_location_enabled,
            },
            "tracking": {
                "cost": settings.track_cost,
                "batches": settings.track_batches,
                "expiry": settings.track_expiry,
            },
            "costing_method": settings.costing_method,
            "allow_negative": settings.allow_negative_stock,
        })

    @classmethod
    @transaction.atomic
    def update(cls, **kwargs) -> Tuple[Dict[str, Any], int]:
        settings = cls.load()
        valid_fields = {
            "stock_enabled", "production_enabled", "purchasing_enabled", "multi_location_enabled",
            "track_cost", "track_batches", "track_expiry", "track_serial_numbers",
            "allow_negative_stock", "auto_deduct_on_sale", "deduct_on_order_status",
            "reserve_on_order_create", "auto_create_production",
            "costing_method", "include_waste_in_cost",
            "low_stock_alert_enabled", "expiry_alert_enabled", "expiry_alert_days",
            "negative_stock_alert",
            "default_location_id", "default_production_location_id", "default_receiving_location_id",
            "require_po_approval", "require_transfer_approval",
            "require_adjustment_approval", "require_count_approval",
        }

        if "costing_method" in kwargs:
            valid_methods = [c[0] for c in StockSettings.CostingMethod.choices]
            if kwargs["costing_method"] not in valid_methods:
                return ServiceResponse.validation_error(
                    errors={"costing_method": f"Invalid costing method. Valid: {valid_methods}"},
                )

        if "deduct_on_order_status" in kwargs:
            valid_statuses = ["CREATED", "PREPARING", "READY", "PAID"]
            if kwargs["deduct_on_order_status"] not in valid_statuses:
                return ServiceResponse.validation_error(
                    errors={"deduct_on_order_status": f"Invalid status. Valid: {valid_statuses}"},
                )

        for loc_field in ["default_location_id", "default_production_location_id", "default_receiving_location_id"]:
            if loc_field in kwargs and kwargs[loc_field]:
                location = StockLocationRepository.get_by_id(kwargs[loc_field])
                if not location or not location.is_active:
                    return ServiceResponse.validation_error(
                        errors={loc_field: "Location not found or inactive"},
                    )

        updated = []
        for field, value in kwargs.items():
            if field in valid_fields:
                setattr(settings, field, value)
                updated.append(field)

        if updated:
            settings.save()

        settings_data, _ = cls.get_all()
        return ServiceResponse.success(
            data={
                "updated_fields": updated,
                "settings": settings_data["data"],
            },
            message=f"Updated {len(updated)} setting(s)",
        )

    @classmethod
    @transaction.atomic
    def toggle_stock(cls, enabled: bool) -> Tuple[Dict[str, Any], int]:
        settings = cls.load()
        settings.stock_enabled = enabled
        settings.save(update_fields=["stock_enabled", "updated_at"])

        return ServiceResponse.success(
            data={"stock_enabled": enabled},
            message=f"Stock system {'enabled' if enabled else 'disabled'}",
        )

    @classmethod
    @transaction.atomic
    def toggle_module(cls, module: str, enabled: bool) -> Tuple[Dict[str, Any], int]:
        settings = cls.load()

        module_fields = {
            "production": "production_enabled",
            "purchasing": "purchasing_enabled",
            "multi_location": "multi_location_enabled",
        }

        if module not in module_fields:
            return ServiceResponse.validation_error(
                errors={"module": f"Invalid module. Valid: {list(module_fields.keys())}"},
            )

        field = module_fields[module]
        setattr(settings, field, enabled)
        settings.save(update_fields=[field, "updated_at"])

        return ServiceResponse.success(
            data={"module": module, "enabled": enabled},
            message=f"{module.replace('_', ' ').title()} module {'enabled' if enabled else 'disabled'}",
        )

    @classmethod
    def get_default_location(cls) -> Optional["StockLocation"]:
        settings = cls.load()
        return settings.default_location

    @classmethod
    def get_default_location_id(cls) -> Optional[int]:
        settings = cls.load()
        return settings.default_location_id

    @classmethod
    def get_production_location(cls) -> Optional["StockLocation"]:
        settings = cls.load()
        return settings.default_production_location

    @classmethod
    def get_receiving_location(cls) -> Optional["StockLocation"]:
        settings = cls.load()
        return settings.default_receiving_location


class AlertConfigService:

    @classmethod
    def get_all(cls) -> Tuple[Dict[str, Any], int]:
        configs = StockAlertConfigRepository.get_all()

        return ServiceResponse.success(data={
            "alerts": [
                {
                    "id": c.id,
                    "uuid": str(c.uuid),
                    "alert_type": c.alert_type,
                    "alert_type_display": c.get_alert_type_display(),
                    "notify_email": c.notify_email,
                    "notify_telegram": c.notify_telegram,
                    "notify_in_app": c.notify_in_app,
                    "threshold_value": str(c.threshold_value) if c.threshold_value else None,
                    "is_active": c.is_active,
                }
                for c in configs
            ],
            "count": configs.count(),
        })

    @classmethod
    def get_by_type(cls, alert_type: str) -> Optional[StockAlertConfig]:
        return StockAlertConfigRepository.get_by_type(alert_type)

    @classmethod
    @transaction.atomic
    def create_or_update(cls, alert_type: str, **kwargs) -> Tuple[Dict[str, Any], int]:
        valid_types = [c[0] for c in StockAlertConfig.AlertType.choices]
        if alert_type not in valid_types:
            return ServiceResponse.validation_error(
                errors={"alert_type": f"Invalid alert type. Valid: {valid_types}"},
            )

        config, created = StockAlertConfig.objects.get_or_create(
            alert_type=alert_type,
            defaults={
                "notify_email": kwargs.get("notify_email", False),
                "notify_telegram": kwargs.get("notify_telegram", True),
                "notify_in_app": kwargs.get("notify_in_app", True),
                "threshold_value": kwargs.get("threshold_value"),
                "is_active": kwargs.get("is_active", True),
            },
        )

        if not created:
            for field in ["notify_email", "notify_telegram", "notify_in_app", "threshold_value", "is_active"]:
                if field in kwargs:
                    setattr(config, field, kwargs[field])
            config.save()

        return ServiceResponse.success(
            data={
                "id": config.id,
                "created": created,
                "alert_type": config.alert_type,
            },
            message=f"Alert config {'created' if created else 'updated'}",
        )

    @classmethod
    def is_alert_enabled(cls, alert_type: str) -> bool:
        config = cls.get_by_type(alert_type)
        return config.is_active if config else False
