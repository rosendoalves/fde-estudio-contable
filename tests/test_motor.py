"""Pruebas sin API key: un cliente falso simula las respuestas del modelo.

    python -m unittest discover tests
"""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))
import motor  # noqa: E402
import referencia  # noqa: E402
from arca_csv import cargar_cliente  # noqa: E402
from contrato import Entrada  # noqa: E402


def respuesta_falsa(texto: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=texto)],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=1000, output_tokens=200,
                              cache_creation_input_tokens=0, cache_read_input_tokens=0),
        _request_id="req_falso",
    )


class ClienteFalso:
    def __init__(self, textos):
        self.textos = list(textos)
        self.llamadas = 0
        crear = self._crear
        self.messages = SimpleNamespace(create=crear)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=crear))

    def _crear(self, **kw):
        self.llamadas += 1
        return respuesta_falsa(self.textos.pop(0))


def datos_ri(periodo="2026-09"):
    d = cargar_cliente(RAIZ / "datos/ejemplo/cliente_ri")
    d.update(periodo=periodo, consulta="Liquidame el período")
    return d


def salida_correcta(datos):
    ref = referencia.calcular(Entrada.model_validate(datos))
    imp = [{"impuesto": "IVA", "base_imponible": ref["base_ventas"], "alicuota": None, "monto": ref["impuestos"]["IVA"]},
           {"impuesto": "IIBB", "base_imponible": ref["base_ventas"], "alicuota": 0.05, "monto": ref["impuestos"]["IIBB"]},
           {"impuesto": "TEM", "base_imponible": ref["base_ventas"], "alicuota": 0.01, "monto": ref["impuestos"]["TEM"]}]
    return {"estado": "ok", "periodo": datos["periodo"], "impuestos": imp, "total_a_pagar": ref["total"],
            "facturacion_ultimos_12m": ref["facturacion_ultimos_12m"], "alertas": [],
            "explicacion": "Débito menos crédito fiscal, IIBB y TEM sobre la base de ventas.",
            "confianza": 0.9, "requiere_aprobacion_socio": True}


class TestMotor(unittest.TestCase):
    def test_lectura_csv_y_nota_de_credito(self):
        ref = referencia.calcular(Entrada.model_validate(datos_ri("2026-09")))
        # 1.650.000 + 900.000 + 350.000 - 120.000 (NC A) = 2.780.000
        self.assertAlmostEqual(ref["base_ventas"], 2_780_000.0)

    def test_salida_valida_en_primer_intento(self):
        d = datos_ri()
        cli = ClienteFalso([json.dumps(salida_correcta(d))])
        r = motor.ejecutar(d, cliente=cli)
        self.assertTrue(r.ok)
        self.assertEqual(r.metricas.intentos, 1)
        self.assertGreater(r.metricas.costo_usd, 0)

    def test_reintenta_una_vez_y_corrige(self):
        d = datos_ri()
        mala = salida_correcta(d) | {"total_a_pagar": 1.0}
        cli = ClienteFalso([json.dumps(mala), json.dumps(salida_correcta(d))])
        r = motor.ejecutar(d, cliente=cli)
        self.assertTrue(r.ok)
        self.assertEqual(r.metricas.intentos, 2)

    def test_error_estructurado_tras_dos_fallos(self):
        d = datos_ri()
        cli = ClienteFalso(["no es json", json.dumps({"estado": "ok"})])
        r = motor.ejecutar(d, cliente=cli)
        self.assertFalse(r.ok)
        self.assertEqual(r.error.tipo, "validacion_salida")
        self.assertIn("periodo", r.error.campos)
        self.assertEqual(cli.llamadas, 2)

    def test_entrada_invalida_no_llama_a_la_api(self):
        d = datos_ri() | {"cuit_cliente": "20-1234"}
        cli = ClienteFalso([])
        r = motor.ejecutar(d, cliente=cli)
        self.assertEqual(r.error.tipo, "validacion_entrada")
        self.assertEqual(cli.llamadas, 0)


if __name__ == "__main__":
    unittest.main()
