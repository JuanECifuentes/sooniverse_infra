terraform {
  required_version = ">= 1.3.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 4.0, < 6.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# -----------------------------------------------------------------------------
# 1. Trust Policy (Política de Confianza)
# Permite que la cuenta AWS de Sooniverse asuma este rol, exigiendo el External ID.
# -----------------------------------------------------------------------------
data "aws_iam_policy_document" "assume_role_trust_policy" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${var.sooniverse_account_id}:root"]
    }

    condition {
      test     = "StringEquals"
      variable = "sts:ExternalId"
      values   = [var.external_id]
    }
  }
}

# -----------------------------------------------------------------------------
# 2. Permissions Policy (Política de Permisos de Infraestructura)
# Permite el aprovisionamiento de VPC, subredes, cómputo GPU y gateway.
# -----------------------------------------------------------------------------
data "aws_iam_policy_document" "sooniverse_permissions" {
  # Gestión de Red, VPCs, Security Groups, Instancias EC2, Volúmenes y NAT
  statement {
    sid    = "SooniverseEC2AndVPCManagement"
    effect = "Allow"
    actions = [
      "ec2:CreateVpc",
      "ec2:DeleteVpc",
      "ec2:DescribeVpcs",
      "ec2:ModifyVpcAttribute",
      "ec2:CreateSubnet",
      "ec2:DeleteSubnet",
      "ec2:DescribeSubnets",
      "ec2:ModifySubnetAttribute",
      "ec2:CreateInternetGateway",
      "ec2:DeleteInternetGateway",
      "ec2:AttachInternetGateway",
      "ec2:DetachInternetGateway",
      "ec2:DescribeInternetGateways",
      "ec2:CreateRouteTable",
      "ec2:DeleteRouteTable",
      "ec2:DescribeRouteTables",
      "ec2:AssociateRouteTable",
      "ec2:DisassociateRouteTable",
      "ec2:CreateRoute",
      "ec2:DeleteRoute",
      "ec2:AllocateAddress",
      "ec2:ReleaseAddress",
      "ec2:DescribeAddresses",
      "ec2:AssociateAddress",
      "ec2:DisassociateAddress",
      "ec2:CreateNatGateway",
      "ec2:DeleteNatGateway",
      "ec2:DescribeNatGateways",
      "ec2:CreateVpcEndpoint",
      "ec2:DeleteVpcEndpoints",
      "ec2:DescribeVpcEndpoints",
      "ec2:CreateSecurityGroup",
      "ec2:DeleteSecurityGroup",
      "ec2:DescribeSecurityGroups",
      "ec2:AuthorizeSecurityGroupIngress",
      "ec2:RevokeSecurityGroupIngress",
      "ec2:AuthorizeSecurityGroupEgress",
      "ec2:RevokeSecurityGroupEgress",
      "ec2:RunInstances",
      "ec2:TerminateInstances",
      "ec2:StartInstances",
      "ec2:StopInstances",
      "ec2:RebootInstances",
      "ec2:DescribeInstances",
      "ec2:DescribeInstanceStatus",
      "ec2:DescribeInstanceTypes",
      "ec2:DescribeImages",
      "ec2:DescribeVolumes",
      "ec2:CreateVolume",
      "ec2:DeleteVolume",
      "ec2:AttachVolume",
      "ec2:DetachVolume",
      "ec2:CreateTags",
      "ec2:DeleteTags",
      "ec2:DescribeTags",
      "ec2:DescribeKeyPairs",
      "ec2:CreateKeyPair",
      "ec2:DeleteKeyPair",
      "ec2:ImportKeyPair",
      "ec2:DescribeAvailabilityZones",
      "ec2:DescribeAccountAttributes",
      # Requeridos por el cliente de SkyPilot (no por el código propio de Sooniverse)
      # para resolver la región/zonas y catálogo de tipos de instancia antes de lanzar:
      # confirmado empíricamente, 'sky launch' falla con "Failed to retrieve AWS
      # regions" / catálogo vacío sin estos tres.
      "ec2:DescribeRegions",
      "ec2:DescribeInstanceTypeOfferings",
      "ec2:DescribeNetworkInterfaces"
    ]
    resources = ["*"]
  }

  # Permisos auxiliares para SkyPilot e Instance Profiles
  statement {
    sid    = "SooniverseIAMSupport"
    effect = "Allow"
    actions = [
      "iam:PassRole",
      "iam:CreateServiceLinkedRole",
      "iam:GetInstanceProfile",
      "iam:ListInstanceProfiles"
    ]
    resources = ["*"]
  }

  # Métricas y monitoreo
  statement {
    sid    = "SooniverseCloudWatchSupport"
    effect = "Allow"
    actions = [
      "cloudwatch:PutMetricData",
      "cloudwatch:GetMetricData",
      "cloudwatch:ListMetrics",
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams"
    ]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "sooniverse_policy" {
  name        = "${var.role_name}Policy"
  description = "Permisos para despliegue y mantenimiento de Sooniverse Infra"
  policy      = data.aws_iam_policy_document.sooniverse_permissions.json
}

# -----------------------------------------------------------------------------
# 3. Creación del IAM Role y Attach
# -----------------------------------------------------------------------------
resource "aws_iam_role" "sooniverse_role" {
  name               = var.role_name
  description        = "Rol delegado para Sooniverse Infra (BYOC)"
  assume_role_policy = data.aws_iam_policy_document.assume_role_trust_policy.json

  tags = {
    "ManagedBy"   = "Sooniverse"
    "Environment" = "BYOC"
  }
}

resource "aws_iam_role_policy_attachment" "sooniverse_attach" {
  role       = aws_iam_role.sooniverse_role.name
  policy_arn = aws_iam_policy.sooniverse_policy.arn
}
