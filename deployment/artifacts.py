import base64
import hashlib
import os
import re
from email.message import Message
from html.parser import HTMLParser
from http.client import HTTPResponse
from pathlib import Path
from typing import IO
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import Request
from xml.etree import ElementTree

from .config import relative_path


def valid_url(url: str) -> str:
    """Return an HTTP(S) artifact URL, rejecting credentials, queries and fragments."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Artifact URLs must use HTTP or HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "Put Artifactory credentials in environment variables, not URLs"
        )
    return url


class Links(HTMLParser):
    """Collect non-empty href attributes from artifact repository listings."""

    def __init__(self) -> None:
        """Initialize the parser and its collected link list."""
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record href values from anchor start tags."""
        if tag == "a":
            self.links.extend(value for key, value in attrs if key == "href" and value)


class Repository:
    """Resolve and download artifacts from an HTTP(S) repository."""

    def __init__(self) -> None:
        """Load optional repository credentials from the environment."""
        self.headers: dict[str, str] = {}
        if os.environ.get("ARTIFACTORY_TOKEN"):
            self.headers["Authorization"] = f"Bearer {os.environ['ARTIFACTORY_TOKEN']}"
        elif os.environ.get("ARTIFACTORY_USERNAME"):
            password = os.environ["ARTIFACTORY_PASSWORD"]
            credentials = f"{os.environ['ARTIFACTORY_USERNAME']}:{password}".encode()
            self.headers["Authorization"] = (
                "Basic " + base64.b64encode(credentials).decode()
            )
        self.origin: tuple[str, str] | None = None

    def open(self, url: str) -> HTTPResponse:
        """Open a validated URL without following redirects or leaking credentials."""
        valid_url(url)
        origin = (urlsplit(url).scheme, urlsplit(url).netloc)
        if self.origin is None:
            self.origin = origin
        if self.headers and origin != self.origin:
            raise ValueError(
                "Refusing to send Artifactory credentials to another origin"
            )
        # Redirects are rejected below before any second request can forward credentials.
        from urllib.request import HTTPRedirectHandler, build_opener

        class NoRedirect(HTTPRedirectHandler):
            """Reject redirects so requests cannot forward credentials elsewhere."""

            def redirect_request(
                self,
                req: Request,
                fp: IO[bytes],
                code: int,
                msg: str,
                headers: Message,
                newurl: str,
            ) -> None:
                """Raise an error rather than constructing a redirected request."""
                raise ValueError(
                    "Artifact redirects are not supported; use the canonical URL"
                )

        return build_opener(NoRedirect).open(
            Request(url, headers=self.headers),  # noqa: S310 - valid_url allows only http(s)
            timeout=60,
        )

    def resolve(self, base: str, artifact_path: str, version: str, suffix: str) -> str:
        """Find the unique matching artifact, resolving latest through Maven metadata."""
        relative_path(artifact_path, "artifact path")
        directory = valid_url(base.rstrip("/") + "/" + artifact_path.strip("/") + "/")
        if version == "latest":
            with self.open(directory + "maven-metadata.xml") as response:
                # ElementTree never fetches external entities, and expat >= 2.4.1
                # (bundled with supported Pythons) limits entity expansion.
                metadata = ElementTree.fromstring(response.read())  # noqa: S314
            latest = metadata.findtext("./versioning/release") or metadata.findtext(
                "./versioning/latest"
            )
            if not latest:
                raise ValueError(
                    "Maven metadata does not declare a latest/release version"
                )
            version = latest
            relative_path(version, "resolved version")
            directory += version + "/"
        elif not artifact_path.endswith("/" + version):
            directory += version + "/"
        parser = Links()
        with self.open(directory) as response:
            parser.feed(response.read().decode())
        candidates: set[str] = set()
        for href in parser.links:
            url = urljoin(directory, href)
            if not url.startswith(directory) or "/" in url[len(directory) :]:
                continue
            name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
            if name.endswith(suffix) and not re.search(
                r"-(sources|javadoc|tests)\.jar$", name
            ):
                valid_url(url)
                candidates.add(url)
        if len(candidates) != 1:
            raise ValueError(
                f"Expected one {suffix} artifact, found {len(candidates)}; "
                "supply an exact --artifact-url/--config-url (including timestamped snapshots)"
            )
        return candidates.pop()

    def download(self, url: str, destination: Path, checksum: str | None = None) -> str:
        """Download an artifact and return its SHA-256 digest after optional verification."""
        digest = hashlib.sha256()
        with self.open(url) as response, destination.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        actual = digest.hexdigest()
        if checksum and actual.lower() != checksum.lower():
            raise ValueError(
                f"Artifact SHA-256 mismatch: expected {checksum.lower()}, "
                f"downloaded {actual}. Check that --sha256 is from the preprod "
                "deployment of this version; otherwise the JAR in Artifactory "
                "changed since then."
            )
        return actual
