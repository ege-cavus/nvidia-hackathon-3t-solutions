"""Client boundary checks without a GPU, service or large model download."""
import base64
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from cosmos_client import generate_image, image_request


class Response:
    status=200
    def __init__(self,payload):self.data=BytesIO(json.dumps(payload).encode())
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,*args):return self.data.read(*args)


class CosmosClientTests(unittest.TestCase):
    def setUp(self):self.item={"incident_id":20261113000001,"prompt":"Synthetic CCTV training scene"}

    def test_request_and_image_save(self):
        image=BytesIO();Image.new('RGB',(32,24)).save(image,format='JPEG')
        with tempfile.TemporaryDirectory() as folder:
            with patch('cosmos_client.urllib.request.urlopen',side_effect=[Response({}),Response({'b64_image':base64.b64encode(image.getvalue()).decode()})]) as call:
                result=generate_image(self.item,output_dir=folder)
            self.assertEqual(result['status'],'GENERATED')
            self.assertEqual((result['width'],result['height']),(32,24))
            self.assertTrue(Path(result['path']).is_file())
            payload=json.loads(call.call_args_list[1].args[0].data)
            self.assertEqual(payload['model_mode'],'text2image')
            self.assertEqual(payload['num_frames'],1)

    def test_existing_image_skips_request(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/f"{self.item['incident_id']}.jpg").write_bytes(b'existing')
            with patch('cosmos_client.urllib.request.urlopen') as call:
                self.assertEqual(generate_image(self.item,output_dir=folder)['status'],'SKIPPED_EXISTING')
                call.assert_not_called()

    def test_invalid_output_not_saved_or_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('cosmos_client.urllib.request.urlopen',side_effect=[Response({}),Response({'b64_image':'invalid'})]) as call:
                with self.assertRaises(ValueError):generate_image(self.item,output_dir=folder)
            self.assertEqual(call.call_count,2)
            self.assertEqual(list(Path(folder).iterdir()),[])

    def test_report_id_cannot_be_path(self):
        with self.assertRaises(ValueError):image_request({'incident_id':'../x','prompt':'x'})
