from dateutil import parser
from singer_sdk.exceptions import FatalAPIError, RetriableAPIError
from singer_sdk.helpers._typing import (
    DatetimeErrorTreatmentEnum,
    get_datelike_property_type,
    handle_invalid_timestamp_in_record,
)
from target_hotglue.client import HotglueSink

from target_sage_200.auth import Sage200Authenticator


BASE_URL = "https://api.columbus.sage.com/uk/sage200extra/accounts/v1"


def odata_string_literal(value):
    """Quote a value for an OData $filter, doubling embedded single quotes."""
    return "'" + str(value).replace("'", "''") + "'"


class RecordMappingError(Exception):
    """A record cannot be mapped onto Sage, naming the record and field at fault."""


def response_records(payload):
    """Normalize Sage list payloads to a list of records.

    Columbus sometimes returns a bare JSON array and sometimes an OData
    ``{"value": [...]}`` wrapper, depending on the endpoint and query.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        value = payload.get("value")
        if isinstance(value, list):
            return value
        if payload.get("id") is not None or payload.get("urn") is not None:
            return [payload]
    return []


class Sage200Sink(HotglueSink):
    unified_schema = None
    # Fields that identify a record in error messages, most identifying first.
    identity_fields = ()
    # Caches are class level on purpose: every sink resolves the same customers,
    # products and tax codes, so sharing them keeps one lookup per entity per run.
    _cache = {}
    _tax_codes = {}

    @property
    def base_url(self):
        return self.config.get("base_url") or BASE_URL

    @property
    def http_headers(self):
        return {
            "X-Site": self.config["site_id"],
            "X-Company": str(self.config["company_id"]),
        }

    @property
    def authenticator(self):
        if not getattr(self, "_authenticator", None):
            self._authenticator = Sage200Authenticator(self._target)
        return self._authenticator

    def _parse_timestamps_in_record(self, record, schema, treatment):
        """Skip fields absent from the schema (ETL emits empty properties {}).

        singer-sdk 0.9 indexes schema["properties"][key] for every record key and
        KeyErrors when the SCHEMA message has no property definitions.
        """
        properties = (schema or {}).get("properties") or {}
        for key in list(record.keys()):
            if key not in properties:
                continue
            datelike_type = get_datelike_property_type(properties[key])
            if not datelike_type:
                continue
            date_val = record[key]
            try:
                if record[key] is not None:
                    date_val = parser.parse(record[key])
            except Exception as ex:
                date_val = handle_invalid_timestamp_in_record(
                    record,
                    [key],
                    date_val,
                    datelike_type,
                    ex,
                    treatment or DatetimeErrorTreatmentEnum.ERROR,
                    self.logger,
                )
            record[key] = date_val

    def validate_response(self, response):
        """Surface status/path when Sage returns an empty error body (common on 401)."""
        if response.status_code in [429] or 500 <= response.status_code < 600:
            raise RetriableAPIError(self.response_error_message(response), response)
        if 400 <= response.status_code < 500:
            body = (response.text or "").strip()
            if not body:
                body = self.response_error_message(response)
                if response.status_code == 401:
                    body += (
                        " — check site_id/company_id and that GET /sites returns this "
                        "site for the authenticated Sage ID"
                    )
            raise FatalAPIError(body)
        return None

    def record_label(self, record):
        """Name a record by whichever identity fields it carries."""
        parts = [
            f"{field}={record[field]!r}"
            for field in self.identity_fields
            if record.get(field) not in (None, "")
        ]
        return ", ".join(parts) or "no identifying fields"

    def require(self, record, field, label=None):
        """Return a required field, or raise naming the record and the field.

        An upstream mapping leaves a field out entirely when the source does not
        supply it, so a missing key here is a gap in the incoming data rather than a
        bug. The message has to identify the record as well as the field, because
        neither the stream nor the offending row survives a bare KeyError.
        """
        value = record.get(field)
        if value is None or value == "":
            raise RecordMappingError(
                f"{self.name} {label or f'record ({self.record_label(record)})'}: "
                f"missing required field {field!r}"
            )
        return value

    def find_by_field(self, endpoint, field, value):
        """Return the record whose field equals value, or None."""
        return self.lookup(endpoint, f"{field} eq {odata_string_literal(value)}")

    def require_by_field(self, endpoint, field, value, entity, hint=None):
        """Look up a record by exact field match, raising if Sage has no match."""
        found = self.find_by_field(endpoint, field, value)
        if not found:
            message = (
                f"{entity} {value!r} does not exist in Sage: no {endpoint} record "
                f"has {field} {value!r}"
            )
            raise RecordMappingError(f"{message} ({hint})" if hint else message)
        return found

    def lookup(self, endpoint, filter_expr):
        """Return the first record matching an OData filter, or None.

        Only hits are cached. A miss usually means the record is about to be
        created, and upsert_by_field seeds the cache with the new id at that point.
        """
        cache_key = (endpoint, filter_expr)
        if cache_key in Sage200Sink._cache:
            return Sage200Sink._cache[cache_key]
        resp = self.request_api("GET", endpoint=endpoint, params={"$filter": filter_expr})
        values = response_records(resp.json())
        if not values:
            return None
        Sage200Sink._cache[cache_key] = values[0]
        return values[0]

    def get_tax_code_id(self, code):
        """Resolve a Sage tax code to its id, fetching the whole table on first use."""
        if not Sage200Sink._tax_codes:
            resp = self.request_api("GET", endpoint="/tax_codes")
            Sage200Sink._tax_codes = {
                str(t["code"]): t["id"] for t in response_records(resp.json())
            }
        return Sage200Sink._tax_codes.get(str(code))

    def get_warehouse_id(self):
        """Return configured warehouse_id, or the first warehouse flagged for sales trading."""
        if self.config.get("warehouse_id") is not None:
            return self.config["warehouse_id"]
        cache_key = ("/warehouses", "__default__")
        if cache_key in Sage200Sink._cache:
            return Sage200Sink._cache[cache_key]["id"]
        resp = self.request_api("GET", endpoint="/warehouses")
        warehouses = response_records(resp.json())
        chosen = next(
            (w for w in warehouses if w.get("use_for_sales_trading")),
            warehouses[0] if warehouses else None,
        )
        if not chosen:
            raise FatalAPIError("No warehouses available to attach product warehouse_holdings")
        Sage200Sink._cache[cache_key] = chosen
        return chosen["id"]

    def resolve_product_group(self, code):
        """Look up a product group by code, falling back to default_product_group."""
        group = (
            self.lookup("/product_groups", f"code eq {odata_string_literal(code)}")
            if code
            else None
        )
        if group:
            return group
        default = self.config.get("default_product_group")
        if not default or default == code:
            return None
        return self.lookup(
            "/product_groups",
            f"code eq {odata_string_literal(default)}",
        )

    @staticmethod
    def scalar_update_payload(record, field):
        """Drop the natural key and any nested collections from an update body.

        Sage treats references and codes as immutable once assigned, and nested
        collections (contacts, warehouse_holdings, etc.) need their own ids on PUT,
        so updates only refresh scalar fields.
        """
        return {
            k: v
            for k, v in record.items()
            if k != field and not isinstance(v, (list, dict))
        }

    def upsert_by_field(self, record, field):
        """Create the record, or update it if the field already matches one.

        Sage has no upsert endpoint, so this searches on the natural key first. A
        match is left untouched unless ``update_existing_records`` is set: records
        curated in Sage should win over the source feed, which only ever seeds new
        ones.
        """
        filter_expr = f"{field} eq {odata_string_literal(record[field])}"
        existing = self.lookup(self.endpoint, filter_expr)
        if existing:
            record_id = existing["id"]
            if not self.config.get("update_existing_records"):
                return record_id, True, {"existing": True}
            payload = self.scalar_update_payload(record, field)
            self.request_api("PUT", endpoint=f"{self.endpoint}/{record_id}", request_data=payload)
            return record_id, True, {"is_updated": True}
        resp = self.request_api("POST", endpoint=self.endpoint, request_data=record)
        body = resp.json()
        record_id = body.get("id") if isinstance(body, dict) else None
        Sage200Sink._cache[(self.endpoint, filter_expr)] = {**record, "id": record_id}
        return record_id, True, {}
