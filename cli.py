"""Base CLI parser + config plumbing shared by the Subify scripts.

Holds the shared server/credential options (ServerConfig, add_server_options,
server_from_args) and the CLI base class. Each tool subclasses CLI to declare
its own options and Config to declare its fields; calling CLI.parse() turns
sys.argv into the tool's Config object.
"""

import argparse
import os
from dataclasses import dataclass

DEFAULT_CACHE = "sync_cache.txt"
DEFAULT_CONFIG = "creds.txt"
DEFAULT_TIMEOUT = 10.0
DEFAULT_DELAY = 0.15


@dataclass
class ServerConfig:
    base_url: str
    username: str
    password: str
    timeout: float = DEFAULT_TIMEOUT
    delay: float = DEFAULT_DELAY
    verbose: bool = False
    config_path: str = DEFAULT_CONFIG


def load_credentials_file(path: str) -> dict:
    """Read url/username/password from a simple key=value text file.

    Accepts 'url', 'username' and 'password' keys (case-insensitive, with or
    without a NAVIDROME_ prefix), '#'/';' comments and optional surrounding
    quotes. Missing files return an empty dict.
    """
    creds = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith(("#", ";")) or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip().replace("NAVIDROME_", "").lower()
                if key in ("url", "username", "password"):
                    value = value.strip().strip("\"'")
                    if value:
                        creds[key] = value
    except OSError:
        pass
    return creds


def resolve_credentials(
    url=None, username=None, password=None, config_path: str = None
) -> tuple:
    """Merge credentials: CLI > config file > env > default URL."""
    file_creds = load_credentials_file(config_path) if config_path else {}
    cfg = {
        "url": (
            url
            if url is not None
            else file_creds.get("url") or os.environ.get("NAVIDROME_URL", "")
        ),
        "username": (
            username
            if username is not None
            else file_creds.get("username") or os.environ.get("NAVIDROME_USER", "")
        ),
        "password": (
            password
            if password is not None
            else file_creds.get("password") or os.environ.get("NAVIDROME_PASSWORD", "")
        ),
    }
    return cfg["url"].rstrip("/"), cfg["username"], cfg["password"]


def add_server_options(
    parser,
    *,
    default_timeout: float = DEFAULT_TIMEOUT,
    default_delay: float = DEFAULT_DELAY,
):
    """Add the shared server/credential options to an argparse parser.

    Adds --config, --url, --username, --password, --timeout, --delay,
    --verbose. Call server_from_args() after parse_args() with the same parser
    to resolve the credentials into a ServerConfig object.
    """
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help=f"credentials file with url/username/password "
        f"(default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Subsonic URL (overrides config file and NAVIDROME_URL)",
    )
    parser.add_argument(
        "--username",
        default=None,
        help="Subsonic username (overrides config file and NAVIDROME_USER)",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="Subsonic password (overrides config file and NAVIDROME_PASSWORD)",
    )
    parser.add_argument("--timeout", type=float, default=default_timeout)
    parser.add_argument(
        "--delay",
        type=float,
        default=default_delay,
        help="seconds to wait between API calls",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="print debug output for each request"
    )
    return parser


def server_from_args(parser, args) -> ServerConfig:
    """Resolve the shared options parsed by add_server_options().

    Exits with a usage error (via parser.error) when no username/password are
    available from the CLI, the config file, or the environment.
    """
    base_url, username, password = resolve_credentials(
        args.url, args.username, args.password, args.config
    )
    if not username and not password:
        parser.error(
            "username/password required (use --username/--password, "
            "a --config file, or the NAVIDROME_USER/NAVIDROME_PASSWORD env vars)"
        )
    return ServerConfig(
        base_url=base_url,
        username=username,
        password=password,
        timeout=args.timeout,
        delay=args.delay,
        verbose=args.verbose,
        config_path=args.config,
    )


@dataclass
class Config:
    csv_path: str
    server: ServerConfig
    auto_skip: bool = False
    cache_path: str = DEFAULT_CACHE
    no_cache: bool = False


class CLI:
    """Base class combining argument parsing and config building.

    Subclasses extend add_arguments() with their own options and override
    build_config() to construct their Config dataclass. Call parse() to run the
    parser and get the config object.
    """

    description = ""
    csv_default = "Liked_Songs.csv"
    default_delay = DEFAULT_DELAY

    def __init__(self, description: str = None):
        self.parser = argparse.ArgumentParser(
            description=description if description is not None else self.description
        )
        self.parser.add_argument(
            "--csv",
            default=self.csv_default,
            help=f"Spotify playlist export CSV (default: {self.csv_default})",
        )
        add_server_options(self.parser, default_delay=self.default_delay)
        self.parser.add_argument(
            "--auto-skip",
            action="store_true",
            help="skip unresolvable tracks without prompting",
        )
        self.parser.add_argument(
            "--cache",
            default=DEFAULT_CACHE,
            help="txt file linking Spotify queries to Subsonic title/artist "
            f"matches (default: {DEFAULT_CACHE})",
        )
        self.parser.add_argument(
            "--no-cache",
            action="store_true",
            help="do not read or write the track-mapping cache file",
        )
        self.add_arguments()

    def add_arguments(self):
        """Add tool-specific arguments; override in subclasses."""

    def build_config(self, args) -> Config:
        return Config(
            csv_path=args.csv,
            server=server_from_args(self.parser, args),
            auto_skip=args.auto_skip,
            cache_path=args.cache,
            no_cache=args.no_cache,
        )

    def parse(self, argv: list = None) -> Config:
        args = self.parser.parse_args(argv)
        return self.build_config(args)
