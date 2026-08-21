"""Sage200 target class."""

from __future__ import annotations

from singer_sdk import typing as th
from singer_sdk.target_base import Target
from target_hotglue.target import TargetHotglue

from target_sage_200.sinks import (
    Sage200Sink,
)


class TargetSage200(Target, TargetHotglue):
    """Sample target for Sage200."""

    name = "target-sage-200"

    SINK_TYPES = []
    MAX_PARALLELISM = 1

    def get_sink_class(self, stream_name: str):
        return Sage200Sink


if __name__ == "__main__":
    TargetSage200.cli()
