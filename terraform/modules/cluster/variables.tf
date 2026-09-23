variable "cluster_name" {
  description = "Name of the kind cluster."
  type        = string
}

variable "node_image" {
  description = "Pinned kind node image."
  type        = string
}

variable "kubeconfig_path" {
  description = "Where kind writes the kubeconfig."
  type        = string
}

variable "node_ports" {
  description = "Host port -> NodePort mappings. Immutable after cluster creation."
  type        = map(number)
}
