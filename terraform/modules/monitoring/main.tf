terraform {
  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.0"
    }
  }
}

resource "helm_release" "kube_prometheus_stack" {
  name       = var.release_name
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "kube-prometheus-stack"
  version    = var.chart_version

  namespace        = var.namespace
  create_namespace = true

  values = [file(var.values_file)]

  # Block until every pod is ready. Without this, Terraform reports success the
  # moment Helm accepts the manifests, and a subsequent `kubectl apply` of the
  # ServiceMonitor races the Operator's CRD installation.
  wait = true
  # The chart pulls several images on first run; 10 minutes is generous but a
  # cold-cache install on a laptop genuinely takes minutes.
  timeout = 900
}
