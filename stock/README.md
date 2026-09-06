# Stock module

Stock code is organized around the inventory domain. Keep Django model names,
database tables, migrations, and sync payloads stable when moving Python code.

| Folder | Responsibility |
| --- | --- |
| `models/` | Catalog, balances, stock movements, recipes, purchasing, production, and transfers |
| `repositories/` | Scoped queries, relation loading, aggregates, and sync-aware bulk updates |
| `services/` | Transactions, validation, workflows, and response contracts |
| `services/recipes/catalog.py` | Recipe catalog, validation, approval, and version lifecycle |
| `services/recipes/costing.py` | Ingredient requirements, yield, costs, portions, and availability |
| `services/recipes/loading.py` | Live child prefetches and conversion metadata for one read |
| `services/recipes/ingredients.py` | Ingredients and substitutes |
| `services/recipes/byproducts.py` | By-products |
| `services/recipes/steps.py` | Preparation steps |
| `services/conversions.py` | Shared unit arithmetic and bulk conversion metadata |
| `views/` | HTTP parsing and service dispatch |
| `tests/` | Catalog, inventory, purchasing, production, AI, and sync regressions |

Existing imports from `stock.services.recipe_service` remain supported through
a small compatibility module. New recipe implementation belongs in the package.

## Quantity and transaction rules

`StockLevel.quantity`, `reserved_quantity`, and transaction `base_quantity` use
the stock item's base unit. Input quantities can use another unit. Convert them
before comparing with a balance or reserving stock. Keep the corresponding unit
ID consistent when passing a converted quantity onward. Item-specific unit
overrides take precedence over the general unit factor.

Use `Decimal` throughout inventory and cost calculations. Aggregate requirements
for the same item across products and recipe lines before checking availability.
Optional recipe ingredients do not block production of a batch. Location-scoped
reads must not satisfy a shortage with another location's balance.

Reserve all ingredients in one transaction, lock items in stable ID order, and
roll back the whole operation if any reservation fails. Stock ledger writes must
use the maintained services and sync-aware save paths. New recipe versions lock
their family root before allocating the next number; failed child creation must
leave no partial recipe behind.

## Read performance

Prefetch only live children and the stock levels needed for the selected page.
Derive displayed totals from those levels. Batch conversion overrides and stock
availability. Conversion metadata belongs to one read operation; do not put it
in a process-wide cache or reuse loaded recipe objects across requests.

Synthetic regression budgets cover both one-row and twenty-row reads:

| Read | SQL query limit |
| --- | ---: |
| Inventory page with location balances | 3 |
| Recipe availability, including transaction savepoints | 7 |
| Recipe details with ingredients, substitutes, steps, by-products, and cost | 7 |
| Stock insight snapshot with recipes and supplier items | 18 |

These are query-count budgets, not production latency benchmarks. Run the stock
suite against both editions. PostgreSQL tests additionally exercise competing
reservations and concurrent recipe version creation; SQLite cannot verify row
locks. Run the complete payment and synchronization suites before updating an
application's core submodule.
