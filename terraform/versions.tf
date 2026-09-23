terraform {
  required_version = ">= 1.6"

  required_providers {
    # Community provider. kind has no official one, which is itself worth knowing:
    # for local-only infrastructure you are usually relying on community providers,
    # and pinning them matters more, not less.
    kind = {
      source  = "tehcyx/kind"
      version = "~> 0.9"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.0"
    }
  }
}
