import threading
from concurrent.futures import ThreadPoolExecutor

from discord_bot import _shutdown_browser


class FakeContext:
    def __init__(self):
        self.close_thread_ident = None

    def close(self):
        self.close_thread_ident = threading.get_ident()


class FakePlaywright:
    def __init__(self):
        self.stop_thread_ident = None

    def stop(self):
        self.stop_thread_ident = threading.get_ident()


def test_shutdown_browser_runs_close_and_stop_on_executor_worker_thread():
    context = FakeContext()
    playwright = FakePlaywright()
    main_thread_ident = threading.get_ident()

    with ThreadPoolExecutor(max_workers=1) as executor:
        # Pin down which thread the executor's single worker actually is.
        worker_thread_ident = executor.submit(threading.get_ident).result()

        _shutdown_browser(context, playwright, executor)

    assert context.close_thread_ident is not None
    assert playwright.stop_thread_ident is not None
    assert context.close_thread_ident != main_thread_ident
    assert playwright.stop_thread_ident != main_thread_ident
    assert context.close_thread_ident == worker_thread_ident
    assert playwright.stop_thread_ident == worker_thread_ident


def test_shutdown_browser_swallows_context_close_errors_and_still_stops_playwright():
    class RaisingContext:
        def close(self):
            raise RuntimeError("already closed")

    playwright = FakePlaywright()

    with ThreadPoolExecutor(max_workers=1) as executor:
        # Must not raise, and playwright.stop() must still run.
        _shutdown_browser(RaisingContext(), playwright, executor)

    assert playwright.stop_thread_ident is not None

