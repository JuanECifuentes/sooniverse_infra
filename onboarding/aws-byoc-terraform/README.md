# Sooniverse BYOC — Onboarding Terraform Module

Este módulo de Terraform crea el rol IAM necesario en la cuenta AWS del cliente para permitir que **Sooniverse** despliegue y gestione la infraestructura de inferencia (LLMs / GPUs) de forma 100% aislada dentro de su propia nube (**BYOC - Bring Your Own Cloud**).

---

## 🔒 Garantías de Seguridad

1. **Sin intercambio de claves fijas**: No es necesario crear ni compartir usuarios IAM ni `AWS Access Key / Secret Key`.
2. **Acceso efímero y temporal**: Sooniverse asume el rol mediante **AWS Security Token Service (STS)** con tokens de corta duración.
3. **Protección contra Confused Deputy**: Se exige un `External ID` secreto y único acordado entre ambas partes.
4. **Auditabilidad total**: Cada acción ejecutada por Sooniverse queda registrada con su nombre en el **AWS CloudTrail** de su cuenta.
5. **Revocación inmediata**: Puede revocar el acceso en cualquier momento eliminando el rol con `terraform destroy`.

---

## 🚀 Instrucciones de Uso para el Cliente

### 1. Prerrequisitos
- Tener instalado [Terraform](https://www.terraform.io/downloads) (versión >= 1.3.0).
- Tener configuradas las credenciales de AWS CLI en su terminal con permisos de administrador en su cuenta.

### 2. Configuración de Variables
Copia el archivo de ejemplo:
```bash
cp terraform.tfvars.example terraform.tfvars
```

Edita `terraform.tfvars` con los datos provistos por el equipo de Sooniverse:
```hcl
sooniverse_account_id = "ID_CUENTA_SOONIVERSE"
external_id           = "EXTERNAL_ID_ACORDADO"
aws_region            = "us-east-1"
```

### 3. Despliegue
Ejecuta los siguientes comandos en esta carpeta:

```bash
# Inicializar proveedores
terraform init

# Verificar los cambios a realizar
terraform plan

# Aplicar y crear el rol
terraform apply
```

Al finalizar, Terraform mostrará el `role_arn` generado:
```text
Outputs:
role_arn = "arn:aws:iam::222222222222:role/SooniverseDeployRole"
```

### 4. Notificar a Sooniverse
Envía el valor de `role_arn` al equipo de Sooniverse para iniciar el despliegue de sus modelos.
