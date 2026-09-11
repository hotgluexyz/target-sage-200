# target-sage-200

A Singer target that loads customers, products and sales documents into
[Sage 200](https://developer.columbus.sage.com/docs) (Standard/Professional, UK),
plus the ETL script that turns Fresho order XML into the records it expects.

Built with the [Meltano Singer SDK](https://sdk.meltano.com) on top of
`target-hotglue`.

## How it fits together

The repo holds two halves of one pipeline:

```
Fresho customer_orders XML
        |
        |  etl/etl.py            denormalised order  ->  per-entity JSON
        v
Customers.json  Products.json  ProductCategories.json
        + one of Invoices.json / SalesOrders.json / CreditNotes.json / SalesReturns.json
        |
        |  target_sage_200/      Singer records  ->  Sage 200 REST calls
        v
    Sage 200
```

### 1. The ETL script (`etl/etl.py`)

Fresho sends one XML file per order, and each file is a single denormalised blob:
the customer, the products, the document header and the purchased lines all hang
off the same `customer_order` element. The script splits that blob into the flat
entity lists Sage models separately.

Every field copy goes through one helper, `set_val`, which coerces the type and
truncates to Sage's column width. Because Sage rejects both over-long strings and
nulls on optional fields, centralising it keeps each `build_*` function a plain
list of source-to-target field pairs.

Only two values are derived rather than copied:

- **Document type.** Fresho expresses a return as a negative quantity. An order
  whose first line is negative becomes a `CreditNote`, or a `SalesReturn` when
  `import_credits_as_sales_returns` is set; anything else becomes an `Invoice`, or
  a `SalesOrder` when `import_as_sales_orders` is set. The two options are
  independent. Quantities and tax are then made absolute, since Sage carries
  direction in the document type rather than the sign.
- **Line tax codes.** Sage requires a tax code per line and Fresho does not send
  one, so zero-tax lines get code `0` and everything else gets `1`.

One run covers many order files, which usually repeat the same customer and
products, so records are consolidated and deduplicated on their natural keys
(`reference` for customers, `code` for products and categories, `order_number` for
documents) before being written.

Paths come from the environment: `base_input_dir` (default `./sync-output`),
`output_dir` (default `./etl-output`) and `config_json` for the flow config.

### 2. The target (`target_sage_200/`)

| File | Role |
| --- | --- |
| `target.py` | Registers the sinks and declares the config schema. `MAX_PARALLELISM = 1`, since later streams depend on ids created by earlier ones. |
| `client.py` | `Sage200Sink` base: auth wiring, the `X-Site`/`X-Company` headers Sage scopes every call by, OData lookups, and the shared upsert. |
| `sinks.py` | One sink per entity, each mapping a record to its Sage payload. |
| `auth.py` | OAuth refresh against `id.sage.com`, writing the rotated token back to the config file. |

Sinks are processed in the order listed in `SINK_TYPES` so that dependencies exist
before they are referenced: product categories, then products, then customers, then
documents.

Sage has no upsert endpoint, so `upsert_by_field` looks the record up by its natural
key with an OData `$filter` and `POST`s a new one when there is no match. A match is
left untouched by default — customer names, VAT numbers and payment terms are
usually maintained in Sage, and the source feed should not overwrite them, so it is
only allowed to seed customers and products that do not exist yet. Set
`update_existing_records` to `true` to `PUT` scalar fields onto matched records
instead; the natural key and any nested collections are stripped from that body,
since Sage treats references and codes as immutable and needs child ids to update
contacts or warehouse holdings. Records matched and skipped are reported under the
`existing` summary counter rather than `updated`.

Lookups and the tax-code table are cached at class level, so a run resolves each
customer, product and tax code once no matter how many sinks need it.

Documents behave differently from the master data:

- **Sales orders and sales returns** (`/sop_orders`, `/sop_returns`) share
  `SopDocumentSink` and post real product lines, each resolved to a `product_id`.
  Prefer these when line-item detail must appear in Sage. Both endpoints take an
  identical body, so a return sends the same positive quantities as an order and
  only the endpoint differs. Fresho order number (`order_number`) is sent as
  `analysis_code_1` and PO (`ref`) as `analysis_code_2` (and also as
  `customer_document_no`). In Sage, rename SOP analysis codes 1 and 2 to
  **F number** and **PO number** and enable free text so those labels appear on
  the document (Accounting System Manager → Maintain Analysis Codes; enable
  amendment on SOP Settings → Invoice and Order Entry). These do not populate
  Transaction Enquiry Reference / 2nd Ref.
- **Invoices and credit notes** (`/sales_invoices`, `/sales_credit_notes`) share
  `LedgerDocumentSink`. Sage's sales ledger endpoint posts a financial summary
  (goods/tax totals) to the customer account — not a printable SOP invoice with
  product lines. The ETL still carries per-line detail on the Singer record, but
  the sink collapses those lines into `document_goods_value` /
  `document_tax_value`. Posted documents show up under Transaction Enquiry, not
  as SOP invoices. There is no Sage 200 API for multi-line SOP invoices; use
  `import_as_sales_orders` instead. These are post-only: Sage returns a `urn`
  instead of an `id`.

The choice between a ledger document and a SOP document is not only about line
detail. A sales ledger invoice or credit note hits the customer account as soon as
it is posted, whereas a SOP order or return is a work-in-progress document that
does not affect the ledger until someone despatches or credits it in Sage — the
`invoice_credit_status` field tracks that. Tenants who reconcile against customer
balances will see nothing until the SOP document is processed.

Product type (Stock / Service/Labour / Miscellaneous) is **inherited from the
product group** in Sage and cannot be set on the product record itself. New
products created by this target therefore become stock items whenever
`default_product_group` (or the Fresho `product_group`) points at a Stock-type
group. To create non-stock items, point `default_product_group` at an existing
Sage product group whose type is Miscellaneous or Service/Labour.

## Configuration

| Setting | Required | Notes |
| --- | --- | --- |
| `client_id` | yes | Sage OAuth client |
| `client_secret` | yes | |
| `refresh_token` | yes | Rotated on every refresh and written back to the config file |
| `access_token` | no | Refreshed automatically when missing or near expiry |
| `site_id` | yes | Sent as the `X-Site` header |
| `company_id` | yes | Sent as the `X-Company` header |
| `base_url` | no | Defaults to the UK Sage 200 Extra endpoint |
| `default_nominal_code` | no | When set, adds a nominal analysis row to ledger documents |
| `import_as_sales_orders` | no (`false`) | Connect UI option. When true, ETL emits `SalesOrders` (line items) instead of `Invoices`. Credit notes are controlled separately. |
| `import_credits_as_sales_returns` | no (`false`) | Connect UI option, independent of the above. When true, ETL emits `SalesReturns` (SOP returns, line items) instead of `CreditNotes`. A SOP return does not credit the customer account until processed in Sage. |
| `allow_sop_pricing` | no | When true, send unit price/discount on SOP order lines |
| `update_existing_records` | no (`false`) | When true, refresh scalar fields on customers/products that already exist in Sage. Off by default so Sage-side values are not overwritten; matched records are skipped and only new ones created. |
| `default_product_group` | no | Fallback product group when Fresho sends an empty group. Product type (stock vs non-stock) is inherited from this group's Sage type. |

The ETL script reads tenant connector config (`target-config.json` / `config.json`, or
`config_json`) and applies:

| Setting | Default | Notes |
| --- | --- | --- |
| `import_as_sales_orders` | `false` | Same key as the Connect UI option above; also accepts legacy `ImportAsSalesOrder`. Stringy booleans are coerced. |
| `import_credits_as_sales_returns` | `false` | Same key as the Connect UI option above. Stringy booleans are coerced. |
| `default_product_group` | `null` | Used when order lines have an empty `product_group`. |

## Development

```bash
poetry install
poetry run pytest
```

Tests cover both halves: `tests/test_etl.py` runs the mapping against the
`etl/__tests__/customer_orders-599.xml` fixture, and `tests/test_sinks.py` stubs
`request_api` to assert the lookup/create/update call sequence per sink.

Run the target directly:

```bash
poetry run target-sage-200 --config /path/to/config.json < records.singer
```

Or through Meltano, which the checked-in `meltano.yml` already configures:

```bash
meltano install
meltano invoke target-sage-200 --version
```
