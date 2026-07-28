"""Read message-client configuration written by runtime activation."""

from __future__ import annotations

import json
import os


def _read_config(data_dir) -> dict:
    try:
        with open(os.path.join(str(data_dir), "config.json")) as config_file:
            return json.load(config_file)
    except Exception:
        return {}


def read_auth_token(data_dir):
    return _read_config(data_dir).get("auth_token") or None


def read_plugin_version(data_dir):
    return _read_config(data_dir).get("plugin_version") or None
