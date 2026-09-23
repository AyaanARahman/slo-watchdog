output "cluster_name" {
  description = "Name of the provisioned kind cluster."
  value       = module.cluster.cluster_name
}

output "kubeconfig_context" {
  description = "kubectl context for this cluster."
  value       = module.cluster.kubeconfig_context
}

output "urls" {
  description = "Host URLs for every component, via the kind port mappings."
  value = {
    budget_api   = "http://localhost:${var.node_ports.budget_api}"
    prometheus   = "http://localhost:${var.node_ports.prometheus}"
    alertmanager = "http://localhost:${var.node_ports.alertmanager}"
    grafana      = "http://localhost:${var.node_ports.grafana}"
  }
}

output "next_steps" {
  description = "Terraform stops at the platform; this is how the app gets deployed."
  value       = "make deploy dashboards slo-apply && make harness"
}
