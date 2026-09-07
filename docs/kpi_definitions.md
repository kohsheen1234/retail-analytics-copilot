# KPI Definitions

## Average Order Value (AOV)
- Current definition (effective 2016-01-01): AOV = SUM(UnitPrice * Quantity * (1 - Discount)) / COUNT(DISTINCT OrderID)
- Legacy definition (retired 2015-12-31): AOV = SUM(UnitPrice * Quantity) / COUNT(DISTINCT OrderID). Use only for historical comparisons when a report explicitly asks for the legacy figure.

## Gross Margin
- GM = SUM((UnitPrice - CostOfGoods) * Quantity * (1 - Discount))
- CostOfGoods is not stored in every system. If it is missing, use a documented approximation and state it in the answer.

## Revenue
- Revenue = SUM(UnitPrice * Quantity * (1 - Discount)) computed from order line items, using the line item unit price at time of sale, not the current catalog price.
