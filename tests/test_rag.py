"""Pruebas del RAG sin API key: ChromaDB local en una carpeta temporal y un cliente falso del modelo.

    python -m unittest discover tests

Cubren los criterios de la PE3 como casos de prueba (entrada -> resultado esperado -> condición):
  - ingesta con metadatos documento / sección / roles en cada fragmento;
  - el filtro por rol va DENTRO de la consulta a ChromaDB (vectorial y corpus de BM25);
  - un auxiliar no recibe nada de un documento restringido, ni en resultados ni en el prompt;
  - un socio sí lo recibe (control positivo);
  - sin evidencia -> requires_human = true; una fuente no recuperada no se puede citar.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import rag  # noqa: E402
from chunking import load_documents  # noqa: E402

RESTRICTED = {"client_fees", "client_file_norte_grande", "partner_meeting_minutes"}


def fake_response(text):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=1500, output_tokens=150,
                              cache_creation_input_tokens=0, cache_read_input_tokens=0),
        _request_id="req_fake")


class FakeClient:
    """Simula la API y guarda todo lo que se le mandó al modelo (para buscar fugas en el prompt)."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.sent.append(json.dumps({"system": kwargs["system"], "messages": [
            m if isinstance(m["content"], str) else {"role": m["role"], "content": "(respuesta previa)"}
            for m in kwargs["messages"]]}, ensure_ascii=False))
        return fake_response(self.replies.pop(0))


def reply(requires_human, source_ids=(), answer="No hay evidencia suficiente en los documentos; consultar a un socio."):
    return json.dumps({
        "status": "needs_clarification" if requires_human else "answered",
        "answer": answer, "explanation": "Respuesta armada solo con los fragmentos recibidos.",
        "confidence": 0.3 if requires_human else 0.9, "requires_human": requires_human,
        "source_ids": list(source_ids)})


class SpyCollection:
    """Envuelve la colección real y registra el `where` de cada consulta."""

    def __init__(self, col):
        self.col, self.wheres = col, []

    def query(self, **kw):
        self.wheres.append(("query", kw.get("where")))
        return self.col.query(**kw)

    def get(self, **kw):
        self.wheres.append(("get", kw.get("where")))
        return self.col.get(**kw)


class TestRag(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.client = rag.get_client(cls.tmp)
        cls.col = rag.ingest(rag.KB_DIR, "section", client=cls.client)
        cls.users = rag.load_users()
        cls.socio, cls.auxiliar = cls.users["nicolas"], cls.users["lucia"]
        cls.restricted_texts = [
            t for t, m in zip(*[cls.col.get(where={"role_auxiliar": False}, include=["documents", "metadatas"])[k]
                                for k in ("documents", "metadatas")])
        ]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # --- ingesta -----------------------------------------------------------------------------
    def test_ingest_every_chunk_has_document_section_and_roles(self):
        data = self.col.get(include=["metadatas"])
        self.assertEqual(len({m["document"] for m in data["metadatas"]}), len(load_documents(rag.KB_DIR)))
        for m in data["metadatas"]:
            for key in ("document", "section", "roles", "role_socio", "role_auxiliar", "version", "effective_date"):
                self.assertIn(key, m)
            self.assertEqual(m["role_auxiliar"], "auxiliar" in m["roles"].split(","))

    # --- permisos: rol auxiliar ----------------------------------------------------------------
    def test_auxiliar_gets_nothing_from_restricted_even_with_exact_text(self):
        # Entrada: el TEXTO EXACTO de cada fragmento restringido, en los 3 modos y con k grande.
        # Esperado: 0 fragmentos de documentos restringidos. Sin el filtro, serían el primer resultado.
        self.assertTrue(self.restricted_texts)
        for text in self.restricted_texts:
            for mode in ("vector", "bm25", "hybrid"):
                hits = rag.search(text, self.auxiliar, k=100, collection=self.col, mode=mode)
                self.assertFalse({h.document for h in hits} & RESTRICTED, f"fuga en modo {mode}")

    def test_role_filter_is_inside_the_db_query(self):
        spy = SpyCollection(self.col)
        rag.search("honorarios Ferretería Los Lapachos", self.auxiliar, k=5, collection=spy)
        self.assertEqual({w for _, w in [(c, json.dumps(w)) for c, w in spy.wheres]},
                         {json.dumps({"role_auxiliar": True})})
        self.assertEqual({c for c, _ in spy.wheres}, {"query", "get"})  # vectorial y corpus BM25

    def test_auxiliar_restricted_question_not_in_prompt_and_requires_human(self):
        fake = FakeClient([reply(True)])
        res = rag.answer(rag.entrada_for("¿Cuánto paga de honorarios mensuales Ferretería Los Lapachos?"),
                         self.auxiliar, collection=self.col, client=fake)
        self.assertTrue(res.ok)
        self.assertTrue(res.output.requires_human)
        self.assertFalse({c.split("::")[0] for c in res.retrieved} & RESTRICTED)
        prompt = "\n".join(fake.sent)
        for doc in RESTRICTED:
            self.assertNotIn(doc, prompt)
        self.assertNotIn("285.000", prompt)
        for text in self.restricted_texts:
            self.assertNotIn(text.splitlines()[-1], prompt)

    # --- permisos: rol socio (control positivo) --------------------------------------------------
    def test_socio_retrieves_restricted_document(self):
        hits = rag.search("¿Cuánto paga de honorarios mensuales Ferretería Los Lapachos?", self.socio, k=5,
                          collection=self.col)
        self.assertEqual(hits[0].document, "client_fees")

    def test_socio_answer_cites_restricted_source(self):
        q = "¿Qué plan de pagos tiene vigente Distribuidora Norte Grande con ARCA?"
        hits = rag.search(q, self.socio, k=5, collection=self.col)
        cid = next(h.chunk_id for h in hits if h.document == "client_file_norte_grande")
        fake = FakeClient([reply(False, [cid], "Plan P-884512 en 24 cuotas de $ 1.180.000.")])
        res = rag.answer(rag.entrada_for(q), self.socio, collection=self.col, client=fake)
        self.assertTrue(res.ok)
        self.assertFalse(res.output.requires_human)
        self.assertEqual(res.output.sources[0].document, "client_file_norte_grande")
        self.assertIn("P-884512", "\n".join(fake.sent))

    # --- respuesta con fuentes / requires_human ---------------------------------------------------
    def test_answered_without_sources_is_rejected_and_retried(self):
        fake = FakeClient([reply(False, []), reply(True)])
        res = rag.answer(rag.entrada_for("¿Cuál es la alícuota de Ganancias para sociedades?"),
                         self.socio, collection=self.col, client=fake)
        self.assertTrue(res.ok)
        self.assertEqual(res.metrics.attempts, 2)
        self.assertTrue(res.output.requires_human)

    def test_cannot_cite_a_chunk_that_was_not_retrieved(self):
        fake = FakeClient([reply(False, ["client_fees::section::001"], "Paga $ 285.000."),
                           reply(False, ["client_fees::section::001"], "Paga $ 285.000.")])
        res = rag.answer(rag.entrada_for("¿Cuándo vence el monotributo?"), self.auxiliar,
                         collection=self.col, client=fake)
        self.assertFalse(res.ok)
        self.assertEqual(res.error.kind, "output_validation")

    def test_no_hits_returns_requires_human_without_calling_api(self):
        empty = self.client.get_or_create_collection("kb_empty_test")
        fake = FakeClient([])
        res = rag.answer(rag.entrada_for("¿Cuándo vence el IVA?"), self.auxiliar, collection=empty, client=fake)
        self.assertTrue(res.output.requires_human)
        self.assertEqual(fake.sent, [])

    def test_invalid_input_skips_search_and_api(self):
        fake = FakeClient([])
        res = rag.answer(rag.entrada_for("hola") | {"client_cuit": "123"}, self.auxiliar,
                         collection=self.col, client=fake)
        self.assertEqual(res.error.kind, "input_validation")
        self.assertEqual(fake.sent, [])


if __name__ == "__main__":
    unittest.main()
