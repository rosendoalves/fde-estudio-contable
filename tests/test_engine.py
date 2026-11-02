"""Pruebas sin API key: un cliente falso simula las respuestas del modelo.

    python -m unittest discover tests
"""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import motor  # noqa: E402
import reference  # noqa: E402
from arca_csv import load_client  # noqa: E402
from contract import EngineInput  # noqa: E402


def fake_response(text: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=1000, output_tokens=200,
                              cache_creation_input_tokens=0, cache_read_input_tokens=0),
        _request_id="req_fake",
    )


class FakeClient:
    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = 0
        create = self._create
        self.messages = SimpleNamespace(create=create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))

    def _create(self, **kwargs):
        self.calls += 1
        return fake_response(self.texts.pop(0))


def ri_data(period="2026-09"):
    data = load_client(ROOT / "data/sample/client_ri")
    data.update(period=period, query="Liquidame el período")
    return data


def valid_output(data):
    ref = reference.compute(EngineInput.model_validate(data))
    base = ref["sales_base"]
    taxes = [{"tax": "IVA", "tax_base": base, "rate": None, "amount": ref["taxes"]["IVA"]},
             {"tax": "IIBB", "tax_base": base, "rate": 0.05, "amount": ref["taxes"]["IIBB"]},
             {"tax": "TEM", "tax_base": base, "rate": 0.01, "amount": ref["taxes"]["TEM"]}]
    return {"status": "ok", "period": data["period"], "taxes": taxes, "total_due": ref["total"],
            "revenue_last_12m": ref["revenue_last_12m"], "alerts": [],
            "explanation": "Débito menos crédito fiscal, IIBB y TEM sobre la base de ventas.",
            "confidence": 0.9, "requires_partner_approval": True}


class TestEngine(unittest.TestCase):
    def test_csv_reading_and_credit_note(self):
        ref = reference.compute(EngineInput.model_validate(ri_data("2026-09")))
        # 1.650.000 + 900.000 + 350.000 - 120.000 (NC A) = 2.780.000
        self.assertAlmostEqual(ref["sales_base"], 2_780_000.0)

    def test_valid_output_first_attempt(self):
        data = ri_data()
        client = FakeClient([json.dumps(valid_output(data))])
        result = motor.run(data, client=client)
        self.assertTrue(result.ok)
        self.assertEqual(result.metrics.attempts, 1)
        self.assertGreater(result.metrics.cost_usd, 0)

    def test_retries_once_and_recovers(self):
        data = ri_data()
        bad = valid_output(data) | {"total_due": 1.0}
        client = FakeClient([json.dumps(bad), json.dumps(valid_output(data))])
        result = motor.run(data, client=client)
        self.assertTrue(result.ok)
        self.assertEqual(result.metrics.attempts, 2)

    def test_structured_error_after_two_failures(self):
        data = ri_data()
        client = FakeClient(["no es json", json.dumps({"status": "ok"})])
        result = motor.run(data, client=client)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.kind, "output_validation")
        self.assertIn("period", result.error.fields)
        self.assertEqual(client.calls, 2)

    def test_invalid_input_skips_api(self):
        data = ri_data() | {"client_cuit": "20-1234"}
        client = FakeClient([])
        result = motor.run(data, client=client)
        self.assertEqual(result.error.kind, "input_validation")
        self.assertEqual(client.calls, 0)


if __name__ == "__main__":
    unittest.main()
