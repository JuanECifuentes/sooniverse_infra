"""Formularios del gestor de API Keys, del login unificado y de credenciales."""

from datetime import date

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError


class LoginForm(forms.Form):
    """Login único del clúster (panel + chat vía SSO). Acepta usuario o correo
    indistintamente -ver metrics/auth_backends.py::UsernameOrEmailBackend."""

    identificador = forms.CharField(
        label="Usuario o correo",
        max_length=254,
        widget=forms.TextInput(
            attrs={
                "class": "sv-input",
                "placeholder": "usuario o correo",
                "autocomplete": "username",
                "autofocus": True,
            }
        ),
    )
    password = forms.CharField(
        label="Contraseña",
        widget=forms.PasswordInput(
            attrs={"class": "sv-input", "autocomplete": "current-password"}
        ),
    )


class ApiKeyForm(forms.Form):
    """Alta de una nueva API Key (se emite en LiteLLM y se registra localmente)."""

    key_alias = forms.CharField(
        label="Alias",
        max_length=120,
        widget=forms.TextInput(
            attrs={
                "class": "sv-input",
                "placeholder": "ej. backend-produccion",
                "autocomplete": "off",
            }
        ),
        help_text="Nombre legible para identificar la key en el panel.",
    )
    owner_email = forms.EmailField(
        label="Responsable",
        required=False,
        widget=forms.EmailInput(
            attrs={"class": "sv-input", "placeholder": "equipo@empresa.com"}
        ),
    )
    descripcion = forms.CharField(
        label="Descripción",
        required=False,
        max_length=500,
        widget=forms.Textarea(
            attrs={
                "class": "sv-textarea",
                "rows": 2,
                "placeholder": "Uso previsto de esta credencial",
            }
        ),
    )
    modelos = forms.MultipleChoiceField(
        label="Modelos permitidos",
        required=False,
        choices=[],
        widget=forms.CheckboxSelectMultiple,
    )
    rpm_limit = forms.IntegerField(
        label="Límite RPM",
        required=False,
        min_value=1,
        widget=forms.NumberInput(
            attrs={"class": "sv-input", "placeholder": "sin límite"}
        ),
    )
    tpm_limit = forms.IntegerField(
        label="Límite TPM",
        required=False,
        min_value=1,
        widget=forms.NumberInput(
            attrs={"class": "sv-input", "placeholder": "sin límite"}
        ),
    )
    vigencia = forms.DateField(
        label="Vigencia",
        required=False,
        widget=forms.DateInput(
            attrs={"class": "sv-input", "type": "text", "data-flatpickr": ""}
        ),
    )

    def __init__(self, *args, modelos=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["modelos"].choices = [(m, m) for m in (modelos or [])]

    def clean_vigencia(self):
        vigencia = self.cleaned_data.get("vigencia")
        if vigencia and vigencia <= date.today():
            raise forms.ValidationError(
                "La fecha de vigencia debe ser posterior a hoy."
            )
        return vigencia


# =============================================================================
# Credenciales (CRUD de usuarios del clúster, solo staff)
# =============================================================================
_USERNAME_VALIDATOR = UnicodeUsernameValidator()

_INPUT = {"class": "sv-input"}


class _PasswordMixin:
    """Validación de contraseña compartida: confirmación coincidente y políticas
    de Django (AUTH_PASSWORD_VALIDATORS: longitud, similitud con el usuario,
    contraseñas comunes, 100% numérica)."""

    def _validar_password(self, password: str, usuario=None):
        if password != self.cleaned_data.get("password2"):
            raise forms.ValidationError("Las contraseñas no coinciden.")
        try:
            validate_password(password, user=usuario)
        except ValidationError as errores:
            # Reempaquetado: los mensajes de los validadores son para humanos.
            self.add_error("password", errores)
            return None
        return password


class CredencialCreateForm(_PasswordMixin, forms.Form):
    """Alta de una cuenta del clúster. El email es OBLIGATORIO aunque Django lo
    permita vacío: es la identidad que el chat recibe vía SSO
    (X-Sooniverse-Email, ver metrics/views.py::auth_check) —sin él la cuenta no
    puede entrar al chat."""

    username = forms.CharField(
        label="Usuario",
        max_length=150,
        validators=[_USERNAME_VALIDATOR],
        widget=forms.TextInput(
            attrs={**_INPUT, "autocomplete": "off", "autofocus": True}
        ),
        help_text="Con este nombre se inicia sesión en el panel y en el chat.",
    )
    email = forms.EmailField(
        label="Correo",
        widget=forms.EmailInput(attrs={**_INPUT, "autocomplete": "off"}),
        help_text="Identidad del chat (SSO). Único en el clúster.",
    )
    first_name = forms.CharField(
        label="Nombre",
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs=_INPUT),
    )
    last_name = forms.CharField(
        label="Apellido",
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs=_INPUT),
    )
    password = forms.CharField(
        label="Contraseña",
        widget=forms.PasswordInput(attrs={**_INPUT, "autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label="Confirmar contraseña",
        widget=forms.PasswordInput(attrs={**_INPUT, "autocomplete": "new-password"}),
    )
    is_staff = forms.BooleanField(
        label="Administrador (acceso al panel)",
        required=False,
        initial=True,
    )
    is_active = forms.BooleanField(label="Cuenta activa", required=False, initial=True)

    def clean_username(self):
        username = self.cleaned_data["username"]
        if get_user_model().objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("Ya existe una cuenta con ese usuario.")
        return username

    def clean_email(self):
        # El backend de login resuelve el email con lookup case-insensitive
        # (auth_backends.UsernameOrEmailBackend): un duplicado aquí rompería el
        # login de AMBAS cuentas, se previene en la puerta de entrada.
        email = self.cleaned_data["email"].lower()
        if get_user_model().objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Ya existe una cuenta con ese correo.")
        return email

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        if password:
            self._validar_password(password)
        return cleaned


class CredencialEditForm(_PasswordMixin, forms.Form):
    """Edición de una cuenta existente. El username NO se edita (es el
    identificador en LiteLLM/logs/sesiones). Contraseña opcional: vacía =
    dejar la actual."""

    email = forms.EmailField(
        label="Correo",
        widget=forms.EmailInput(attrs={**_INPUT, "autocomplete": "off"}),
        help_text="Identidad del chat (SSO). Único en el clúster.",
    )
    first_name = forms.CharField(
        label="Nombre",
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs=_INPUT),
    )
    last_name = forms.CharField(
        label="Apellido",
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs=_INPUT),
    )
    password = forms.CharField(
        label="Nueva contraseña (opcional)",
        required=False,
        widget=forms.PasswordInput(attrs={**_INPUT, "autocomplete": "new-password"}),
        help_text="Déjalo vacío para mantener la contraseña actual.",
    )
    password2 = forms.CharField(
        label="Confirmar nueva contraseña",
        required=False,
        widget=forms.PasswordInput(attrs={**_INPUT, "autocomplete": "new-password"}),
    )
    is_staff = forms.BooleanField(
        label="Administrador (acceso al panel)", required=False
    )
    is_active = forms.BooleanField(label="Cuenta activa", required=False)

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.usuario = usuario
        if usuario is not None and not self.is_bound:
            self.initial.update(
                email=usuario.email,
                first_name=usuario.first_name,
                last_name=usuario.last_name,
                is_staff=usuario.is_staff,
                is_active=usuario.is_active,
            )

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        duplicados = get_user_model().objects.filter(email__iexact=email)
        if self.usuario is not None:
            duplicados = duplicados.exclude(pk=self.usuario.pk)
        if duplicados.exists():
            raise forms.ValidationError("Ya existe otra cuenta con ese correo.")
        return email

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        if password:
            self._validar_password(password, usuario=self.usuario)
        elif cleaned.get("password2"):
            self.add_error("password2", "Rellena también la nueva contraseña.")
        return cleaned

    def guardar_en(self, usuario):
        usuario.email = self.cleaned_data["email"]
        usuario.first_name = self.cleaned_data["first_name"]
        usuario.last_name = self.cleaned_data["last_name"]
        usuario.is_staff = self.cleaned_data["is_staff"]
        usuario.is_active = self.cleaned_data["is_active"]
        if self.cleaned_data["password"]:
            usuario.set_password(self.cleaned_data["password"])
        usuario.save()
