"""Reference media boundaries using tiny local fixtures, never a GPU request."""
import base64
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image

from cosmos_reference import generate_reference_clip, video_request


class Response:
    status = 200

    def __init__(self, payload):
        self.data = BytesIO(json.dumps(payload).encode())

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, *args):
        return self.data.read(*args)


class ReferenceClientTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.reference = self.root / "source.png"
        Image.new("RGB", (32, 24), "blue").save(self.reference)
        self.config = {"incident_id": 123, "prompt": "Water spreads across the road",
                       "reference_image": str(self.reference), "num_frames": 25,
                       "attribution": {"creator": "Test author", "license": "CC BY-SA 2.0"}}

    def video(self, count=25):
        path = self.root / "fixture.mp4"
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 12, (32, 24))
        self.assertTrue(writer.isOpened())
        try:
            for index in range(count):
                writer.write(np.full((24, 32, 3), index * 5, dtype=np.uint8))
        finally:
            writer.release()
        return base64.b64encode(path.read_bytes()).decode()

    def test_request_preserves_reference_bytes_and_mime(self):
        request = video_request(self.config, self.reference, num_frames=25)
        mime, encoded = request["input_reference"].split(",", 1)
        self.assertEqual(mime, "data:image/png;base64")
        self.assertEqual(base64.b64decode(encoded), self.reference.read_bytes())
        self.assertEqual(request["model_mode"], "image2video")
        for options in ({"num_frames": 24}, {"num_frames": 298}, {"fps": float("nan")},
                        {"fps": True}, {"resolution": "480_unsupported"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                video_request(self.config, self.reference, **options)

    def test_decodes_video_extracts_frames_and_preserves_provenance(self):
        encoded = self.video()
        with patch("cosmos_reference.urllib.request.urlopen", side_effect=[Response({}), Response({"b64_video": encoded})]) as call:
            result = generate_reference_clip(self.config, output_dir=self.root / "output")
        record = json.loads(Path(result["metadata"]).read_text())
        self.assertFalse(record["review"]["approved_for_qwen"])
        self.assertEqual(record["attribution"], self.config["attribution"])
        self.assertNotIn("input_reference", record["request"])
        self.assertEqual(len(result["frames"]), 3)
        for frame in result["frames"]:
            with Image.open(frame) as image:
                self.assertEqual(image.size, (32, 24))
        self.assertEqual(call.call_count, 2)

    def test_truncated_or_invalid_video_is_not_published_or_retried(self):
        for encoded in (self.video(count=24), base64.b64encode(b"invalid video").decode()):
            with self.subTest(encoded_size=len(encoded)):
                with patch("cosmos_reference.urllib.request.urlopen", side_effect=[Response({}), Response({"b64_video": encoded})]) as call:
                    with self.assertRaises(ValueError):
                        generate_reference_clip(self.config, output_dir=self.root / "output")
                self.assertEqual(call.call_count, 2)
                self.assertEqual(list((self.root / "output").iterdir()), [])

    def test_existing_output_is_not_overwritten(self):
        output = self.root / "output"
        output.mkdir()
        (output / "123-reference.mp4").write_bytes(b"existing")
        with patch("cosmos_reference.urllib.request.urlopen") as call:
            result = generate_reference_clip(self.config, output_dir=output)
        self.assertEqual(result["status"], "SKIPPED_EXISTING")
        call.assert_not_called()
