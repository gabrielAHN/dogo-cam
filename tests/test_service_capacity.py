import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ServiceCapacityTest(unittest.TestCase):
    def test_gunicorn_keeps_control_headroom_at_max_stream_viewers(self):
        """MJPEG viewers must not occupy every Gunicorn request thread."""
        service = (ROOT / "service_startup" / "dog-stream-flask.service").read_text()
        env_example = (ROOT / ".env.example").read_text()

        threads_match = re.search(r"--threads\s+(\d+)", service)
        viewers_match = re.search(r"^MAX_VIEWERS=(\d+)$", env_example, re.MULTILINE)
        if threads_match is None:
            self.fail("Gunicorn thread count is missing from the service")
        if viewers_match is None:
            self.fail("MAX_VIEWERS is missing from .env.example")

        threads = int(threads_match.group(1))
        max_viewers = int(viewers_match.group(1))
        self.assertGreaterEqual(
            threads,
            max_viewers + 2,
            "MJPEG streams are long-lived; reserve at least two threads for controls and health checks",
        )


if __name__ == "__main__":
    unittest.main()
