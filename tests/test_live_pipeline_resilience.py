import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from data import flood_pipeline, wildfire_pipeline


class Response:
    def __init__(self, document):
        self.body = json.dumps(document).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.body


class LivePipelineResilienceTests(unittest.TestCase):
    modules = (flood_pipeline, wildfire_pipeline)

    def test_transient_network_error_is_retried(self):
        for module in self.modules:
            with self.subTest(module=module.__name__):
                with (
                    patch.object(
                        module.urllib.request,
                        "urlopen",
                        side_effect=[
                            urllib.error.URLError("connection reset"),
                            Response({"ok": True}),
                        ],
                    ) as request,
                    patch.object(module.time, "sleep") as sleep,
                ):
                    self.assertEqual(
                        module.get_json("https://example.test"), {"ok": True}
                    )
                self.assertEqual(request.call_count, 2)
                sleep.assert_called_once_with(1)

    def test_non_retryable_http_error_fails_immediately(self):
        for module in self.modules:
            with self.subTest(module=module.__name__):
                error = urllib.error.HTTPError(
                    "https://example.test",
                    404,
                    "Not Found",
                    None,
                    io.BytesIO(b"missing"),
                )
                with (
                    patch.object(
                        module.urllib.request, "urlopen", side_effect=error
                    ) as request,
                    patch.object(module.time, "sleep") as sleep,
                ):
                    with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
                        module.get_json("https://example.test")
                request.assert_called_once()
                sleep.assert_not_called()

    def test_retry_limit_raises_a_clear_error(self):
        for module in self.modules:
            with self.subTest(module=module.__name__):
                with (
                    patch.object(
                        module.urllib.request,
                        "urlopen",
                        side_effect=urllib.error.URLError("still unavailable"),
                    ) as request,
                    patch.object(module.time, "sleep") as sleep,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "Request failed after 4 attempts"
                    ):
                        module.get_json("https://example.test")
                self.assertEqual(request.call_count, 4)
                self.assertEqual(
                    [call.args[0] for call in sleep.call_args_list], [1, 2, 4]
                )


if __name__ == "__main__":
    unittest.main()

