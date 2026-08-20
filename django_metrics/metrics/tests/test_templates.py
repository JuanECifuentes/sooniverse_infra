"""
Invariantes de las plantillas Django.

Nace de un bug real: la sintaxis de comentario de Django que empieza con
llave-almohadilla NO admite saltos de línea (a diferencia del bloque
comment/endcomment). Un comentario de una sola idea escrito en varias líneas
físicas no se reconoce como token de comentario y el motor de plantillas lo
deja pasar como texto literal -las llaves, la almohadilla y todo el contenido
aparecían impresos en la página real, delante del usuario, en la barra de
filtros y en la página de capacidad-.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"

# Construido con chr() para que este propio archivo no contenga una secuencia
# de llave-almohadilla literal (evita confundir a cualquier herramienta, propia
# o de terceros, que procese plantillas Django sobre el repo).
_ABRE = chr(123) + chr(35)   # "{#"
_CIERRA = chr(35) + chr(125)  # "#}"
COMENTARIO_HASH_RE = re.compile(re.escape(_ABRE) + r"(.*?)" + re.escape(_CIERRA), re.DOTALL)


class ComentariosDjangoBienFormados(SimpleTestCase):
    def test_ningun_comentario_hash_abarca_varias_lineas(self):
        rotos = []
        for path in TEMPLATES_DIR.rglob("*.html"):
            texto = path.read_text(encoding="utf-8")
            for m in COMENTARIO_HASH_RE.finditer(texto):
                if "\n" in m.group(1):
                    rotos.append(f"{path.relative_to(TEMPLATES_DIR)}: {m.group(0)[:60]!r}")
        self.assertFalse(
            rotos,
            "Comentario Django de llave-almohadilla multilínea (se renderiza como "
            "texto literal; usa el bloque comment/endcomment en su lugar):\n"
            + "\n".join(rotos),
        )
