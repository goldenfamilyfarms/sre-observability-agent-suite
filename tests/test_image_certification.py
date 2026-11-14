"""All 120 predicates can certify a deployed image, but cannot reuse source-kind evidence."""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from sre_suite.execution.wire import canonical
from tests.test_foundation import NOW
from tests.verification_fixture import VerificationFixture


class ImageCertificationTests(unittest.TestCase):
    def test_image_kind_requires_independent_matching_fixture_then_current_gate(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        fixture = VerificationFixture(Path(temp.name).resolve())
        self.addCleanup(fixture.close)
        value = replace(fixture.input, artifact_kind="container-image-v1")
        self.assertNotEqual(fixture.engine.run(value.wire())["verdict"], "PASSED")
        doc = json.loads(fixture.api.read(value.fixture))
        doc["artifact_kind"] = "container-image-v1"
        ref = fixture.assessor.upload(
            "image-fixture",
            fixture.scope,
            canonical(doc).encode(),
            NOW,
            NOW + timedelta(minutes=15),
        )
        value = replace(value, fixture=ref)
        report = fixture.engine.run(value.wire())
        self.assertEqual(report["verdict"], "PASSED")
        self.assertEqual(len(report["results"]), 120)
        receipt = fixture.engine.run(value.wire(), publish=True)
        self.assertTrue(fixture.engine.gate(value.wire(), receipt).passed)
        with self.assertRaises(ValueError):
            fixture.engine.gate(replace(value, artifact_kind="source-candidate-v1").wire(), receipt)
