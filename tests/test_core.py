"""Tests for target-sage-200."""

from target_sage_200.target import TargetSage200, _coerce_int_config

SAMPLE_CONFIG = {
    "client_id": "client-id",
    "client_secret": "client-secret",
    "refresh_token": "refresh-token",
    "access_token": "access-token",
    "site_id": "71ace3b9-a75c-4600-a1b9-ae958cbfb060",
    "company_id": 2,
}


def test_coerce_int_config_string_company_id():
    config = {**SAMPLE_CONFIG, "company_id": "2", "warehouse_id": "10"}
    _coerce_int_config(config)
    assert config["company_id"] == 2
    assert config["warehouse_id"] == 10


def test_target_accepts_string_company_id():
    target = TargetSage200(config={**SAMPLE_CONFIG, "company_id": "2"}, validate_config=True)
    assert target.config["company_id"] == 2
