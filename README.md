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
                        + one of Invoices.json / CreditNotes.json / SalesOrders.json
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
  whose first line is negative becomes a `CreditNote`; anything else becomes an
  `Invoice`, or a `SalesOrder` when `ImportAsSalesOrder` is set. Quantities and tax
  are then made absolute, since Sage carries direction in the document type rather
  than the sign.
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
key with an OData `$filter` and either `PUT`s to the returned id or `POST`s a new
one. The natural key is stripped from the `PUT` body because Sage treats references
and codes as immutable. Lookups and the tax-code table are cached at class level, so
a run resolves each customer, product and tax code once no matter how many sinks
need it.

Documents behave differently from the master data:

- **Sales orders** (`/sop_orders`) post real product lines, each resolved to a
  `product_id`.
- **Invoices and credit notes** (`/sales_invoices`, `/sales_credits`) share
  `LedgerDocumentSink`. The sales ledger takes a financial summary rather than
  lines, so the ETL's lines are summed into a goods and tax total, with optional
  tax and nominal analysis rows. These are post-only: a posted ledger document
  cannot be looked up and amended, and Sage returns a `urn` instead of an `id`.

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

The ETL script reads its own config from `config_json` and understands one key:

| Setting | Default | Notes |
| --- | --- | --- |
| `ImportAsSalesOrder` | `false` | Emit `SalesOrders` instead of `Invoices`. Credit notes are unaffected. |

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
