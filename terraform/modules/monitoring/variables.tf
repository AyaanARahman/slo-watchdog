variable "release_name" {
  description = "Helm release name. Becomes the prefix of every resource the chart creates."
  type        = string
}

variable "namespace" {
  description = "Namespace for the release."
  type        = string
}

variable "chart_version" {
  description = "Pinned kube-prometheus-stack chart version."
  type        = string
}

variable "values_file" {
  description = <<-EOT
    Path to the chart values file.

    Deliberately a file reference rather than inline HCL `set` blocks: the same file
    is used by `make monitoring`, so the Terraform path and the Makefile path cannot
    drift into configuring two different monitoring stacks.
  EOT
  type        = string
}
