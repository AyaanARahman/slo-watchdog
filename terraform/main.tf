# Terraform owns the *infrastructure*: the cluster and the monitoring platform.
# It deliberately does NOT own the application.
#
# Why the boundary sits there:
#
#   * Application manifests live in k8s/ as plain Kubernetes YAML, applied with
#     kubectl. If Terraform owned the Deployment, every application rollout would
#     become a `terraform apply` against shared state — slow, lock-contended, and
#     wrong for something that changes many times a day. Infrastructure changes
#     weekly; workloads change hourly. Different lifecycles deserve different tools.
#
#   * It also sidesteps the well-known Terraform/CRD ordering problem. ServiceMonitor
#     and PrometheusRule are custom resources whose CRDs are installed by the Helm
#     release in this very configuration. `kubernetes_manifest` resolves its schema
#     against the live API server *at plan time*, so planning them in the same run
#     that installs their CRDs fails with "no matches for kind". The usual
#     workarounds are a two-stage targeted apply or a provider that defers schema
#     resolution. Not owning workloads here avoids the problem rather than papering
#     over it.
#
# So: `terraform apply` gives you a cluster with a working monitoring platform.
# `make deploy dashboards slo-apply` puts the application on it.

module "cluster" {
  source = "./modules/cluster"

  cluster_name    = var.cluster_name
  node_image      = var.node_image
  kubeconfig_path = var.kubeconfig_path
  node_ports      = var.node_ports
}

module "monitoring" {
  source = "./modules/monitoring"

  release_name  = "monitoring"
  namespace     = var.monitoring_namespace
  chart_version = var.monitoring_chart_version
  values_file   = "${path.root}/../monitoring/kube-prometheus-stack-values.yaml"

  # The cluster must exist before Helm can talk to it. The provider configuration
  # below references only variables, so this explicit edge is what actually orders
  # the two modules — there is no implicit data dependency to infer it from.
  depends_on = [module.cluster]
}
