"""
Pruebas del panel de métricas.

    cd django_metrics && python manage.py test metrics

TODAS las clases de este paquete son `SimpleTestCase`, y eso es load-bearing:
los modelos son `managed = False` (el DDL vive en `database/*.sql`), así que una
base de datos de pruebas creada por Django saldría vacía e inútil. Con
`SimpleTestCase` el runner no crea ninguna: `databases` está vacío por defecto.

La consecuencia de diseño buscada es que la lógica interesante viva en funciones
puras -detección de rachas ociosas, regresión, densificación de la rejilla,
formato- en vez de incrustada en un QuerySet. Lo que sí necesita PostgreSQL
(percentile_cont, EXTRACT con zona horaria) se cubre en el despliegue real.

Sin pytest-django, sin pytest.ini y sin dependencias nuevas: así la colección de
las pruebas de infraestructura de `tests/` en la raíz no se ve afectada.
"""
