"""Sage200 target sink class, which handles writing streams."""

from __future__ import annotations

import backoff
import requests
from singer_sdk.exceptions import FatalAPIError, RetriableAPIError

from singer_sdk.sinks import RecordSink

from target_hotglue.client import HotglueSink


from target_sage_200.auth import Sage200Authenticator


class Sage200Sink(HotglueSink, RecordSink):
    """Sage200 target sink class."""

    @backoff.on_exception(
        backoff.expo,
        (RetriableAPIError, requests.exceptions.ReadTimeout),
        max_tries=5,
        factor=2,
    )
    def _request(
        self, http_method, endpoint, params=None, request_data=None, headers=None
    ) -> requests.PreparedRequest:
        """Prepare a request object."""
        url = self.url(endpoint)
        headers = self.http_headers

        response = requests.request(
            method=http_method,
            url=url,
            params=params,
            headers=headers,
            json=request_data,
            auth=self.authenticator.get_auth_session(),
        )
        self.validate_response(response)
        return response

    def process_record(self, record: dict, context: dict) -> None:
        """Process the record.

        Args:
            record: Individual record in the stream.
            context: Stream partition or context dictionary.
        """
        # Sample:
        # ------
        # client.write(record)  # noqa: ERA001

    
    @property
    def authenticator(self):
        url = self.url()
        return Sage200Authenticator(
            self._target,
            self.auth_state,
            url
        )