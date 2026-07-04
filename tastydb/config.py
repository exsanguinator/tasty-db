"""Runtime configuration, sourced from environment variables (optionally
seeded from a .env file in the working directory).

Required for API access (create these at my.tastytrade.com -> Manage -> My Profile
-> API -> OAuth Applications; see README):

    TT_CLIENT_SECRET   OAuth application client secret
    TT_REFRESH_TOKEN   refresh token from a personal grant
    TT_CLIENT_ID       optional; sent along if set
    TT_ENV             "prod" (default) or "sandbox"

Other settings:

    TASTYDB_DB_URL        SQLAlchemy URL. Default depends on TT_ENV so the two
                          environments never share a database:
                          sqlite:///tastydb.sqlite3 (prod),
                          sqlite:///tastydb-sandbox.sqlite3 (sandbox).
    TASTYDB_MATCH_METHOD  "fifo" (default) or "lifo"
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROD_BASE_URL = "https://api.tastyworks.com"
SANDBOX_BASE_URL = "https://api.cert.tastyworks.com"

PROD_DEFAULT_DB_URL = "sqlite:///tastydb.sqlite3"
SANDBOX_DEFAULT_DB_URL = "sqlite:///tastydb-sandbox.sqlite3"

USER_AGENT = "tasty-db/0.1"  # tastytrade rejects requests without a User-Agent


def load_dotenv(path: str | Path = ".env") -> dict[str, str]:
    """Load KEY=VALUE lines from a .env file into os.environ.

    Real environment variables win: a key already present in the environment
    is never overridden. Supports blank lines, `#` comments, an optional
    `export ` prefix, and single/double quotes around the value. Returns the
    keys actually applied.
    """
    path = Path(path)
    applied: dict[str, str] = {}
    if not path.is_file():
        return applied
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied


@dataclass
class Config:
    # None means "use the per-environment default" — resolved lazily by
    # resolved_db_url so a later `--sandbox` override still picks the right file.
    db_url: str | None = field(default_factory=lambda: os.environ.get("TASTYDB_DB_URL"))
    client_id: str | None = field(default_factory=lambda: os.environ.get("TT_CLIENT_ID"))
    client_secret: str | None = field(default_factory=lambda: os.environ.get("TT_CLIENT_SECRET"))
    refresh_token: str | None = field(default_factory=lambda: os.environ.get("TT_REFRESH_TOKEN"))
    env: str = field(default_factory=lambda: os.environ.get("TT_ENV", "prod"))
    match_method: str = field(
        default_factory=lambda: os.environ.get("TASTYDB_MATCH_METHOD", "fifo").lower()
    )
    # Don't auto-expire lots until this many days past expiration, so a late-posting
    # settlement/assignment transaction can still claim them.
    expiration_grace_days: int = 4

    @property
    def base_url(self) -> str:
        return SANDBOX_BASE_URL if self.env == "sandbox" else PROD_BASE_URL

    @property
    def resolved_db_url(self) -> str:
        """Explicit --db/TASTYDB_DB_URL wins; otherwise each environment gets
        its own database so sandbox testing never touches prod data."""
        if self.db_url:
            return self.db_url
        return SANDBOX_DEFAULT_DB_URL if self.env == "sandbox" else PROD_DEFAULT_DB_URL

    @property
    def has_credentials(self) -> bool:
        return bool(self.client_secret and self.refresh_token)
