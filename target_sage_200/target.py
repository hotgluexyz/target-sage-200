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
    ).to_dict()


if __name__ == "__main__":
    TargetSage200.cli()
