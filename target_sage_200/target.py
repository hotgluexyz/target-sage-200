from singer_sdk import typing as th
from target_hotglue.target import TargetHotglue

from target_sage_200.sinks import (
    CreditNotesSink,
    CustomersSink,
    InvoicesSink,
    ProductCategoriesSink,
    ProductsSink,
    SalesOrdersSink,
)

# Hotglue connect UI often stores typed number fields as strings.
_INTEGER_CONFIG_KEYS = ("company_id", "warehouse_id")


def _coerce_int_config(config: dict) -> None:
    """In-place: coerce stringy integer config values before JSON Schema validation."""
    for key in _INTEGER_CONFIG_KEYS:
        value = config.get(key)
        if not isinstance(value, str):
            continue
        if value.strip() == "":
            config[key] = None
        else:
            config[key] = int(value)


class TargetSage200(TargetHotglue):
    name = "target-sage-200"
    MAX_PARALLELISM = 1
    SINK_TYPES = [
        ProductCategoriesSink,
        ProductsSink,
        CustomersSink,
        SalesOrdersSink,
        InvoicesSink,
        CreditNotesSink,
    ]
    config_jsonschema = th.PropertiesList(
        th.Property("client_id", th.StringType, required=True),
        th.Property("client_secret", th.StringType, required=True),
        th.Property("refresh_token", th.StringType, required=True),
        th.Property("access_token", th.StringType),
        th.Property("site_id", th.StringType, required=True),
        th.Property("company_id", th.IntegerType, required=True),
        th.Property("base_url", th.StringType),
        th.Property("default_nominal_code", th.StringType),
        th.Property(
            "default_product_group",
            th.StringType,
            description=(
                "Existing Sage product group code used when the ETL group cannot "
                "be created (product_groups POST is unavailable on some sites)"
            ),
        ),
        th.Property(
            "warehouse_id",
            th.IntegerType,
            description="Warehouse id for product warehouse_holdings; defaults to first sales warehouse",
        ),
        th.Property(
            "allow_sop_pricing",
            th.BooleanType,
            description=(
                "When true, send selling_unit_price and unit_discount_percent on SOP "
                "order lines. Off by default because many API users lack permission."
            ),
        ),
        th.Property(
            "import_as_sales_orders",
            th.BooleanType,
            description=(
                "Connect UI option. When true, the ETL emits SalesOrders (with product "
                "lines) instead of Invoices. Sales ledger invoices are financial "
                "postings only — Sage has no API for SOP invoices with line items. "
                "Credit notes are unaffected."
            ),
        ),
    ).to_dict()

    def _validate_config(self, raise_errors=True, warnings_as_errors=False):
        _coerce_int_config(self._config)
        return super()._validate_config(
            raise_errors=raise_errors,
            warnings_as_errors=warnings_as_errors,
        )


if __name__ == "__main__":
    TargetSage200.cli()
