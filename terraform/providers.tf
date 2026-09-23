# The chicken-and-egg problem, and why this configuration avoids it.
#
# The natural thing to write is to feed the cluster's computed credentials into the
# helm provider — `host = module.cluster.endpoint`, and so on. That fails: provider
# configuration must be resolvable at plan time, and those attributes are unknown
# until the cluster is actually created. Terraform reports "Provider configuration
# not known yet", and the usual advice becomes "run it twice".
#
# Instead both values below derive only from input variables, so they are known
# before anything is created. kind writes to the default kubeconfig and names the
# context `kind-<cluster_name>`, which makes this deterministic rather than lucky.
provider "helm" {
  kubernetes = {
    config_path    = pathexpand(var.kubeconfig_path)
    config_context = "kind-${var.cluster_name}"
  }
}
