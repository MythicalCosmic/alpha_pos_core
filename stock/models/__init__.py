"""stock app models — split across themed submodules.

External code keeps importing from `stock.models` directly; this
__init__ re-exports every model so the public surface is unchanged.
The split is purely for human readability (T5).
"""
from stock.models.catalog import StockLocation, StockUnit, StockCategory
from stock.models.items import StockItem, StockItemUnit
from stock.models.recipes import Recipe, RecipeIngredient, RecipeIngredientSubstitute, RecipeByProduct, RecipeStep
from stock.models.product_links import ProductStockLink, ProductComponentStock
from stock.models.suppliers import Supplier, SupplierStockItem, SupplierTransaction
from stock.models.purchases import PurchaseOrder, PurchaseOrderItem, PurchaseReceiving, PurchaseReceivingItem
from stock.models.inventory import StockLevel, StockBatch, StockTransaction
from stock.models.production import ProductionOrder, ProductionOrderIngredient, ProductionOrderOutput, ProductionOrderStep
from stock.models.transfers import StockTransfer, StockTransferItem
from stock.models.counts import VarianceReasonCode, StockCount, StockCountItem
from stock.models.settings import StockSettings, StockAlertConfig
from stock.models.ai_chat import AIChat, AIMessage
from stock.models.ai_briefing import AIBriefing
from stock.models.ai_anomaly import Anomaly, AnomalySettings

__all__ = [
    'StockLocation',
    'StockUnit',
    'StockCategory',
    'StockItem',
    'StockItemUnit',
    'Recipe',
    'RecipeIngredient',
    'RecipeIngredientSubstitute',
    'RecipeByProduct',
    'RecipeStep',
    'ProductStockLink',
    'ProductComponentStock',
    'Supplier',
    'SupplierStockItem',
    'SupplierTransaction',
    'PurchaseOrder',
    'PurchaseOrderItem',
    'PurchaseReceiving',
    'PurchaseReceivingItem',
    'StockLevel',
    'StockBatch',
    'StockTransaction',
    'ProductionOrder',
    'ProductionOrderIngredient',
    'ProductionOrderOutput',
    'ProductionOrderStep',
    'StockTransfer',
    'StockTransferItem',
    'VarianceReasonCode',
    'StockCount',
    'StockCountItem',
    'StockSettings',
    'StockAlertConfig',
    'AIChat',
    'AIMessage',
    'AIBriefing',
    'Anomaly',
    'AnomalySettings',
    'AIMessage',
]
