from target_hotglue.client import HotglueSink

from target_sage_200.auth import Sage200Authenticator


BASE_URL = "https://api.columbus.sage.com/uk/sage200extra/accounts/v1"


def odata_string_literal(value):
    """Quote a value for an OData $filter, doubling embedded single quotes."""
    return "'" + str(value).replace("'", "''") + "'"


class Sage200Sink(HotglueSink):
    unified_schema = None
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

    def lookup(self, endpoint, filter_expr):
        """Return the first record matching an OData filter, or None.

        Only hits are cached. A miss usually means the record is about to be
        created, and upsert_by_field seeds the cache with the new id at that point.
        """
        cache_key = (endpoint, filter_expr)
        if cache_key in Sage200Sink._cache:
            return Sage200Sink._cache[cache_key]
        resp = self.request_api("GET", endpoint=endpoint, params={"$filter": filter_expr})
        values = resp.json().get("value") or []
        if not values:
            return None
        Sage200Sink._cache[cache_key] = values[0]
        return values[0]

    def get_tax_code_id(self, code):
        """Resolve a Sage tax code to its id, fetching the whole table on first use."""
        if not Sage200Sink._tax_codes:
            resp = self.request_api("GET", endpoint="/tax_codes")
            Sage200Sink._tax_codes = {
                str(t["code"]): t["id"] for t in resp.json().get("value", [])
            }
        return Sage200Sink._tax_codes.get(str(code))

    def upsert_by_field(self, record, field):
        """Create the record, or update it in place if the field already matches one.

        Sage has no upsert endpoint, so this searches on the natural key first. The
        key itself is stripped from the PUT body because Sage treats references and
        codes as immutable once assigned.
        """
        filter_expr = f"{field} eq {odata_string_literal(record[field])}"
        existing = self.lookup(self.endpoint, filter_expr)
        if existing:
            record_id = existing["id"]
            payload = {k: v for k, v in record.items() if k != field}
            self.request_api("PUT", endpoint=f"{self.endpoint}/{record_id}", request_data=payload)
            return record_id, True, {"is_updated": True}
        resp = self.request_api("POST", endpoint=self.endpoint, request_data=record)
        record_id = resp.json().get("id")
        Sage200Sink._cache[(self.endpoint, filter_expr)] = {**record, "id": record_id}
        return record_id, True, {}
