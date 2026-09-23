variable "cluster_name" {
  description = "Name of the kind cluster. Also determines the kubeconfig context name."
  type        = string
  default     = "slo-watchdog"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$", var.cluster_name))
    error_message = "cluster_name must be a lowercase DNS-style label."
  }
}

variable "node_image" {
  description = <<-EOT
    kind node image, pinned so the Kubernetes version is reproducible.

    Capped by the PROVIDER, not by the kind CLI. tehcyx/kind 0.11.0 embeds
    sigs.k8s.io/kind v0.31.0, whose newest supported node image is v1.35.0. Asking
    for v1.37.0 (which the v0.33 CLI defaults to) makes the embedded kind emit a
    kubeadm config using an API version Kubernetes 1.37 has removed, and `kubeadm
    init` fails inside the container with a message that says nothing about version
    skew.

    kind.yaml pins the same image so `make cluster` and `terraform apply` produce
    identical clusters.
  EOT
  type        = string
  default     = "kindest/node:v1.35.0"
}

variable "kubeconfig_path" {
  description = "Kubeconfig kind writes to, and that the helm provider reads."
  type        = string
  default     = "~/.kube/config"
}

variable "monitoring_namespace" {
  description = "Namespace for the kube-prometheus-stack release."
  type        = string
  default     = "monitoring"
}

variable "monitoring_chart_version" {
  description = <<-EOT
    kube-prometheus-stack chart version. Pinned, not floating: the chart ships the
    Prometheus Operator CRDs, so an unpinned upgrade can change CRD schemas
    underneath resources this configuration does not manage.
  EOT
  type        = string
  default     = "90.0.0"
}

variable "node_ports" {
  description = <<-EOT
    Host port -> NodePort mappings, baked into the cluster at creation time.

    These are the addresses the chaos harness talks to. Changing them forces the
    cluster to be replaced, which is correct: a kind cluster's port mappings are
    immutable after creation, and Terraform should surface that as a replacement
    rather than silently drifting.
  EOT
  type        = map(number)
  default = {
    budget_api   = 30080
    prometheus   = 30090
    alertmanager = 30093
    grafana      = 30030
  }
}
