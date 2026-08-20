"""
Almacenamiento de estáticos del panel.

Existe por una razón concreta: desde que el JS del panel usa ES modules
(`import { fmtTok } from "./format.js"`), el hashing de
`ManifestStaticFilesStorage` rompe esos imports salvo que se le pida
explícitamente que también los reescriba.

`support_js_module_import_aggregation` es False por defecto en Django, así que
sin esta subclase el navegador pediría `/static/js/format.js` (sin hash) desde
dentro de un `metrics-charts.<hash>.js`, y recibiría un 404 que tumba el módulo
entero -y con él, toda la interactividad de la página-.
"""

from whitenoise.storage import CompressedManifestStaticFilesStorage


class SooniverseStaticFilesStorage(CompressedManifestStaticFilesStorage):
    # Reescribe también las rutas de `import`/`export ... from` dentro de los
    # .js, no solo las `url()` de los .css.
    support_js_module_import_aggregation = True
