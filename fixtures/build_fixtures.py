"""
Builds synthetic .pbit and .pbix fixtures that reproduce the real file
format's awkward parts, so the extractor is tested against the quirks
that actually break naive readers:

  - UTF-16LE encoding with BOM on Layout and DataModelSchema
  - config / query / filters stored as JSON strings inside JSON
  - DataMashup as a binary header followed by an embedded zip
  - a measure named "Net Revenue (base)" — must not be read as a
    REVENUE() function call
  - role-playing dimension: two relationships to DimDate, one inactive
  - a deliberately circular measure pair, to prove cycle detection
  - an implicit aggregation in a visual (raw column, not a measure)
  - a measure referenced by a visual that does not exist in the model
"""
from __future__ import annotations

import json
import os
import zipfile


def _u16(obj) -> bytes:
    """Encode as UTF-16LE with BOM, the way Power BI writes these parts."""
    text = obj if isinstance(obj, str) else json.dumps(obj)
    return b"\xff\xfe" + text.encode("utf-16-le")


# --------------------------------------------------------------------------
# Model (TMSL)
# --------------------------------------------------------------------------

DATA_MODEL_SCHEMA = {
    "name": "SemanticModel",
    "model": {
        "culture": "en-US",
        "tables": [
            {
                "name": "FactSales",
                "columns": [
                    {"name": "SalesKey", "dataType": "int64", "isHidden": True},
                    {"name": "OrderDateKey", "dataType": "dateTime"},
                    {"name": "ShipDateKey", "dataType": "dateTime"},
                    {"name": "CustomerKey", "dataType": "int64", "isHidden": True},
                    {"name": "ProductKey", "dataType": "int64", "isHidden": True},
                    {"name": "Quantity", "dataType": "int64", "summarizeBy": "sum"},
                    {"name": "UnitPrice", "dataType": "decimal", "summarizeBy": "sum"},
                    {"name": "NetAmount", "dataType": "decimal", "summarizeBy": "sum",
                     "formatString": "#,0.00"},
                    {"name": "Cost", "dataType": "decimal", "summarizeBy": "sum"},
                    {"name": "Margin Amount", "dataType": "decimal", "type": "calculated",
                     "expression": "FactSales[NetAmount] - FactSales[Cost]"},
                ],
                "partitions": [{
                    "name": "FactSales-Partition", "mode": "import",
                    "source": {"type": "m", "expression":
                               'let Source = Sql.Database("dw-prod.internal", "SalesDW"), '
                               'dbo_FactSales = Source{[Schema="dbo",Item="FactSales"]}[Data] '
                               'in dbo_FactSales'},
                }],
                "measures": [
                    {"name": "Total Sales", "expression": "SUM(FactSales[NetAmount])",
                     "formatString": "#,0", "displayFolder": "Core"},
                    # Parenthesised name — must NOT parse as REVENUE()
                    {"name": "Net Revenue (base)",
                     "expression": "SUM(FactSales[NetAmount]) - SUM(FactSales[Cost])",
                     "displayFolder": "Core"},
                    {"name": "Total Cost", "expression": "SUM(FactSales[Cost])",
                     "displayFolder": "Core", "isHidden": True},
                    {"name": "Margin %",
                     "expression": "DIVIDE([Total Sales] - [Total Cost], [Total Sales])",
                     "formatString": "0.00%", "displayFolder": "Profitability"},
                    {"name": "Sales YTD",
                     "expression": "TOTALYTD([Total Sales], DimDate[Date])",
                     "displayFolder": "Time Intelligence"},
                    {"name": "Sales PY",
                     "expression": "CALCULATE([Total Sales], SAMEPERIODLASTYEAR(DimDate[Date]))",
                     "displayFolder": "Time Intelligence"},
                    {"name": "YoY Growth %",
                     "expression": "DIVIDE([Total Sales] - [Sales PY], [Sales PY])",
                     "formatString": "0.0%", "displayFolder": "Time Intelligence"},
                    # Role-playing: uses the inactive ShipDate relationship
                    {"name": "Sales by Ship Date",
                     "expression": "CALCULATE([Total Sales], "
                                   "USERELATIONSHIP(FactSales[ShipDateKey], DimDate[Date]))",
                     "displayFolder": "Time Intelligence"},
                    # Complex, with VARs — should be medium confidence
                    {"name": "Top Product Share",
                     "expression": (
                         "VAR TopProduct = TOPN(1, VALUES(DimProduct[ProductName]), [Total Sales])\n"
                         "VAR TopValue = CALCULATE([Total Sales], TopProduct)\n"
                         "VAR AllValue = CALCULATE([Total Sales], ALL(DimProduct))\n"
                         "RETURN DIVIDE(TopValue, AllValue)"),
                     "displayFolder": "Analysis"},
                    {"name": "Product Rank",
                     "expression": "RANKX(ALL(DimProduct[ProductName]), [Total Sales], , DESC)",
                     "displayFolder": "Analysis"},
                    # Never used anywhere — should be flagged as unused
                    {"name": "Legacy Discount Total",
                     "expression": "SUMX(FactSales, FactSales[UnitPrice] * 0.05)",
                     "displayFolder": "Deprecated"},
                    # Deliberate circular pair
                    {"name": "Circular A", "expression": "[Circular B] + 1"},
                    {"name": "Circular B", "expression": "[Circular A] * 2"},
                ],
            },
            {
                "name": "DimDate",
                "columns": [
                    {"name": "Date", "dataType": "dateTime"},
                    {"name": "Year", "dataType": "int64"},
                    {"name": "MonthName", "dataType": "string", "sortByColumn": "MonthNumber"},
                    {"name": "MonthNumber", "dataType": "int64", "isHidden": True},
                    {"name": "Quarter", "dataType": "string"},
                ],
                "partitions": [{"name": "DimDate-P", "mode": "import",
                                "source": {"type": "calculated",
                                           "expression": "CALENDAR(DATE(2020,1,1), DATE(2026,12,31))"}}],
                "hierarchies": [{"name": "Calendar", "levels": [
                    {"name": "Year", "column": "Year", "ordinal": 0},
                    {"name": "Quarter", "column": "Quarter", "ordinal": 1},
                    {"name": "Month", "column": "MonthName", "ordinal": 2}]}],
                "measures": [],
            },
            {
                "name": "DimCustomer",
                "columns": [
                    {"name": "CustomerKey", "dataType": "int64", "isHidden": True},
                    {"name": "CustomerName", "dataType": "string"},
                    {"name": "Segment", "dataType": "string"},
                    {"name": "Region", "dataType": "string"},
                ],
                "partitions": [{"name": "DimCustomer-P", "mode": "import",
                                "source": {"type": "m", "expression":
                                           'let Source = Sql.Database("dw-prod.internal", "SalesDW") '
                                           'in Source'}}],
                "measures": [],
            },
            {
                "name": "DimProduct",
                "columns": [
                    {"name": "ProductKey", "dataType": "int64", "isHidden": True},
                    {"name": "ProductName", "dataType": "string"},
                    {"name": "Category", "dataType": "string"},
                ],
                "partitions": [{"name": "DimProduct-P", "mode": "import",
                                "source": {"type": "m", "expression":
                                           'let Source = Snowflake.Databases("acct.snowflakecomputing.com", "WH") '
                                           'in Source'}}],
                "measures": [],
            },
        ],
        "relationships": [
            {"name": "r1", "fromTable": "FactSales", "fromColumn": "OrderDateKey",
             "toTable": "DimDate", "toColumn": "Date", "isActive": True,
             "fromCardinality": "many", "toCardinality": "one"},
            # Role-playing: second relationship to the same dimension, inactive
            {"name": "r2", "fromTable": "FactSales", "fromColumn": "ShipDateKey",
             "toTable": "DimDate", "toColumn": "Date", "isActive": False,
             "fromCardinality": "many", "toCardinality": "one"},
            {"name": "r3", "fromTable": "FactSales", "fromColumn": "CustomerKey",
             "toTable": "DimCustomer", "toColumn": "CustomerKey", "isActive": True,
             "fromCardinality": "many", "toCardinality": "one"},
            # Bidirectional — should raise a warning
            {"name": "r4", "fromTable": "FactSales", "fromColumn": "ProductKey",
             "toTable": "DimProduct", "toColumn": "ProductKey", "isActive": True,
             "crossFilteringBehavior": "bothDirections",
             "fromCardinality": "many", "toCardinality": "one"},
        ],
        "roles": [{
            "name": "RegionalManager", "modelPermission": "read",
            "tablePermissions": [{
                "name": "DimCustomer",
                "filterExpression": "DimCustomer[Region] = USERPRINCIPALNAME()"}],
        }],
    },
}


# --------------------------------------------------------------------------
# Report layout
# --------------------------------------------------------------------------

def _measure_ref(entity, prop):
    return {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def _column_ref(entity, prop):
    return {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def _visual(vid, vtype, title, x, y, w, h, projections, filters=None):
    config = {
        "name": vid,
        "singleVisual": {
            "visualType": vtype,
            "projections": {"Values": projections},
            "vcObjects": {"title": [{"properties": {"text": {"expr": {
                "Literal": {"Value": f"'{title}'"}}}}}]} if title else {},
        },
    }
    return {
        "x": x, "y": y, "width": w, "height": h, "z": 0,
        "config": json.dumps(config),
        "filters": json.dumps(filters or []),
    }


LAYOUT = {
    "id": 0,
    "config": json.dumps({
        "version": "5.43",
        "themeCollection": {"baseTheme": {"name": "CY24SU06"}},
        "bookmarks": [{"name": "bm1", "displayName": "Default View",
                       "explorationState": {"activeSection": "page1"}}],
    }),
    "filters": json.dumps([{
        "name": "rf1", "type": "Categorical",
        "expression": _column_ref("DimDate", "Year"),
        "filter": {"Where": []},
    }]),
    "sections": [
        {
            "name": "page1", "displayName": "Executive Summary", "ordinal": 0,
            "width": 1280, "height": 720,
            "config": json.dumps({}),
            "filters": json.dumps([]),
            "visualContainers": [
                _visual("v1", "card", "Total Sales", 20, 20, 280, 140,
                        [{"queryRef": "FactSales.Total Sales",
                          "field": _measure_ref("FactSales", "Total Sales")}]),
                _visual("v2", "card", "Margin %", 320, 20, 280, 140,
                        [{"queryRef": "FactSales.Margin %",
                          "field": _measure_ref("FactSales", "Margin %")}]),
                _visual("v3", "lineChart", "Sales Trend", 20, 180, 900, 320,
                        [{"queryRef": "FactSales.Total Sales",
                          "field": _measure_ref("FactSales", "Total Sales")},
                         {"queryRef": "FactSales.Sales YTD",
                          "field": _measure_ref("FactSales", "Sales YTD")},
                         {"queryRef": "DimDate.MonthName",
                          "field": _column_ref("DimDate", "MonthName")}]),
                # Implicit aggregation — raw column summarised in the visual
                _visual("v4", "clusteredBarChart", "Quantity by Category", 20, 520, 900, 180,
                        [{"queryRef": "Sum(FactSales.Quantity)",
                          "field": {"Aggregation": {
                              "Expression": _column_ref("FactSales", "Quantity"),
                              "Function": 0}}},
                         {"queryRef": "DimProduct.Category",
                          "field": _column_ref("DimProduct", "Category")}]),
            ],
        },
        {
            "name": "page2", "displayName": "Product Detail", "ordinal": 1,
            "width": 1280, "height": 720,
            "config": json.dumps({}),
            "filters": json.dumps([{
                "name": "pf1", "type": "Categorical",
                "expression": _column_ref("DimProduct", "Category"),
                "filter": {"Where": []}, "isLockedInViewMode": True,
            }]),
            "visualContainers": [
                _visual("v5", "tableEx", "Product Performance", 20, 20, 1200, 600,
                        [{"queryRef": "DimProduct.ProductName",
                          "field": _column_ref("DimProduct", "ProductName")},
                         {"queryRef": "FactSales.Total Sales",
                          "field": _measure_ref("FactSales", "Total Sales")},
                         {"queryRef": "FactSales.Product Rank",
                          "field": _measure_ref("FactSales", "Product Rank")},
                         {"queryRef": "FactSales.Top Product Share",
                          "field": _measure_ref("FactSales", "Top Product Share")}],
                        filters=[{"name": "vf1", "type": "TopN",
                                  "expression": _column_ref("DimProduct", "ProductName"),
                                  "filter": {"Where": []}}]),
            ],
        },
        {
            # Hidden page referencing a measure that does not exist
            "name": "page3", "displayName": "QA Scratch", "ordinal": 2,
            "width": 1280, "height": 720,
            "config": json.dumps({"visibility": 1}),
            "filters": json.dumps([]),
            "visualContainers": [
                _visual("v6", "card", "Orphan Check", 20, 20, 280, 140,
                        [{"queryRef": "FactSales.Forecast Variance",
                          "field": _measure_ref("FactSales", "Forecast Variance")}]),
            ],
        },
    ],
}


# --------------------------------------------------------------------------
# DataMashup
# --------------------------------------------------------------------------

SECTION_M = '''section Section1;

shared FactSales = let
    Source = Sql.Database("dw-prod.internal", "SalesDW"),
    dbo_FactSales = Source{[Schema="dbo",Item="FactSales"]}[Data],
    #"Removed Columns" = Table.RemoveColumns(dbo_FactSales,{"ETLBatchId"}),
    #"Filtered Rows" = Table.SelectRows(#"Removed Columns", each [OrderDateKey] >= #date(2020,1,1)),
    #"Changed Type" = Table.TransformColumnTypes(#"Filtered Rows",{{"NetAmount", type number}})
in
    #"Changed Type";

shared DimCustomer = let
    Source = Sql.Database("dw-prod.internal", "SalesDW"),
    dbo_DimCustomer = Source{[Schema="dbo",Item="DimCustomer"]}[Data],
    #"Merged Region" = Table.AddColumn(dbo_DimCustomer, "RegionLabel", each [Region] & " Region")
in
    #"Merged Region";

shared DimProduct = let
    Source = Snowflake.Databases("acct.snowflakecomputing.com", "WH_REPORTING"),
    Query = Value.NativeQuery(Source, "SELECT PRODUCT_KEY, PRODUCT_NAME, CATEGORY FROM DIM_PRODUCT WHERE ACTIVE = 1")
in
    Query;
'''


def _build_datamashup() -> bytes:
    """Binary header + embedded zip, mirroring the real part's structure."""
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Formulas/Section1.m", SECTION_M.encode("utf-8"))
        zf.writestr("Config/Package.xml",
                    '<?xml version="1.0"?><Package><Version>2.0</Version></Package>')
    # Real files prefix a version/length header before the zip payload.
    return b"\x00\x00\x00\x00" + len(buf.getvalue()).to_bytes(4, "little") + buf.getvalue()


# --------------------------------------------------------------------------

def build_pbit(path: str):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Version", _u16("1.28"))
        zf.writestr("DataModelSchema", _u16(DATA_MODEL_SCHEMA))
        zf.writestr("Report/Layout", _u16(LAYOUT))
        zf.writestr("DataMashup", _build_datamashup())
        zf.writestr("Connections", json.dumps({"Version": 2, "Connections": []}))
        zf.writestr("Metadata", _u16({"Version": 1}))
        zf.writestr("[Content_Types].xml",
                    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')


def build_pbix(path: str):
    """A .pbix: same report, but the model is an opaque compressed blob."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Version", _u16("1.28"))
        # Stand-in for the Xpress9-compressed SSAS backup.
        zf.writestr("DataModel", b"\xff\xfe\x00\x00XPRESS9-COMPRESSED-BLOB" + os.urandom(512))
        zf.writestr("Report/Layout", _u16(LAYOUT))
        zf.writestr("DataMashup", _build_datamashup())
        zf.writestr("SecurityBindings", os.urandom(64))
        zf.writestr("[Content_Types].xml",
                    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    build_pbit(os.path.join(here, "SalesAnalytics.pbit"))
    build_pbix(os.path.join(here, "SalesAnalytics.pbix"))
    print("Fixtures written to", here)