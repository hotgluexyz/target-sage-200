import pytest

from tests.test_core import SAMPLE_CONFIG
from target_sage_200.client import Sage200Sink, odata_string_literal
from target_sage_200.sinks import (
    CreditNotesSink,
    CustomersSink,
    InvoicesSink,
    ProductCategoriesSink,
    ProductsSink,
    SalesOrdersSink,
)
from target_sage_200.target import TargetSage200

SCHEMA = {"type": "object", "properties": {}}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = str(payload)

    def json(self):
        return self._payload


@pytest.fixture
def target():
    Sage200Sink._cache = {}
    Sage200Sink._tax_codes = {}
    return TargetSage200(config=SAMPLE_CONFIG)


def test_odata_string_literal():
    assert odata_string_literal("OSO21") == "'OSO21'"
    assert odata_string_literal("O'Brien") == "'O''Brien'"


def test_empty_schema_timestamp_parse_skips_unknown_keys(target):
    """ETL SCHEMA messages use empty properties; must not KeyError on record keys."""
    sink = ProductCategoriesSink(target, "ProductCategories", SCHEMA, [])
    record = {"code": "599iPhone", "description": "599iPhone", "invoice_date": "2024-10-01"}
    sink._parse_timestamps_in_record(record, SCHEMA, None)
    assert record["code"] == "599iPhone"
    assert record["invoice_date"] == "2024-10-01"


def test_customer_lookup_miss_posts(target):
    sink = CustomersSink(target, "Customers", SCHEMA, [])
    calls = []

    def fake_request(http_method, endpoint=None, params=None, request_data=None, headers=None):
        calls.append((http_method, endpoint, params, request_data))
        if http_method == "GET":
            return FakeResponse({"value": []})
        return FakeResponse({"id": 42}, 201)

    sink.request_api = fake_request
    record = sink.preprocess_record(
        {
            "reference": "599Fresh",
            "name": "599 Freshodemo",
            "contact_name": "Bruce Wayne",
            "telephone": "(599) 456 7890",
            "email": "demo@fresho.com",
            "vat_number": "DE123456599",
            "payment_term_days": 0,
            "payment_term_option": "DAYS_AFTER_BILL_MONTH",
        },
        {},
    )
    record_id, success, state = sink.upsert_record(record, {})
    assert success
    assert record_id == 42
    assert calls[0][0] == "GET"
    assert calls[1][0] == "POST"
    assert calls[1][1] == "/customers"


def test_customer_lookup_hit_puts(target):
    sink = CustomersSink(target, "Customers", SCHEMA, [])
    calls = []

    def fake_request(http_method, endpoint=None, params=None, request_data=None, headers=None):
        calls.append((http_method, endpoint, params, request_data))
        if http_method == "GET":
            return FakeResponse({"value": [{"id": 7, "reference": "599Fresh"}]})
        return FakeResponse({}, 200)

    sink.request_api = fake_request
    record = {"reference": "599Fresh", "name": "599 Freshodemo"}
    record_id, success, state = sink.upsert_record(record, {})
    assert success
    assert record_id == 7
    assert state.get("is_updated")
    assert calls[0][0] == "GET"
    assert calls[1][0] == "PUT"
    assert calls[1][1] == "/customers/7"


def test_product_lookup_miss_posts(target):
    sink = ProductsSink(target, "Products", SCHEMA, [])
    calls = []

    def fake_request(http_method, endpoint=None, params=None, request_data=None, headers=None):
        calls.append((http_method, endpoint, params, request_data))
        if http_method == "GET" and endpoint == "/product_groups":
            return FakeResponse([{"id": 10, "code": "599iPhone"}])
        if http_method == "GET" and endpoint == "/tax_codes":
            return FakeResponse([{"id": 99, "code": 0}])
        if http_method == "GET" and endpoint == "/warehouses":
            return FakeResponse([{"id": 5, "use_for_sales_trading": True}])
        if http_method == "GET":
            return FakeResponse([])
        return FakeResponse({"id": 55}, 201)

    sink.request_api = fake_request
    record = sink.preprocess_record(
        {
            "code": "599IphoneX",
            "name": "599 IPhone X",
            "product_group": "599iPhone",
            "tax_code": "0",
        },
        {},
    )
    assert record["product_group_id"] == 10
    assert record["tax_code_id"] == 99
    assert record["warehouse_holdings"][0]["warehouse_id"] == 5
    record_id, success, _ = sink.upsert_record(record, {})
    assert success
    assert record_id == 55
    methods = [c[0] for c in calls]
    assert "POST" in methods
    assert "PUT" not in methods


def test_product_falls_back_to_default_product_group(target):
    target._config["default_product_group"] = "ACCESSORIES"
    sink = ProductsSink(target, "Products", SCHEMA, [])

    def fake_request(http_method, endpoint=None, params=None, request_data=None, headers=None):
        if http_method == "GET" and endpoint == "/product_groups":
            filt = (params or {}).get("$filter", "")
            if "599iPhone" in filt:
                return FakeResponse([])
            if "ACCESSORIES" in filt:
                return FakeResponse([{"id": 22, "code": "ACCESSORIES"}])
            return FakeResponse([])
        if http_method == "GET" and endpoint == "/tax_codes":
            return FakeResponse([{"id": 99, "code": 0}])
        if http_method == "GET" and endpoint == "/warehouses":
            return FakeResponse([{"id": 5, "use_for_sales_trading": True}])
        return FakeResponse([])

    sink.request_api = fake_request
    record = sink.preprocess_record(
        {"code": "599IphoneX", "name": "599 IPhone X", "product_group": "599iPhone", "tax_code": "0"},
        {},
    )
    assert record["product_group_id"] == 22


def test_product_category_lookup_only_on_hit(target):
    sink = ProductCategoriesSink(target, "ProductCategories", SCHEMA, [])
    calls = []

    def fake_request(http_method, endpoint=None, params=None, request_data=None, headers=None):
        calls.append((http_method, endpoint))
        if http_method == "GET":
            return FakeResponse([{"id": 3, "code": "599iPhone"}])
        return FakeResponse({}, 200)

    sink.request_api = fake_request
    record = sink.preprocess_record({"code": "599iPhone", "description": "599iPhone"}, {})
    record_id, success, state = sink.upsert_record(record, {})
    assert success
    assert record_id == 3
    assert calls == [("GET", "/product_groups")]
    assert not state.get("is_skipped")


def test_product_category_skips_create_when_missing(target):
    sink = ProductCategoriesSink(target, "ProductCategories", SCHEMA, [])
    calls = []

    def fake_request(http_method, endpoint=None, params=None, request_data=None, headers=None):
        calls.append(http_method)
        return FakeResponse([])

    sink.request_api = fake_request
    record_id, success, state = sink.upsert_record({"code": "599iPhone"}, {})
    assert success
    assert record_id is None
    assert state.get("is_skipped")
    assert calls == ["GET"]


def test_invoice_posts_without_get_by_id(target):
    sink = InvoicesSink(target, "Invoices", SCHEMA, [])
    calls = []

    def fake_request(http_method, endpoint=None, params=None, request_data=None, headers=None):
        calls.append((http_method, endpoint, params, request_data))
        if http_method == "GET" and endpoint == "/customers":
            return FakeResponse({"value": [{"id": 21, "reference": "599Fresh"}]})
        if http_method == "GET" and endpoint == "/tax_codes":
            return FakeResponse({"value": [{"id": 99, "code": 0}]})
        if http_method == "POST":
            return FakeResponse({"urn": 1327}, 201)
        return FakeResponse({"value": []})

    sink.request_api = fake_request
    record = sink.preprocess_record(
        {
            "order_number": "599ONum",
            "ref": "P0599",
            "invoice_date": "2024-10-01",
            "customer_reference": "599Fresh",
            "lines": [
                {
                    "product_code": "599IphoneX",
                    "quantity": 1.0,
                    "unit_price": 699.99,
                    "tax": 0.0,
                    "tax_code": "0",
                    "discount_percent": 10.0,
                    "discounted_line_total": 17.85,
                },
                {
                    "product_code": "599IphoneSE",
                    "quantity": 2.0,
                    "unit_price": 599.99,
                    "tax": 0.0,
                    "tax_code": "0",
                    "discount_percent": 10.0,
                    "discounted_line_total": 17.85,
                },
            ],
        },
        {},
    )
    assert record["customer_id"] == 21
    assert record["document_goods_value"] == 35.7
    assert "tax_analysis_items" not in record
    record_id, success, _ = sink.upsert_record(record, {})
    assert success
    assert record_id == 1327
    get_endpoints = [c[1] for c in calls if c[0] == "GET"]
    assert "/sales_invoices" not in get_endpoints
    assert not any(
        c[0] == "GET" and c[1] and "/sales_invoices/" in str(c[1]) for c in calls
    )
    assert any(c[0] == "POST" for c in calls)
    posted = next(c[3] for c in calls if c[0] == "POST")
    assert "tax_analysis_items" not in (posted or {})


def test_credit_note_posts_positive_goods_value(target):
    sink = CreditNotesSink(target, "CreditNotes", SCHEMA, [])
    calls = []

    def fake_request(http_method, endpoint=None, params=None, request_data=None, headers=None):
        calls.append((http_method, endpoint, params, request_data))
        if http_method == "GET" and endpoint == "/customers":
            return FakeResponse({"value": [{"id": 21, "reference": "599Fresh"}]})
        if http_method == "GET" and endpoint == "/tax_codes":
            return FakeResponse({"value": [{"id": 99, "code": 0}]})
        if http_method == "POST":
            return FakeResponse({"urn": 27023}, 201)
        return FakeResponse({"value": []})

    sink.request_api = fake_request
    record = sink.preprocess_record(
        {
            "order_number": "599ONum",
            "ref": "P0599",
            "invoice_date": "2024-10-01",
            "customer_reference": "599Fresh",
            "lines": [
                {
                    "product_code": "599IphoneX",
                    "quantity": 1.0,
                    "unit_price": 699.99,
                    "tax": 0.0,
                    "tax_code": "0",
                    "discount_percent": 10.0,
                    "discounted_line_total": -17.85,
                },
                {
                    "product_code": "599IphoneSE",
                    "quantity": 2.0,
                    "unit_price": 599.99,
                    "tax": 0.0,
                    "tax_code": "0",
                    "discount_percent": 10.0,
                    "discounted_line_total": -17.85,
                },
            ],
        },
        {},
    )
    assert record["document_goods_value"] == 35.7
    record_id, success, _ = sink.upsert_record(record, {})
    assert success
    assert record_id == 27023
    assert sink.endpoint == "/sales_credit_notes"
    assert any(c[0] == "POST" for c in calls)


def test_sales_order_omits_pricing_by_default(target):
    sink = SalesOrdersSink(target, "SalesOrders", SCHEMA, [])

    def fake_request(http_method, endpoint=None, params=None, request_data=None, headers=None):
        if http_method == "GET" and endpoint == "/customers":
            return FakeResponse([{"id": 21, "reference": "599Fresh"}])
        if http_method == "GET" and endpoint == "/products":
            return FakeResponse([{"id": 55, "code": "599IphoneX"}])
        if http_method == "GET" and endpoint == "/tax_codes":
            return FakeResponse([{"id": 99, "code": 0}])
        return FakeResponse([])

    sink.request_api = fake_request
    record = sink.preprocess_record(
        {
            "order_number": "599ONum",
            "ref": "P0599",
            "invoice_date": "2024-10-01",
            "customer_reference": "599Fresh",
            "lines": [{
                "product_code": "599IphoneX",
                "quantity": 1.0,
                "unit_price": 699.99,
                "tax_code": "0",
                "discount_percent": 10.0,
            }],
        },
        {},
    )
    line = record["lines"][0]
    assert "selling_unit_price" not in line
    assert "unit_discount_percent" not in line


def test_sales_order_includes_pricing_when_allowed(target):
    target._config["allow_sop_pricing"] = True
    sink = SalesOrdersSink(target, "SalesOrders", SCHEMA, [])

    def fake_request(http_method, endpoint=None, params=None, request_data=None, headers=None):
        if http_method == "GET" and endpoint == "/customers":
            return FakeResponse([{"id": 21, "reference": "599Fresh"}])
        if http_method == "GET" and endpoint == "/products":
            return FakeResponse([{"id": 55, "code": "599IphoneX"}])
        if http_method == "GET" and endpoint == "/tax_codes":
            return FakeResponse([{"id": 99, "code": 0}])
        return FakeResponse([])

    sink.request_api = fake_request
    record = sink.preprocess_record(
        {
            "order_number": "599ONum",
            "ref": "P0599",
            "invoice_date": "2024-10-01",
            "customer_reference": "599Fresh",
            "lines": [{
                "product_code": "599IphoneX",
                "quantity": 1.0,
                "unit_price": 699.99,
                "tax_code": "0",
                "discount_percent": 10.0,
            }],
        },
        {},
    )
    line = record["lines"][0]
    assert line["selling_unit_price"] == 699.99
    assert line["unit_discount_percent"] == 10.0
