from target_sage_200.client import RecordMappingError, Sage200Sink


def _as_datetime(value):
    """Sage rejects bare dates on document fields, so widen them to midnight UTC."""
    if value and "T" not in str(value):
        return f"{value}T00:00:00Z"
    return value


class ProductCategoriesSink(Sage200Sink):
    name = "ProductCategories"
    endpoint = "/product_groups"
    identity_fields = ("code", "description")

    def preprocess_record(self, record, context):
        code = self.require(record, "code")
        return self.clean_payload({
            "code": code,
            "description": record.get("description") or code,
        })

    def upsert_record(self, record, context):
        """Look up only: this Sage build returns 404 for POST/PUT on product_groups."""
        existing = self.find_by_field(self.endpoint, "code", record["code"])
        if existing:
            return existing["id"], True, {}
        self.logger.warning(
            "Product group %s not found and cannot be created via API; "
            "products will use default_product_group if configured",
            record.get("code"),
        )
        return None, True, {"is_skipped": True}


class ProductsSink(Sage200Sink):
    name = "Products"
    endpoint = "/products"
    identity_fields = ("code", "name")

    def require_product_group(self, record):
        """Resolve the record's product group, explaining the config fix if it fails."""
        code = record.get("product_group")
        group = self.resolve_product_group(code)
        if group:
            return group
        described = f"{code!r}" if code else "(field 'product_group' not set)"
        raise RecordMappingError(
            f"{self.name} record ({self.record_label(record)}): product group "
            f"{described} does not exist in Sage and cannot be created through the "
            "API; set default_product_group in the connector config to an existing "
            "Sage product group code"
        )

    def preprocess_record(self, record, context):
        return self.clean_payload({
            "code": self.require(record, "code"),
            "name": self.require(record, "name"),
            "product_group_id": self.require_product_group(record)["id"],
            "tax_code_id": self.get_tax_code_id(record.get("tax_code")),
            "allow_sales_order": True,
            "warehouse_holdings": [{
                "warehouse_id": self.get_warehouse_id(),
                "reorder_level": 0,
                "minimum_level": 0,
                "maximum_level": 0,
            }],
        })

    def upsert_record(self, record, context):
        return self.upsert_by_field(record, "code")


class CustomersSink(Sage200Sink):
    name = "Customers"
    endpoint = "/customers"
    identity_fields = ("reference", "name")

    def preprocess_record(self, record, context):
        payload = {
            "reference": self.require(record, "reference"),
            "name": self.require(record, "name"),
            "vat_number": record.get("vat_number"),
            "telephone_subscriber_number": record.get("telephone"),
            "payment_terms_days": record.get("payment_term_days"),
            "payment_terms_basis": (
                "PaymentDueFromEndOfMonth"
                if record.get("payment_term_option") == "DAYS_AFTER_BILL_MONTH"
                else "PaymentDueFromDocumentDate"
            ),
        }
        if record.get("contact_name"):
            contact = {"name": record["contact_name"], "is_default": True}
            if record.get("email"):
                contact["email"] = record["email"]
            payload["contacts"] = [contact]
        return self.clean_payload(payload)

    def upsert_record(self, record, context):
        return self.upsert_by_field(record, "reference")


class DocumentSink(Sage200Sink):
    """Shared customer resolution for the streams that post a document."""

    identity_fields = ("order_number", "ref", "customer_reference")

    def require_customer(self, record):
        return self.require_by_field(
            "/customers",
            "reference",
            self.require(record, "customer_reference"),
            "Customer",
            hint="the Customers stream creates it earlier in the same job, so check "
            "that stream for errors",
        )


class SopDocumentSink(DocumentSink):
    """Shared mapping for the SOP documents (sales orders and sales returns).

    Both endpoints take an identical body of real product lines, each resolved to
    a Sage ``product_id``. Direction lives in the document type rather than the
    sign of the quantities, so a return posts the same positive figures as an
    order and only the endpoint differs.
    """

    def line_product_id(self, line, position, record):
        """Resolve a line's product code to a Sage product_id, naming the bad line."""
        label = f"record ({self.record_label(record)}) line {position}"
        if line.get("description"):
            label += f" ({line['description']!r})"
        code = self.require(line, "product_code", label)
        return self.require_by_field(
            "/products",
            "code",
            code,
            "Product",
            hint="the Products stream creates it earlier in the same job, so check "
            "that stream for errors",
        )["id"]

    def build_line(self, line, position, record):
        sop_line = {
            "line_type": "EnumLineTypeStandard",
            "product_id": self.line_product_id(line, position, record),
            "line_quantity": line.get("quantity"),
            "tax_code_id": self.get_tax_code_id(line.get("tax_code")),
        }
        # Many Sage API users cannot set line pricing on SOP orders; omit unless
        # explicitly enabled and the fields are present.
        if self.config.get("allow_sop_pricing"):
            if line.get("unit_price") is not None:
                sop_line["selling_unit_price"] = line.get("unit_price")
            if line.get("discount_percent") is not None:
                sop_line["unit_discount_percent"] = line.get("discount_percent")
        return sop_line

    def preprocess_record(self, record, context):
        customer = self.require_customer(record)
        lines = [
            self.build_line(line, position, record)
            for position, line in enumerate(record.get("lines") or [], start=1)
        ]
        # Analysis codes are positional (analysis_code_1, _2, …). Label them in Sage
        # as "F number" and "PO number" (Maintain Analysis Codes, free text) so the
        # UI matches. Values are the record's order_number and ref.
        return self.clean_payload({
            "customer_id": customer["id"],
            "document_date": _as_datetime(record.get("invoice_date")),
            "customer_document_no": record.get("ref"),
            "analysis_code_1": record.get("order_number"),
            "analysis_code_2": record.get("ref"),
            "lines": lines,
        })


class SalesOrdersSink(SopDocumentSink):
    name = "SalesOrders"
    endpoint = "/sop_orders"


class SalesReturnsSink(SopDocumentSink):
    name = "SalesReturns"
    endpoint = "/sop_returns"


class LedgerDocumentSink(DocumentSink):
    """Shared mapping for the sales ledger documents (invoices and credit notes).

    Unlike sales orders, the ledger endpoints post a financial summary rather than
    product lines, so the ETL's lines are collapsed into a single goods and tax
    total plus optional tax/nominal analysis rows.
    """

    @staticmethod
    def goods_value(lines):
        """Prefer the discounted totals the source supplies, else quantity x price."""
        discounted = sum(float(line.get("discounted_line_total") or 0) for line in lines)
        if discounted:
            return abs(discounted)
        return abs(sum(
            float(line.get("quantity") or 0) * float(line.get("unit_price") or 0)
            for line in lines
        ))

    def preprocess_record(self, record, context):
        lines = record.get("lines") or []
        goods = self.goods_value(lines)
        tax = sum(float(line.get("tax") or 0) for line in lines)
        customer = self.require_customer(record)
        payload = {
            "customer_id": customer["id"],
            "reference": record.get("order_number"),
            "second_reference": record.get("ref"),
            "transaction_date": _as_datetime(record.get("invoice_date")),
            "document_goods_value": goods,
            "document_tax_value": tax,
        }
        # Do not send tax_analysis_items on create: Sage rejects them without an id
        # (SageNullFieldException). A goods/tax total alone is enough to post.
        nominal = self.config.get("default_nominal_code")
        if nominal:
            payload["nominal_analysis_items"] = [{"code": nominal, "goods_value": goods}]
        return self.clean_payload(payload)

    def upsert_record(self, record, context):
        """Post only: posted ledger documents cannot be looked up and amended."""
        body = self.request_api("POST", request_data=record).json()
        return body.get("urn") or body.get("id"), True, {}


class InvoicesSink(LedgerDocumentSink):
    name = "Invoices"
    endpoint = "/sales_invoices"


class CreditNotesSink(LedgerDocumentSink):
    name = "CreditNotes"
    endpoint = "/sales_credit_notes"
