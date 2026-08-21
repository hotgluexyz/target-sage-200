"""Sage200 target sink class, which handles writing streams."""

from __future__ import annotations

from __future__ import annotations



from target_sage_200.client import Sage200Sink

class ExampleSink(Sage200Sink):
    """Sage200 target sink class."""

    
    endpoint = "/example-endpoint"
    unified_schema = NotImplementedError() # Place a unified schema class here
    name = unified_schema.schema_name
    

    def process_record(self, record: dict, context: dict) -> None:
        """Process the record.

        Args:
            record: Individual record in the stream.
            context: Stream partition or context dictionary.
        """
        # Sample:
        # ------
        # client.write(record)  # noqa: ERA001