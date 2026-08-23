variable "sooniverse_account_id" {
  description = "ID de la cuenta AWS de Sooniverse (Cuenta A) autorizada para asumir este rol"
  type        = string
}

variable "external_id" {
  description = "Identificador único y secreto acordado entre el cliente y Sooniverse para prevenir el ataque Confused Deputy"
  type        = string
}

variable "role_name" {
  description = "Nombre del rol IAM a crear en la cuenta del cliente"
  type        = string
  default     = "SooniverseDeployRole"
}

variable "aws_region" {
  description = "Región de AWS por defecto"
  type        = string
  default     = "us-east-1"
}
