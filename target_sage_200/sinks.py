from target_sage_200.client import Sage200Sink, odata_string_literal


def _as_datetime(value):
    """Sage rejects bare dates on document fields, so widen them to midnight UTC."""
    if value and "T" not in str(value):
        return f"{value}T00:00:00Z"
    return value


class ProductCategoriesSink(Sage200Sink):
    name = "ProductCategories"
    endpoint = "/product_groups"

    def preprocess_record(self, record, context):
        return self.clean_payload({
            "code": record["code"],
            "description": record.get("description") or record["code"],
        })

    def upsert_record(self, record, context):
        """Look up only: this Sage build returns 404 for POST/PUT on product_groups."""
        existing = self.lookup(
            self.endpoint,
            f"code eq {odata_string_literal(record['code'])}",
        )
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

    def preprocess_record(self, record, context):
        group = self.resolve_product_group(record["product_group"])
        if not group:
            raise Exception(
                f"Product group {record['product_group']!r} not found; set "
                "default_product_group in config to an existing Sage product group code"
            )
        return self.clean_payload({
            "code": record["code"],
            "name": record["name"],
            "product_group_id": group["id"],
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

    def preprocess_record(self, record, context):
        payload = {
            "reference": record["reference"],
            "name": record["name"],
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


class SalesOrdersSink(Sage200Sink):
    name = "SalesOrders"
    endpoint = "/sop_orders"

    def preprocess_record(self, record, context):
        customer = self.lookup(
            "/customers",
            f"reference eq {odata_string_literal(record['customer_reference'])}",
        )
        lines = []
        for line in record.get("lines") or []:
            product = self.lookup(
                "/products",
                f"code eq {odata_string_literal(line['product_code'])}",
            )
            sop_line = {
                "line_type": "EnumLineTypeStandard",
                "product_id": product["id"],
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
            lines.append(sop_line)
        # Analysis codes are positional (analysis_code_1, _2, …). Label them in Sage
        # as "F number" and "PO number" (Maintain Analysis Codes, free text) so the
        # UI matches. Values are Fresho order_number and ref.
        return self.clean_payload({
            "customer_id": customer["id"],
            "document_date": _as_datetime(record.get("invoice_date")),
            "customer_document_no": record.get("ref"),
            "analysis_code_1": record.get("order_number"),
            "analysis_code_2": record.get("ref"),
            "lines": lines,
        })


class LedgerDocumentSink(Sage200Sink):
    """Shared mapping for the sales ledger documents (invoices and credit notes).

    Unlike sales orders, the ledger endpoints post a financial summary rather than
    product lines, so the ETL's lines are collapsed into a single goods and tax
    total plus optional tax/nominal analysis rows.
    """

    @staticmethod
    def goods_value(lines):
        """Prefer the discounted totals Fresho supplies, else quantity x price."""
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
        customer = self.lookup(
            "/customers",
            f"reference eq {odata_string_literal(record['customer_reference'])}",
        )
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
