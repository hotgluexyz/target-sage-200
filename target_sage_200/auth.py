import json

import requests
from hotglue_singer_sdk.helpers._util import utc_now
from hotglue_singer_sdk.target_sdk.auth import OAuthAuthenticator


AUTH_ENDPOINT = "https://id.sage.com/oauth/token"


class Sage200Authenticator(OAuthAuthenticator):
    def __init__(self, target, state=None):
        super().__init__(target, state or {}, auth_endpoint=AUTH_ENDPOINT)
        if self._config.get("expires_in") is not None:
            self.expires_in = int(self._config["expires_in"])
            self.last_refreshed = utc_now()

    def update_access_token(self):
        """Refresh the token and persist it back to the config file.

        Overrides the SDK only to keep the request body, which carries the client
        secret and refresh token, out of the logs.
        """
        request_time = utc_now()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        self.logger.info(f"Oauth request - endpoint: {self._auth_endpoint}")
        token_response = requests.post(
            self._auth_endpoint, data=self.oauth_request_body, headers=headers
        )
        token_response.raise_for_status()
        token_json = token_response.json()
        self.access_token = token_json["access_token"]
        self._config["access_token"] = token_json["access_token"]
        self._config["refresh_token"] = token_json["refresh_token"]
        self.expires_in = int(token_json["expires_in"]) + int(request_time.timestamp())
        self.last_refreshed = request_time
        self._config["expires_in"] = self.expires_in
        with open(self._config_file_path, "w") as outfile:
            json.dump(self._config, outfile, indent=4)
