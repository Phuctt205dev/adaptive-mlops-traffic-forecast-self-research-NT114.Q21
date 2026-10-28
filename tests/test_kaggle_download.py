import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from src.cloud_training.kaggle_execution import download_file
from src.cloud_training.kaggle_transport import ProviderError


class Response:
    headers = {}
    def __init__(self, chunks):
        self.chunks = chunks
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def raise_for_status(self):
        pass
    def iter_content(self, size):
        for item in self.chunks:
            if isinstance(item, Exception):
                raise item
            yield item


class KaggleDownloadTests(unittest.TestCase):
    def test_expired_signed_url_is_refreshed_for_the_same_file(self):
        body = b"verified"
        expired = Response([])
        http = requests.exceptions.HTTPError(response=type("HTTP", (), {"status_code": 403})())
        expired.raise_for_status = Mock(side_effect=http)
        refresh = Mock(return_value="https://provider.example/new-signed-url")
        with tempfile.TemporaryDirectory() as directory, \
             patch("requests.get", side_effect=[expired, Response([body])]) as get, \
             patch("src.cloud_training.kaggle_execution.time.sleep"):
            download_file("https://provider.example/expired", Path(directory) / "file", limit=len(body),
                          expected_size=len(body), expected_sha256=hashlib.sha256(body).hexdigest(), refresh_url=refresh)
        refresh.assert_called_once()
        self.assertEqual(get.call_count, 2)

    def test_partial_response_is_removed_then_get_retries_with_a_fresh_url(self):
        body = b"complete verified synthetic output"
        refresh = Mock(return_value="https://provider.example/fresh")
        with tempfile.TemporaryDirectory() as directory, \
             patch("requests.get", side_effect=[Response([b"partial", requests.exceptions.ReadTimeout()]), Response([body])]) as get, \
             patch("src.cloud_training.kaggle_execution.time.sleep"):
            path = Path(directory) / "model.bin"
            download_file("https://provider.example/old", path, limit=len(body), expected_size=len(body),
                          expected_sha256=hashlib.sha256(body).hexdigest(), refresh_url=refresh)
            self.assertEqual(path.read_bytes(), body)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.args[0], "https://provider.example/fresh")
        refresh.assert_called_once()

    def test_checksum_failure_is_not_retried_or_retained(self):
        with tempfile.TemporaryDirectory() as directory, patch("requests.get", return_value=Response([b"wrong"])) as get:
            path = Path(directory) / "model.bin"
            with self.assertRaises(ProviderError) as caught:
                download_file("https://provider.example/file", path, limit=10, expected_size=5, expected_sha256="a" * 64)
            self.assertEqual(caught.exception.category, "checksum_mismatch")
            self.assertFalse(path.exists())
        self.assertEqual(get.call_count, 1)

    def test_existing_destination_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory, patch("requests.get") as get:
            path = Path(directory) / "existing.bin";path.write_bytes(b"preserve")
            with self.assertRaises(ProviderError):
                download_file("https://provider.example/file", path, limit=10)
            self.assertEqual(path.read_bytes(), b"preserve")
        get.assert_not_called()

    def test_retry_budget_ends_with_a_normalized_error(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("requests.get", side_effect=requests.exceptions.ConnectTimeout("PRIVATE_RAW_DETAIL")) as get, \
             patch("src.cloud_training.kaggle_execution.time.sleep"):
            with self.assertRaises(ProviderError) as caught:
                download_file("https://provider.example/file", Path(directory) / "model.bin", limit=10)
        self.assertEqual(get.call_count, 3)
        self.assertEqual(caught.exception.operation, "output_download")
        self.assertNotIn("PRIVATE_RAW_DETAIL", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
