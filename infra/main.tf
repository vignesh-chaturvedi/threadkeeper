# Threadkeeper on ECS Fargate.
#
# One module, on purpose. The plan asks for "an honest Terraform bullet, not a
# production platform", and splitting eight resources across four modules with a
# variables file each would be architecture for its own sake. When this needs a
# second environment it needs modules; it does not have a second environment.
#
# What this deliberately does NOT do:
#   * no NAT gateway — it is $32/month to let private subnets reach the internet,
#     and the tasks run in public subnets with no inbound rules instead. That is
#     a real tradeoff, written down in the README rather than hidden.
#   * no autoscaling — two tasks, fixed. Scaling policy without load data is
#     guesswork with a monthly bill attached.
#   * no multi-AZ RDS, no read replica, no snapshot schedule. This is a demo
#     holding zero real customer data, and pretending otherwise costs money.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  name = var.project

  # The drain contract, in the one place both halves of it are visible.
  # TK_DRAIN_TIMEOUT_S (25) < uvicorn graceful shutdown (30) < stopTimeout (40).
  # Each must exceed the one before it or the drain is cut off by the thing
  # meant to be waiting for it. There is a test asserting exactly this, reading
  # all three out of the files they live in.
  stop_timeout_s = 40

  # The load balancer stops sending new requests and waits this long for
  # existing ones before ECS sends SIGTERM. It only has to cover in-flight HTTP
  # requests — the webhook returns in milliseconds and does its work in a
  # background task, so this is short and the *task* drain is what matters.
  deregistration_delay_s = 15
}
