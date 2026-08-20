"""Minimal fakes for LearnClient's HTTP dependency, so tests never touch the
network or require real Waterloo credentials.

LearnClient only ever calls `context.request.get(url)` and reads
`.status` / `.ok` / `.json()` off the result -- these fakes implement
exactly that surface, nothing more.
"""

import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES_DIR / name).read_text())


class FakeResponse:
    def __init__(self, status: int, body):
        self.status = status
        self.ok = 200 <= status < 300
        self._body = body

    def json(self):
        return self._body


class FakeAPIRequestContext:
    """routes: dict mapping a URL substring -> (status, body). First
    substring match wins; raises AssertionError if nothing matches, so a
    test can't accidentally pass by hitting an unconfigured route."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requested_urls: list[str] = []

    def get(self, url: str) -> FakeResponse:
        self.requested_urls.append(url)
        for substring, (status, body) in self.routes.items():
            if substring in url:
                return FakeResponse(status, body)
        raise AssertionError(f"No fake route configured for URL: {url}")


class FakeBrowserContext:
    def __init__(self, routes: dict):
        self.request = FakeAPIRequestContext(routes)


def make_context(extra_routes: dict) -> FakeBrowserContext:
    """Build a FakeBrowserContext with the /d2l/api/versions/ route
    pre-configured (every LearnClient call needs it), plus whatever
    extra routes a test passes in, e.g.:

        make_context({"dropbox/folders": (200, load_fixture("dropbox_folders.json"))})
    """
    routes = {"/d2l/api/versions/": (200, load_fixture("versions.json"))}
    routes.update(extra_routes)
    return FakeBrowserContext(routes)
