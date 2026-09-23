terraform {
  required_providers {
    kind = {
      source  = "tehcyx/kind"
      version = "~> 0.9"
    }
  }
}

# A single control-plane node, which kind leaves untainted so workloads schedule on
# it. A worker node costs another ~500MB of container overhead, and on a laptop that
# budget is better spent on Prometheus than on demonstrating a scheduler with one
# place to put things.
resource "kind_cluster" "this" {
  name            = var.cluster_name
  node_image      = var.node_image
  kubeconfig_path = pathexpand(var.kubeconfig_path)
  wait_for_ready  = true

  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"

    node {
      role = "control-plane"

      # Host ports mapped straight through to NodePorts, so the harness gets stable
      # URLs. The alternative — `kubectl port-forward` — is a long-lived subprocess
      # the harness would have to spawn, health-check and reap per scenario, and it
      # dies on pod restarts, which is exactly what a chaos harness provokes.
      dynamic "extra_port_mappings" {
        for_each = var.node_ports
        content {
          container_port = extra_port_mappings.value
          host_port      = extra_port_mappings.value
          protocol       = "TCP"
        }
      }
    }
  }
}
