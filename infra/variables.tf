variable "project" {
  type        = string
  default     = "threadkeeper"
  description = "Name prefix for every resource. Short — ALB target groups cap at 32 characters."
}

variable "region" {
  type        = string
  default     = "ap-south-1"
  description = "Mumbai. The customers this funnel is for are in India, and so is the data."
}

variable "image" {
  type        = string
  description = "Full ECR image URI including tag. Set by the deploy script, never latest."
}

variable "vpc_cidr" {
  type    = string
  default = "10.42.0.0/16"
}

# Two AZs is the minimum an ALB will accept, and the minimum for an RDS subnet
# group. Not a resilience claim — a single-AZ database in a demo is honest, and
# multi_az below is off.
variable "azs" {
  type    = list(string)
  default = ["ap-south-1a", "ap-south-1b"]
}

variable "db_instance_class" {
  type        = string
  default     = "db.t4g.micro"
  description = "Smallest Graviton instance. ~$13/month."
}

variable "cache_node_type" {
  type        = string
  default     = "cache.t4g.micro"
  description = "~$12/month."
}

variable "api_desired_count" {
  type        = number
  default     = 2
  description = <<-EOT
    Two, so a deploy has somewhere to send traffic while the other task drains.
    One task means every rolling deploy is an outage — which is the whole reason
    the drain exists.
  EOT
}

variable "worker_desired_count" {
  type        = number
  default     = 1
  description = <<-EOT
    One is enough and more is safe: the scheduler claims work with
    FOR UPDATE SKIP LOCKED, so two workers never send the same nudge. There is a
    test firing five concurrent claims that asserts exactly one wins.
  EOT
}

variable "task_cpu" {
  type    = number
  default = 512
}

variable "task_memory" {
  type    = number
  default = 1024
}

variable "log_retention_days" {
  type        = number
  default     = 14
  description = "Logs carry conversation ids, so they are PII-adjacent and expire."
}

variable "whatsapp_verify_token" {
  type        = string
  default     = "threadkeeper-verify"
  description = "Echoed during Meta's webhook subscription handshake. Not a secret."
}
