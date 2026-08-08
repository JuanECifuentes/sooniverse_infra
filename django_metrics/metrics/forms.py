"""Formularios del gestor de API Keys."""

from datetime import date

from django import forms

from .models import TokenUsageRollup


class ApiKeyForm(forms.Form):
    """Alta de una nueva API Key (se emite en LiteLLM y se registra localmente)."""

    key_alias = forms.CharField(
        label="Alias", max_length=120,
        widget=forms.TextInput(attrs={
            "class": "sv-input", "placeholder": "ej. backend-produccion", "autocomplete": "off",
        }),
        help_text="Nombre legible para identificar la key en el panel.",
    )
    owner_email = forms.EmailField(
        label="Responsable", required=False,
        widget=forms.EmailInput(attrs={"class": "sv-input", "placeholder": "equipo@empresa.com"}),
    )
    descripcion = forms.CharField(
        label="Descripción", required=False, max_length=500,
        widget=forms.Textarea(attrs={
            "class": "sv-textarea", "rows": 2, "placeholder": "Uso previsto de esta credencial",
        }),
    )
    modelos = forms.MultipleChoiceField(
        label="Modelos permitidos", required=False, choices=[],
        widget=forms.CheckboxSelectMultiple,
    )
    rpm_limit = forms.IntegerField(
        label="Límite RPM", required=False, min_value=1,
        widget=forms.NumberInput(attrs={"class": "sv-input", "placeholder": "sin límite"}),
    )
    tpm_limit = forms.IntegerField(
        label="Límite TPM", required=False, min_value=1,
        widget=forms.NumberInput(attrs={"class": "sv-input", "placeholder": "sin límite"}),
    )
    vigencia = forms.DateField(
        label="Vigencia", required=False,
        widget=forms.DateInput(attrs={"class": "sv-input", "type": "text", "data-flatpickr": ""}),
    )

    def __init__(self, *args, modelos=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["modelos"].choices = [(m, m) for m in (modelos or [])]

    def clean_vigencia(self):
        vigencia = self.cleaned_data.get("vigencia")
        if vigencia and vigencia <= date.today():
            raise forms.ValidationError("La fecha de vigencia debe ser posterior a hoy.")
        return vigencia


class FiltroMetricasForm(forms.Form):
    """Filtro del panel: granularidad de agregación + API Keys + modelos + rango de fechas."""

    granularity = forms.ChoiceField(
        label="Agrupación", required=False,
        choices=TokenUsageRollup.GRANULARITIES, initial=TokenUsageRollup.DAILY,
    )
    api_key = forms.MultipleChoiceField(label="API Key", required=False, choices=[])
    modelo = forms.MultipleChoiceField(label="Modelo", required=False, choices=[])
    desde = forms.DateField(label="Desde", required=False)
    hasta = forms.DateField(label="Hasta", required=False)

    def __init__(self, *args, api_keys=None, modelos=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["api_key"].choices = [
            (str(k["id"]), k["key_alias"]) for k in (api_keys or [])
        ]
        self.fields["modelo"].choices = [(m, m) for m in (modelos or [])]

    def clean(self):
        cleaned = super().clean()
        desde, hasta = cleaned.get("desde"), cleaned.get("hasta")
        if desde and hasta and desde > hasta:
            self.add_error("hasta", "La fecha \"hasta\" no puede ser anterior a \"desde\".")
        return cleaned
