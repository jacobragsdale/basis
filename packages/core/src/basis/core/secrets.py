"""What a setting holds that is secret, hidden wherever basis shows the setting: a URL's password, a query's
signature or token, as an Azure SAS's `sig` is, and a connection string's password. A name, a host or a user is
shown, so a message still says where it points. Pure: this only rewrites text."""

import re

_LOGIN = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<user>[^/@:\s]*):(?P<password>[^/@\s]+)@")
"""A URL's `user:password@`."""
_QUERY = re.compile(r"(?P<key>[?&](?:sig|signature|password|pwd|secret|token|key|access_key|secret_key|client_secret|sas))=(?P<value>[^&\s#]*)", re.IGNORECASE)
"""A query's value under a name that holds a secret."""
_PASSWORD = re.compile(r"(?P<key>(?:^|;)\s*(?:PWD|Password)\s*=\s*)(?P<value>\{(?:[^}]|\}\})*\}|[^;]*)", re.IGNORECASE)
"""A connection string's password, braced or not."""


def redacted(text: str) -> str:
    """`text` with every secret it holds shown as `***`."""
    found = _LOGIN.sub(lambda match: f"{match['scheme']}{match['user']}:***@", text)
    found = _QUERY.sub(lambda match: f"{match['key']}=***", found)
    return _PASSWORD.sub(lambda match: f"{match['key']}***", found)
