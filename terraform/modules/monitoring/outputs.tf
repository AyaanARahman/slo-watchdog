output "release_name" {
  description = "Name of the Helm release."
  value       = helm_release.kube_prometheus_stack.name
}

output "namespace" {
  description = "Namespace the release was installed into."
  value       = helm_release.kube_prometheus_stack.namespace
}

output "chart_version" {
  description = "Chart version actually deployed."
  value       = helm_release.kube_prometheus_stack.version
}
