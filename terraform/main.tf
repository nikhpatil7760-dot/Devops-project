terraform {
  backend "local" {
    path = "../.local/terraform.tfstate"
  }

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.38"
    }
  }
}

provider "kubernetes" {
  config_path    = "~/.kube/config"
  config_context = var.kube_context
}

variable "kube_context" {
  description = "Kind context used for local Terraform-managed platform namespaces."
  type        = string
  default     = "kind-flask-platform"
}

resource "kubernetes_namespace_v1" "observability" {
  metadata {
    name = "observability"
    labels = {
      "app.kubernetes.io/part-of"    = "devops-platform"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
}