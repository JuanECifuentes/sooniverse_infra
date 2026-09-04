"""
==============================================================================
Soporte del módulo Credenciales: el rol de Administrador
==============================================================================
Separación de roles del clúster (dos banderas deliberadamente independientes):

  · Acceso al panel  -> `User.is_staff`: entra al panel de métricas y gestiona
    API Keys. NO ve la tab de credenciales ni puede crear/modificar usuarios.
  · Administrador    -> miembro del Group 'Administrador' (nombre en
    settings.GRUPO_ADMIN_CREDENCIALES): TODO lo anterior más la tab de
    credenciales (crear/modificar/deshabilitar usuarios) y el admin de Django
    (/admin/), para lo cual el grupo recibe los permisos sobre auth.User.

El superuser de Django (cuenta técnica de despliegue) pasa siempre el check de
administrador: es el cinturón de arranque cuando el grupo aún no existe.

Nota de diseño: usamos un Group -no `is_superuser`- para el rol de
administrador de credenciales; así las cuentas administrativas siguen siendo
editables desde el propio panel y no quedan con poder total sobre Django.
"""

from django.conf import settings
from django.contrib.auth.models import Group, Permission

_PERMISOS_ADMIN = ("add_user", "change_user", "delete_user", "view_user")


def nombre_grupo_admin() -> str:
    return getattr(settings, "GRUPO_ADMIN_CREDENCIALES", "Administrador")


def es_admin_credenciales(user) -> bool:
    """True si la cuenta puede ver/gestionar la tab de credenciales."""
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True
    return user.groups.filter(name=nombre_grupo_admin()).exists()


def grupo_administradores() -> Group:
    """Grupo del rol, creado a demanda con los permisos del admin de Django
    sobre auth.User. `permissions.add` es idempotente, así que llamarlo en
    cada acceso a la tab es seguro (y repara grupos viejos sin permisos)."""
    grupo, _ = Group.objects.get_or_create(name=nombre_grupo_admin())
    grupo.permissions.add(
        *Permission.objects.filter(
            codename__in=_PERMISOS_ADMIN, content_type__app_label="auth"
        )
    )
    return grupo


def sincronizar_grupo_admin(usuario, es_admin: bool) -> None:
    """Alinea la membresía del grupo con la casilla 'Administrador'."""
    grupo = grupo_administradores()
    if es_admin:
        usuario.groups.add(grupo)
    else:
        usuario.groups.remove(grupo)
