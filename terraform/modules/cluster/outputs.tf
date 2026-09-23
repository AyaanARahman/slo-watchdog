output "cluster_name" {
  description = "Name of the created cluster."
  value       = kind_cluster.this.name
}

output "kubeconfig_context" {
  description = "kubectl context kind creates for this cluster."
  value       = "kind-${kind_cluster.this.name}"
}

output "endpoint" {
  description = "Kubernetes API server endpoint."
  value       = kind_cluster.this.endpoint
}
