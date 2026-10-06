terraform {
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.38"
    }
  }
}

provider "kubernetes" {
  config_path = "~/.kube/config"
}

resource "kubernetes_namespace" "podinfo" {
  metadata {
    name = "podinfo"
  }
}

resource "kubernetes_deployment" "podinfo" {
  metadata {
    name      = "podinfo"
    namespace = kubernetes_namespace.podinfo.metadata[0].name
  }

  spec {
    replicas = 2

    selector {
      match_labels = {
        app = "podinfo"
      }
    }

    template {
      metadata {
        labels = {
          app = "podinfo"
        }
      }

      spec {
        container {
          name  = "podinfo"
          image = "ghcr.io/stefanprodan/podinfo:6.15.0"

          port {
            container_port = 9898
          }

          readiness_probe {
            http_get {
              path = "/readyz"
              port = 9898
            }
            initial_delay_seconds = 5
            period_seconds        = 5
          }

          liveness_probe {
            http_get {
              path = "/healthz"
              port = 9898
            }
            initial_delay_seconds = 10
            period_seconds        = 10
          }
        }
      }
    }
  }
}

resource "kubernetes_service" "podinfo" {
  metadata {
    name      = "podinfo"
    namespace = kubernetes_namespace.podinfo.metadata[0].name
  }

  spec {
    selector = {
      app = "podinfo"
    }

    port {
      port        = 9898
      target_port = 9898
    }
  }
}
